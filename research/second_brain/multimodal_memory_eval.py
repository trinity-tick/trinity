"""
# status: orphan (2026-08-15 audit, not in runtime path)
P14-3: Multi-Session Multimodal Evaluation (对标 WorldMemArena)
================================================================

核心设计（基于 WorldMemArena Multi-Session Multimodal Benchmark）：
  - MultimodalEvaluator：同时评测文本+图像记忆的端到端表现
  - UtilityVsRecallMetric：区分检索可用性（实际决策有用率）和原始召回覆盖率
    防止"高召回低质量"的评测盲区
  - SessionBoundaryDetector：检测跨会话记忆衰减，量化"新会话后精度下降率"
  - AgenticExecutionDegradation：测量代理主动执行时记忆质量的退化幅度
    （从被动回答 ~55% QA-C 到主动执行后的下降）
  - ImageCaptionPreservation：评估图像→文字描述转换过程中丢失的空间/步骤上下文

兼容性：
  - 与 memory_bench.py 兼容，扩展其评测维度

Reference:
  - WorldMemArena: Multi-Session Multimodal Memory Evaluation Benchmark (2026)
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ──────────────────────────────────────────────────

class Modality(Enum):
    """模态类型。"""
    TEXT = "text"
    IMAGE = "image"
    TEXT_IMAGE = "text_image"  # 图文混合


class SessionTransition(Enum):
    """会话转换类型。"""
    SAME_SESSION = "same_session"
    NEW_SESSION = "new_session"
    LONG_GAP = "long_gap"  # 长时间间隔 (> 24h)


class SpatialContextLevel(Enum):
    """空间上下文保留等级。"""
    FULL = "full"            # 完整空间信息保留
    PARTIAL = "partial"      # 部分保留
    DEGRADED = "degraded"    # 严重退化
    LOST = "lost"            # 完全丢失


class StepContextLevel(Enum):
    """步骤上下文保留等级。"""
    FULL = "full"
    ORDER_ONLY = "order_only"  # 仅保留顺序
    FRAGMENTED = "fragmented"
    LOST = "lost"


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class MultimodalSample:
    """多模态评测样本。"""
    sample_id: str
    text_query: str
    image_paths: List[str] = field(default_factory=list)
    ground_truth: str = ""
    modality: Modality = Modality.TEXT
    session_id: str = "default"
    task_type: str = "qa"  # qa / planning / execution


@dataclass
class SessionRecord:
    """会话记录。"""
    session_id: str
    start_time: float
    end_time: float
    samples: List[MultimodalSample] = field(default_factory=list)
    gap_from_previous_hours: float = 0.0  # 距上一会话间隔


@dataclass
class UtilityVsRecallResult:
    """可用性 vs 召回率 对比结果。"""
    raw_recall: float = 0.0         # 原始召回覆盖率
    utility: float = 0.0             # 决策有用率
    precision: float = 0.0           # 精确率
    f1_utility_adjusted: float = 0.0  # 经可用性调整的 F1
    blind_spot_ratio: float = 0.0    # 盲区比例（高召回低质量）

    def to_dict(self) -> Dict[str, float]:
        return {
            "raw_recall": round(self.raw_recall, 4),
            "utility": round(self.utility, 4),
            "precision": round(self.precision, 4),
            "f1_utility_adjusted": round(self.f1_utility_adjusted, 4),
            "blind_spot_ratio": round(self.blind_spot_ratio, 4),
        }


@dataclass
class SessionBoundaryStats:
    """跨会话边界统计。"""
    same_session_accuracy: float = 0.0
    new_session_accuracy: float = 0.0
    long_gap_accuracy: float = 0.0
    accuracy_drop_rate: float = 0.0  # 新会话精度下降率
    retention_half_life_hours: float = 0.0  # 记忆半衰期（小时）

    def to_dict(self) -> Dict[str, float]:
        return {
            "same_session_accuracy": round(self.same_session_accuracy, 4),
            "new_session_accuracy": round(self.new_session_accuracy, 4),
            "long_gap_accuracy": round(self.long_gap_accuracy, 4),
            "accuracy_drop_rate": round(self.accuracy_drop_rate, 4),
            "retention_half_life_hours": round(self.retention_half_life_hours, 2),
        }


@dataclass
class CaptionPreservationScore:
    """图像→文字转换保真度得分。"""
    spatial_context_retention: float = 0.0    # 空间上下文保留
    step_context_retention: float = 0.0        # 步骤上下文保留
    object_count_preservation: float = 0.0     # 对象数量准确性
    relation_preservation: float = 0.0         # 关系保留度
    composite_score: float = 0.0               # 综合分数

    def to_dict(self) -> Dict[str, float]:
        return {
            "spatial_context_retention": round(self.spatial_context_retention, 4),
            "step_context_retention": round(self.step_context_retention, 4),
            "object_count_preservation": round(self.object_count_preservation, 4),
            "relation_preservation": round(self.relation_preservation, 4),
            "composite_score": round(self.composite_score, 4),
        }


# ── 核心类 ─────────────────────────────────────────────────────────

class UtilityVsRecallMetric:
    """检索可用性 vs 召回覆盖率 双轨指标

    解决"高召回低质量"的评测盲区：
      - raw_recall: 传统召回率（命中数 / 应召回数）
      - utility: 实际决策有用率（被下游任务有效利用的比例）
      - blind_spot_ratio: 盲区比例 = (高召回 & 低有用) 的比例
    """

    def __init__(self, utility_threshold: float = 0.5):
        self._lock = threading.RLock()
        self._utility_threshold = utility_threshold
        self._results: List[UtilityVsRecallResult] = []

    def compute(
        self,
        retrieved_items: List[Any],
        ground_truth: List[Any],
        utility_scores: List[float],
    ) -> UtilityVsRecallResult:
        """计算可用性-召回率双轨指标。

        Args:
            retrieved_items: 检索返回的项目
            ground_truth: 应召回的标准答案项目
            utility_scores: 每个检索项的实际有用度评分 (0~1)
        """
        with self._lock:
            # 原始召回率
            gt_set = set(str(g) for g in ground_truth)
            ret_set = set(str(r) for r in retrieved_items)
            hits = gt_set & ret_set
            raw_recall = len(hits) / max(len(gt_set), 1)

            # 精确率
            precision = len(hits) / max(len(ret_set), 1)

            # 可用性：被有效利用的检索项比例
            useful_count = sum(1 for s in utility_scores if s >= self._utility_threshold)
            utility = useful_count / max(len(retrieved_items), 1)

            # 经可用性调整的 F1
            if (raw_recall + utility) > 0:
                f1_adj = 2 * raw_recall * utility / (raw_recall + utility)
            else:
                f1_adj = 0.0

            # 盲区比例：高召回但低质量的样本
            blind_spot = raw_recall * (1.0 - utility)

            result = UtilityVsRecallResult(
                raw_recall=raw_recall,
                utility=utility,
                precision=precision,
                f1_utility_adjusted=f1_adj,
                blind_spot_ratio=blind_spot,
            )
            self._results.append(result)
            return result

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            if self._results:
                avg = UtilityVsRecallResult(
                    raw_recall=float(np.mean([r.raw_recall for r in self._results])),
                    utility=float(np.mean([r.utility for r in self._results])),
                    precision=float(np.mean([r.precision for r in self._results])),
                    f1_utility_adjusted=float(np.mean([r.f1_utility_adjusted for r in self._results])),
                    blind_spot_ratio=float(np.mean([r.blind_spot_ratio for r in self._results])),
                )
            else:
                avg = UtilityVsRecallResult()
            return {
                "total_evaluations": len(self._results),
                "average_metrics": avg.to_dict(),
            }


class SessionBoundaryDetector:
    """跨会话记忆衰减检测器

    核心指标：
      - same_session_accuracy: 同一会话内准确率
      - new_session_accuracy: 新会话准确率
      - accuracy_drop_rate: 精度下降率 = (同会话 - 新会话) / 同会话
      - retention_half_life: 记忆半衰期（精度减半所需时间）
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._session_stats: Dict[str, List[float]] = defaultdict(list)
        self._boundary_stats: List[SessionBoundaryStats] = []

    def record_score(
        self,
        session_id: str,
        transition: SessionTransition,
        accuracy: float,
    ) -> None:
        """记录单次评测得分。"""
        with self._lock:
            key = f"{session_id}:{transition.value}"
            self._session_stats[key].append(accuracy)

    def compute_boundary_stats(self) -> SessionBoundaryStats:
        """计算跨会话边界统计。"""
        with self._lock:
            same = []
            new = []
            long_gap = []

            for key, scores in self._session_stats.items():
                sid, trans = key.split(":", 1)
                if trans == SessionTransition.SAME_SESSION.value:
                    same.extend(scores)
                elif trans == SessionTransition.NEW_SESSION.value:
                    new.extend(scores)
                elif trans == SessionTransition.LONG_GAP.value:
                    long_gap.extend(scores)

            same_acc = float(np.mean(same)) if same else 0.0
            new_acc = float(np.mean(new)) if new else 0.0
            long_acc = float(np.mean(long_gap)) if long_gap else 0.0

            # 精度下降率
            drop_rate = (same_acc - new_acc) / max(same_acc, 0.001) if same_acc > 0 else 0.0

            # 记忆半衰期：假设指数衰减
            if new_acc > 0 and same_acc > 0 and drop_rate > 0:
                decay_constant = -np.log(max(new_acc / same_acc, 0.01)) / 24.0  # 假设 24h
                half_life = np.log(2) / max(decay_constant, 1e-6)
            else:
                half_life = 0.0

            stats = SessionBoundaryStats(
                same_session_accuracy=same_acc,
                new_session_accuracy=new_acc,
                long_gap_accuracy=long_acc,
                accuracy_drop_rate=drop_rate,
                retention_half_life_hours=half_life,
            )
            self._boundary_stats.append(stats)
            return stats

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            stats = self.compute_boundary_stats()
            return {
                "tracked_sessions": len(self._session_stats),
                "latest_boundary_stats": stats.to_dict(),
            }


