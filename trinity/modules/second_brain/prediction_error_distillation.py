"""
# status: orphan (2026-08-15 audit, not in runtime path)
Nemori — Prediction-Error-Driven Adaptive Memory Distillation
==============================================================
ACL 2026 · P37-2

三元语: 预测误差驱动的自适应记忆蒸馏——
用"可预测性"判断什么值得记住 (越难预测越重要),
将原始交互转换为连贯叙事, 通过预测误差提取洞察,
蒸馏门控控制保留/丢弃决策。

设计要点:
  - PredictionErrorDistillationEngine: 核心蒸馏引擎,
    基于预测误差门控决定记忆保留/丢弃/压缩。
  - EpisodicMemoryIntegrator: 将碎片化交互编织为连贯叙事,
    维护时间线连续性与因果一致性。
  - SemanticKnowledgeExtractor: 从预测误差模式中提取
    高阶语义洞察 (概念漂移、模式涌现、异常来源)。
  - DistillationGate: 门控单元, 综合预测误差、新颖性、
    情感显著性三维度决策。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class NemoriGateDecision(Enum):
    """蒸馏门控决策。"""
    RETAIN_FULL = auto()           # 完整保留
    COMPRESS = auto()              # 压缩后保留
    DISCARD = auto()               # 丢弃
    DEFER = auto()                 # 暂缓 (交给下游)


class PredictionErrorMetric(Enum):
    """预测误差度量维度。"""
    PERPLEXITY = auto()            # 语言模型困惑度
    SURPRISE = auto()              # 信息论惊奇度
    NOVELTY = auto()               # 新颖性 (与已有记忆的距离)
    DIVERGENCE = auto()            # 分布偏移 (KL 散度)
    ANOMALY = auto()               # 异常分数


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class NemoriDistillationRecord:
    """单条蒸馏记录。"""
    record_id: str
    raw_interaction: str
    prediction_error: float        # 预测误差值 [0, ∞)
    novelty_score: float           # 新颖性 [0, 1]
    salience_score: float          # 显著性 [0, 1]
    gate_decision: NemoriGateDecision
    compressed_content: str = ""
    timestamp: float = field(default_factory=time.time)
    retained: bool = False


@dataclass
class NarrativeSegment:
    """叙事片段 (EpisodicMemoryIntegrator 产出)。"""
    segment_id: str
    start_timestamp: float
    end_timestamp: float
    events: List[NemoriDistillationRecord]
    narrative_text: str
    causal_chain: List[str]        # "event_A → event_B → event_C"
    coherence_score: float = 1.0


@dataclass
class SemanticInsight:
    """语义洞察 (SemanticKnowledgeExtractor 产出)。"""
    insight_id: str
    insight_type: str              # concept_drift, pattern_emergence, anomaly_source, etc.
    description: str
    supporting_records: List[str]
    confidence: float
    extracted_at: float = field(default_factory=time.time)


# ============================================================================
# Core Class 1: DistillationGate
# ============================================================================

class DistillationGate:
    """蒸馏门控。

    综合三维度 (预测误差 + 新颖性 + 显著性) 进行保留/丢弃决策。

    Parameters
    ----------
    error_threshold : float
        预测误差阈值, 高于此值倾向保留。
    novelty_weight : float
        新颖性权重。
    salience_weight : float
        显著性权重。
    """

    def __init__(
        self,
        error_threshold: float = 0.5,
        novelty_weight: float = 0.3,
        salience_weight: float = 0.2,
    ) -> None:
        self.error_threshold = error_threshold
        self.novelty_weight = novelty_weight
        self.salience_weight = salience_weight
        self._lock = threading.RLock()
        self._decisions_made: int = 0
        logger.info("DistillationGate initialized [thresh=%.2f nw=%.2f sw=%.2f]",
                    error_threshold, novelty_weight, salience_weight)

    def decide(
        self,
        prediction_error: float,
        novelty_score: float,
        salience_score: float,
    ) -> Tuple[NemoriGateDecision, float]:
        """综合打分决策。

        Parameters
        ----------
        prediction_error : float
            预测误差值。
        novelty_score : float
            新颖性得分 [0,1]。
        salience_score : float
            显著性得分 [0,1]。

        Returns
        -------
        Tuple[NemoriGateDecision, float]
            (决策, 综合得分)。
        """
        with self._lock:
            # 归一化预测误差
            norm_error = 2.0 * (1.0 / (1.0 + np.exp(-prediction_error * 2.0)) - 0.5)
            norm_error = float(np.clip(norm_error, 0.0, 1.0))

            combined = (
                norm_error * (1.0 - self.novelty_weight - self.salience_weight)
                + novelty_score * self.novelty_weight
                + salience_score * self.salience_weight
            )

            self._decisions_made += 1

            if combined >= self.error_threshold + 0.2:
                decision = NemoriGateDecision.RETAIN_FULL
            elif combined >= self.error_threshold:
                decision = NemoriGateDecision.COMPRESS
            elif combined >= self.error_threshold - 0.3:
                decision = NemoriGateDecision.DEFER
            else:
                decision = NemoriGateDecision.DISCARD

            return decision, combined

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"decisions_made": self._decisions_made, "error_threshold": self.error_threshold}


# ============================================================================
# Core Class 2: PredictionErrorDistillationEngine
# ============================================================================

class PredictionErrorDistillationEngine:
    """预测误差驱动的自适应记忆蒸馏引擎。

    管道: 计算预测误差 → 门控决策 → 保留/压缩/丢弃。

    Parameters
    ----------
    gate : DistillationGate
        蒸馏门控。
    embedding_dim : int
        嵌入维度。
    history_window : int
        滑动窗口大小 (条数)。
    """

    def __init__(
        self,
        gate: Optional[DistillationGate] = None,
        embedding_dim: int = 256,
        history_window: int = 32,
    ) -> None:
        self.gate = gate or DistillationGate()
        self.embedding_dim = embedding_dim
        self.history_window = history_window

        self._records: List[NemoriDistillationRecord] = []
        self._history_embeddings: List[np.ndarray] = []
        self._lock = threading.RLock()
        self._counter: int = 0

        logger.info(
            "PredictionErrorDistillationEngine initialized [dim=%d window=%d]",
            embedding_dim, history_window,
        )

    def process(
        self,
        interaction_text: str,
        ground_truth: Optional[str] = None,
    ) -> NemoriDistillationRecord:
        """处理单条交互, 蒸馏决策。

        Parameters
        ----------
        interaction_text : str
            原始交互文本。
        ground_truth : Optional[str]
            期望输出 (用于计算预测误差)。

        Returns
        -------
        NemoriDistillationRecord
            蒸馏记录。
        """
        with self._lock:
            self._counter += 1
            # 计算当前嵌入
            current_emb = self._make_embedding(interaction_text)

            # 预测误差: 与历史平均嵌入的偏差
            prediction_error = self._compute_prediction_error(
                current_emb, ground_truth,
            )

            # 新颖性: 与历史的平均相似度求逆
            novelty = self._compute_novelty(current_emb)

            # 显著性: 文本中是否有关键信号词
            salience = self._compute_salience(interaction_text)

            # 门控决策
            decision, score = self.gate.decide(prediction_error, novelty, salience)

            compressed = ""
            if decision == NemoriGateDecision.COMPRESS:
                compressed = self._compress(interaction_text)

            record = NemoriDistillationRecord(
                record_id=f"rec_{self._counter}_{int(time.time()*1e6)}",
                raw_interaction=interaction_text,
                prediction_error=prediction_error,
                novelty_score=novelty,
                salience_score=salience,
                gate_decision=decision,
                compressed_content=compressed,
                retained=(decision != NemoriGateDecision.DISCARD),
            )

            if decision != NemoriGateDecision.DISCARD:
                self._records.append(record)

            self._history_embeddings.append(current_emb)
            # 滑动窗口
            if len(self._history_embeddings) > self.history_window:
                self._history_embeddings.pop(0)

            return record

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_processed": self._counter,
                "retained": len(self._records),
                "history_window": len(self._history_embeddings),
                "gate": self.gate.statistics(),
            }

    # ------------------------------------------------------------------
    def _compute_prediction_error(
        self,
        current_emb: np.ndarray,
        ground_truth: Optional[str],
    ) -> float:
        if ground_truth:
            gt_emb = self._make_embedding(ground_truth)
            return float(1.0 - np.dot(current_emb, gt_emb) /
                         (np.linalg.norm(current_emb) * np.linalg.norm(gt_emb) + 1e-8))
        # 无 ground truth 时用历史均值偏差
        if self._history_embeddings:
            avg = np.mean(self._history_embeddings, axis=0)
            return float(np.linalg.norm(current_emb - avg) / max(len(self._history_embeddings), 1))
        return 1.0

    def _compute_novelty(self, current_emb: np.ndarray) -> float:
        if not self._history_embeddings:
            return 0.8
        sims = [
            float(np.dot(current_emb, h) / (np.linalg.norm(current_emb) * np.linalg.norm(h) + 1e-8))
            for h in self._history_embeddings[-16:]
        ]
        avg_sim = np.mean(sims) if sims else 1.0
        return float(np.clip(1.0 - avg_sim, 0.0, 1.0))

    def _compute_salience(self, text: str) -> float:
        salience_keywords = [
            "urgent", "紧急", "must", "必须", "critical", "关键",
            "deadline", "截止", "important", "重要", "alert", "警告",
        ]
        lower = text.lower()
        hits = sum(1 for kw in salience_keywords if kw in lower)
        return float(np.clip(hits / 3.0, 0.0, 1.0))

    def _compress(self, text: str) -> str:
        if len(text) <= 80:
            return text
        return text[:40] + " ... " + text[-40:]

    def _make_embedding(self, text: str) -> np.ndarray:
        import hashlib
        seed = int(hashlib.sha256(text.encode()).hexdigest()[:16], 16)
        rng = np.random.RandomState(seed % (2 ** 31 - 1))
        vec = rng.randn(self.embedding_dim)
        return vec / (np.linalg.norm(vec) + 1e-8)


# ============================================================================
# Core Class 3: EpisodicMemoryIntegrator
# ============================================================================

class EpisodicMemoryIntegrator:
    """情景记忆整合器。

    将碎片化的蒸馏记录编织为连贯叙事,
    维护时间线连续性与因果一致性。

    Parameters
    ----------
    max_narrative_length : int
        单条叙事最大事件数。
    coherence_threshold : float
        连贯性阈值 (低于此值拆分为新叙事)。
    """

    def __init__(
        self,
        max_narrative_length: int = 64,
        coherence_threshold: float = 0.3,
    ) -> None:
        self.max_narrative_length = max_narrative_length
        self.coherence_threshold = coherence_threshold
        self._narratives: List[NarrativeSegment] = []
        self._lock = threading.RLock()
        self._seg_counter: int = 0
        logger.info("EpisodicMemoryIntegrator initialized [max_len=%d coh_th=%.2f]",
                    max_narrative_length, coherence_threshold)

    def integrate(
        self,
        records: List[NemoriDistillationRecord],
    ) -> Optional[NarrativeSegment]:
        """将多条蒸馏记录整合为叙事片段。

        Parameters
        ----------
        records : List[NemoriDistillationRecord]
            待整合的蒸馏记录。

        Returns
        -------
        Optional[NarrativeSegment]
            生成的叙事片段。
        """
        with self._lock:
            if not records:
                return None

            # 按时间排序
            sorted_records = sorted(records, key=lambda r: r.timestamp)

            # 生成因果链
            causal_chain: List[str] = []
            for i in range(len(sorted_records) - 1):
                causal_chain.append(
                    f"{sorted_records[i].record_id} → {sorted_records[i+1].record_id}"
                )

            # 生成叙事文本
            narrative_parts = []
            for r in sorted_records:
                if r.retained:
                    narrative_parts.append(
                        f"[{r.prediction_error:.2f}] {r.raw_interaction[:60]}"
                    )
            narrative_text = " | ".join(narrative_parts)

            # 计算连贯性
            coherence = self._compute_coherence(sorted_records)
            if coherence < self.coherence_threshold and len(self._narratives) > 0:
                logger.debug("Low coherence %.3f, splitting narrative", coherence)

            self._seg_counter += 1
            segment = NarrativeSegment(
                segment_id=f"seg_{self._seg_counter}",
                start_timestamp=sorted_records[0].timestamp,
                end_timestamp=sorted_records[-1].timestamp,
                events=sorted_records,
                narrative_text=narrative_text,
                causal_chain=causal_chain,
                coherence_score=coherence,
            )
            self._narratives.append(segment)
            return segment

    def _compute_coherence(self, records: List[NemoriDistillationRecord]) -> float:
        if len(records) < 2:
            return 0.9
        retained = [r for r in records if r.retained]
        if len(retained) < 2:
            return 0.5
        errors = np.array([r.prediction_error for r in retained])
        # 误差波动越小越连贯
        std = float(np.std(errors))
        return float(np.clip(1.0 - std, 0.1, 1.0))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"narratives_count": len(self._narratives), "last_coherence": float(self._narratives[-1].coherence_score) if self._narratives else 0.0}


# ============================================================================
# Core Class 4: SemanticKnowledgeExtractor
# ============================================================================

class SemanticKnowledgeExtractor:
    """语义知识提取器。

    从预测误差模式中提取高阶语义洞察:
    - concept_drift: 概念漂移检测
    - pattern_emergence: 模式涌现识别
    - anomaly_source: 异常来源追踪

    Parameters
    ----------
    drift_threshold : float
        概念漂移检测阈值。
    pattern_min_support : int
        模式涌现最小支持度。
    """

    def __init__(
        self,
        drift_threshold: float = 0.4,
        pattern_min_support: int = 3,
    ) -> None:
        self.drift_threshold = drift_threshold
        self.pattern_min_support = pattern_min_support
        self._insights: List[SemanticInsight] = []
        self._lock = threading.RLock()
        self._insight_counter: int = 0
        logger.info("SemanticKnowledgeExtractor initialized [drift=%.2f min_sup=%d]",
                    drift_threshold, pattern_min_support)

    def extract(
        self,
        records: List[NemoriDistillationRecord],
    ) -> List[SemanticInsight]:
        """从蒸馏记录中提取语义洞察。

        Parameters
        ----------
        records : List[NemoriDistillationRecord]
            蒸馏记录列表。

        Returns
        -------
        List[SemanticInsight]
            提取的洞察列表。
        """
        with self._lock:
            insights: List[SemanticInsight] = []
            if not records:
                return insights

            # concept_drift: 预测误差均值随时间的变化
            errors = [r.prediction_error for r in records]
            if len(errors) >= 4:
                first_half = np.mean(errors[:len(errors) // 2])
                second_half = np.mean(errors[len(errors) // 2:])
                drift = abs(second_half - first_half)
                if drift > self.drift_threshold:
                    self._insight_counter += 1
                    insights.append(SemanticInsight(
                        insight_id=f"ins_{self._insight_counter}",
                        insight_type="concept_drift",
                        description=f"Prediction error shifted by {drift:.3f} ({first_half:.2f}→{second_half:.2f})",
                        supporting_records=[r.record_id for r in records[-5:]],
                        confidence=min(drift / self.drift_threshold * 0.8, 0.95),
                    ))

            # pattern_emergence: 连续 RETAIN_FULL 记录块
            retain_runs: List[List[NemoriDistillationRecord]] = []
            current_run: List[NemoriDistillationRecord] = []
            for r in records:
                if r.gate_decision == NemoriGateDecision.RETAIN_FULL:
                    current_run.append(r)
                else:
                    if len(current_run) >= self.pattern_min_support:
                        retain_runs.append(current_run)
                    current_run = []
            if len(current_run) >= self.pattern_min_support:
                retain_runs.append(current_run)

            for run in retain_runs:
                self._insight_counter += 1
                insights.append(SemanticInsight(
                    insight_id=f"ins_{self._insight_counter}",
                    insight_type="pattern_emergence",
                    description=f"High-importance pattern of {len(run)} consecutive RETAIN events",
                    supporting_records=[r.record_id for r in run],
                    confidence=0.7 + min(len(run) * 0.05, 0.25),
                ))

            # anomaly_source: 孤立高误差记录
            for r in records:
                if r.prediction_error > self.drift_threshold * 1.5:
                    self._insight_counter += 1
                    insights.append(SemanticInsight(
                        insight_id=f"ins_{self._insight_counter}",
                        insight_type="anomaly_source",
                        description=f"High prediction error {r.prediction_error:.2f} for: {r.raw_interaction[:50]}",
                        supporting_records=[r.record_id],
                        confidence=0.6,
                    ))

            self._insights.extend(insights)
            return insights

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = {}
            for ins in self._insights:
                type_counts[ins.insight_type] = type_counts.get(ins.insight_type, 0) + 1
            return {"total_insights": len(self._insights), "by_type": type_counts}