class AgenticExecutionDegradation:
    """代理主动执行时的记忆质量退化测量

    对标 WorldMemArena 指标：
      - 被动 QA-C 基准：~55% 准确率
      - 主动执行后：出现显著退化
      - 测量退化幅度并分析退化模式
    """

    PASSIVE_BASELINE = 0.55  # QA-C 基准

    def __init__(self):
        self._lock = threading.RLock()
        self._passive_scores: List[float] = []
        self._agentic_scores: List[float] = []
        self._paired_results: List[Dict[str, float]] = []

    def record_passive(self, score: float) -> None:
        """记录被动问答得分。"""
        with self._lock:
            self._passive_scores.append(score)

    def record_agentic(self, score: float) -> None:
        """记录主动执行得分。"""
        with self._lock:
            self._agentic_scores.append(score)

    def record_pair(self, passive_score: float, agentic_score: float) -> None:
        """记录配对得分（同一任务被动 vs 主动）。"""
        with self._lock:
            self._paired_results.append({
                "passive": passive_score,
                "agentic": agentic_score,
                "degradation": passive_score - agentic_score,
            })

    def compute_degradation(self) -> Dict[str, Any]:
        """计算退化幅度与模式。"""
        with self._lock:
            passive_avg = float(np.mean(self._passive_scores)) if self._passive_scores else self.PASSIVE_BASELINE
            agentic_avg = float(np.mean(self._agentic_scores)) if self._agentic_scores else 0.0

            degradation = passive_avg - agentic_avg
            degradation_pct = (degradation / max(passive_avg, 0.001)) * 100

            # 配对分析
            paired_degradations = []
            if self._paired_results:
                paired_degradations = [p["degradation"] for p in self._paired_results]

            return {
                "passive_baseline": round(passive_avg, 4),
                "agentic_score": round(agentic_avg, 4),
                "absolute_degradation": round(degradation, 4),
                "degradation_percent": round(degradation_pct, 2),
                "paired_samples": len(self._paired_results),
                "avg_paired_degradation": round(float(np.mean(paired_degradations)), 4) if paired_degradations else 0.0,
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        return self.compute_degradation()


class ImageCaptionPreservation:
    """图像→文字描述转换保真度评估器

    评估维度：
      - 空间上下文保留：图片中的位置/朝向/距离信息是否被保留
      - 步骤上下文保留：序列图中的步骤顺序是否保留
      - 对象数量准确性：描述中提到的对象数与实际是否一致
      - 关系保留度：对象间关系（包含/因果/先后）是否保留
    """

    WEIGHTS = {
        "spatial": 0.30,
        "step": 0.25,
        "object_count": 0.25,
        "relation": 0.20,
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._scores: List[CaptionPreservationScore] = []

    def evaluate(
        self,
        original_description: str,
        caption_text: str,
        spatial_keywords: Optional[List[str]] = None,
        step_keywords: Optional[List[str]] = None,
        expected_object_count: int = 0,
        relation_keywords: Optional[List[str]] = None,
    ) -> CaptionPreservationScore:
        """评估图像描述的信息保真度。

        Args:
            original_description: 原始图像描述（或图像多模态理解的 full context）
            caption_text: 转换后的纯文本描述
            spatial_keywords: 空间关键词列表
            step_keywords: 步骤关键词列表
            expected_object_count: 预期对象数
            relation_keywords: 关系关键词列表
        """
        with self._lock:
            # 空间上下文保留
            spatial_score = 1.0
            if spatial_keywords:
                orig_lower = original_description.lower()
                caption_lower = caption_text.lower()
                preserved = sum(
                    1 for kw in spatial_keywords
                    if kw.lower() in orig_lower and kw.lower() in caption_lower
                )
                spatial_score = preserved / max(len(spatial_keywords), 1)

            # 步骤上下文保留
            step_score = 1.0
            if step_keywords:
                preserved = sum(
                    1 for kw in step_keywords
                    if kw.lower() in caption_text.lower()
                )
                step_score = preserved / max(len(step_keywords), 1)

            # 对象数量准确性
            object_score = 1.0
            if expected_object_count > 0:
                # 简单启发式：数数字/名词
                import re
                numbers = re.findall(r'\b(\d+)\b', caption_text)
                object_score = min(
                    len(set(numbers)) / max(expected_object_count, 1), 1.0
                )

            # 关系保留度
            relation_score = 1.0
            if relation_keywords:
                preserved = sum(
                    1 for kw in relation_keywords
                    if kw.lower() in caption_text.lower()
                )
                relation_score = preserved / max(len(relation_keywords), 1)

            composite = (
                self.WEIGHTS["spatial"] * spatial_score
                + self.WEIGHTS["step"] * step_score
                + self.WEIGHTS["object_count"] * object_score
                + self.WEIGHTS["relation"] * relation_score
            )

            score = CaptionPreservationScore(
                spatial_context_retention=spatial_score,
                step_context_retention=step_score,
                object_count_preservation=object_score,
                relation_preservation=relation_score,
                composite_score=composite,
            )
            self._scores.append(score)
            return score

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            if self._scores:
                avg = CaptionPreservationScore(
                    spatial_context_retention=float(np.mean([s.spatial_context_retention for s in self._scores])),
                    step_context_retention=float(np.mean([s.step_context_retention for s in self._scores])),
                    object_count_preservation=float(np.mean([s.object_count_preservation for s in self._scores])),
                    relation_preservation=float(np.mean([s.relation_preservation for s in self._scores])),
                    composite_score=float(np.mean([s.composite_score for s in self._scores])),
                )
            else:
                avg = CaptionPreservationScore()
            return {
                "evaluations": len(self._scores),
                "average_scores": avg.to_dict(),
            }


class MultimodalEvaluator:
    """多模态记忆端到端评测器

    整合：
      - 文本 + 图像双模态评测
      - 可用性 vs 召回率双轨
      - 跨会话衰减检测
      - 主动执行退化测量
      - 图像描述保真度评估
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._utility_recall = UtilityVsRecallMetric()
        self._session_detector = SessionBoundaryDetector()
        self._degradation = AgenticExecutionDegradation()
        self._caption_preservation = ImageCaptionPreservation()
        self._evaluation_log: List[Dict[str, Any]] = []

    def evaluate_text(self, samples: List[MultimodalSample]) -> Dict[str, Any]:
        """评测纯文本记忆。"""
        with self._lock:
            results: Dict[str, Any] = {"modality": "text", "sample_count": len(samples)}
            return results

    def evaluate_image_memory(
        self, samples: List[MultimodalSample], captions: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """评测图像记忆。"""
        with self._lock:
            image_samples = [s for s in samples if s.modality in (Modality.IMAGE, Modality.TEXT_IMAGE)]
            results: Dict[str, Any] = {
                "modality": "image",
                "image_sample_count": len(image_samples),
            }
            if captions:
                for i, (sample, caption) in enumerate(zip(image_samples, captions)):
                    score = self._caption_preservation.evaluate(
                        original_description=sample.ground_truth,
                        caption_text=caption,
                    )
                    self._evaluation_log.append({
                        "sample_id": sample.sample_id,
                        "caption_preservation": score.to_dict(),
                    })
                results["caption_preservation"] = self._caption_preservation.statistics()
            return results

    def evaluate_cross_session(
        self,
        session_records: List[SessionRecord],
        accuracies: List[float],
        transitions: List[SessionTransition],
    ) -> SessionBoundaryStats:
        """评测跨会话记忆衰减。"""
        with self._lock:
            for record, acc, trans in zip(session_records, accuracies, transitions):
                self._session_detector.record_score(
                    record.session_id, trans, acc
                )
            return self._session_detector.compute_boundary_stats()

    def evaluate_agentic_degradation(
        self,
        passive_scores: List[float],
        agentic_scores: List[float],
    ) -> Dict[str, Any]:
        """评测主动执行退化。"""
        with self._lock:
            for ps in passive_scores:
                self._degradation.record_passive(ps)
            for ag in agentic_scores:
                self._degradation.record_agentic(ag)
            for ps, ag in zip(passive_scores, agentic_scores):
                self._degradation.record_pair(ps, ag)
            return self._degradation.compute_degradation()

    def full_evaluation_report(self) -> Dict[str, Any]:
        """生成完整评测报告。"""
        with self._lock:
            return {
                "utility_vs_recall": self._utility_recall.statistics(),
                "session_boundary": self._session_detector.statistics(),
                "agentic_degradation": self._degradation.compute_degradation(),
                "caption_preservation": self._caption_preservation.statistics(),
                "total_evaluations": len(self._evaluation_log),
            }

    @property
    def utility_vs_recall(self) -> UtilityVsRecallMetric:
        return self._utility_recall

    @property
    def session_detector(self) -> SessionBoundaryDetector:
        return self._session_detector

    @property
    def degradation_measure(self) -> AgenticExecutionDegradation:
        return self._degradation

    @property
    def caption_preservation(self) -> ImageCaptionPreservation:
        return self._caption_preservation

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        return self.full_evaluation_report()
