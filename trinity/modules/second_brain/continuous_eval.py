# continuous_eval — CB55: Continuous Evaluation Pipeline
# 对标 RAGAS 框架，在线持续评测检索质量
#
# 当前问题:
#   Trinity 有 BEAMLIGHT 离线评测，但缺少生产级线上自动评测能力。
#   无法在每次检索后自动检测 faithfulness / answer_relevancy /
# status: frozen (2026-09 EXECUTION 163)
#   context_precision / context_recall 并做回归告警。
#
# 对标:
#   - RAGAS: Automated Evaluation of RAG Systems (arXiv:2309.15217)
#   - BEAMLIGHT 10 维度评测体系 (engine_retrieval.py)
#
# 设计要点:
#   1. RagasMetrics — 四指标 + 综合评分 + 时间戳
#   2. ContinuousEvalEngine — 单次评测 + 管线钩子 + 滚动报告 + 异常告警
#   3. EvalResultStore — 环形缓冲区 + Markdown 报告 + 维度分组
#   4. 检索管线集成 — search() 后自动 evaluate()
#   5. 启发式近似计算 — 不依赖完整 LLM 调用，基于统计+NER+相似度

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# 配置常量
# =============================================================================

CONTINUOUS_EVAL_ENABLED: bool = True
"""是否启用持续评测。False 时 evaluate() 直接返回 None。"""

CONTINUOUS_EVAL_WINDOW: int = 100
"""滑动窗口大小，rolling_report() 统计最近 N 条结果。"""

CONTINUOUS_EVAL_ALERT_THRESHOLD: float = 0.40
"""alert 阈值。连续 N 轮的 overall_score 均低于此值时触发告警。"""

CONTINUOUS_EVAL_ALERT_CONSECUTIVE: int = 10
"""触发告警所需连续低于阈值的轮数。"""

CONTINUOUS_EVAL_BUFFER_SIZE: int = 1000
"""EvalResultStore 环形缓冲区容量。"""

CONTINUOUS_EVAL_FAITHFULNESS_SIM_THRESHOLD: float = 0.30
"""faithfulness 中句子级相似度阈值（token Jaccard），>= 阈值视为可映射到上下文。"""

CONTINUOUS_EVAL_RELEVANCY_STOP_WORDS_EN: set = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "of", "in", "to", "for", "on", "with", "at", "by", "from", "as",
    "and", "or", "but", "not", "this", "that", "it", "its",
}


# =============================================================================
# BEAMLIGHT 10 维度定义
# =============================================================================

BEAMLIGHT_DIMENSIONS = OrderedDict([
    ("retrieval_accuracy", "检索精度"),
    ("context_fidelity", "上下文保真"),
    ("answer_coherence", "回答连贯性"),
    ("latency_efficiency", "延迟效率"),
    ("coverage_recall", "覆盖召回"),
    ("robustness_noise", "抗噪鲁棒性"),
    ("multimodal_alignment", "多模态对齐"),
    ("cross_session_consistency", "跨会话一致性"),
    ("knowledge_freshness", "知识新鲜度"),
    ("safety_compliance", "安全合规"),
])


# =============================================================================
# 数据类
# =============================================================================

@dataclass
class RagasMetrics:
    """
    RAGAS 四指标快照。

    对标 RAGAS Result 结构:
      - faithfulness:    答案能从上下文推导的程度
      - answer_relevancy: 答案与问题的语义相关度
      - context_precision: 检索结果中相关上下文的排位质量
      - context_recall:   答案覆盖上下文中关键实体的比例
    """
    faithfulness: float = 0.0
    """答案语句可映射到上下文的占比 (0-1)"""
    answer_relevancy: float = 0.0
    """答案与问题的语义相似度 (0-1)"""
    context_precision: float = 0.0
    """相关上下文的 MRR (0-1)"""
    context_recall: float = 0.0
    """答案覆盖上下文关键实体的比例 (0-1)"""
    overall_score: float = 0.0
    """综合评分 (四指标加权平均)"""
    timestamp: float = 0.0
    """评测时间戳"""
    query_id: str = ""
    """检索唯一标识"""

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()
        if self.overall_score == 0.0:
            self.overall_score = self._compute_overall()

    def _compute_overall(self) -> float:
        """加权综合: faithfulness 40%, relevancy 25%, precision 20%, recall 15%"""
        return round(
            0.40 * self.faithfulness +
            0.25 * self.answer_relevancy +
            0.20 * self.context_precision +
            0.15 * self.context_recall, 4
        )

    def to_dict(self) -> dict:
        return {
            "faithfulness": self.faithfulness,
            "answer_relevancy": self.answer_relevancy,
            "context_precision": self.context_precision,
            "context_recall": self.context_recall,
            "overall_score": self.overall_score,
            "timestamp": self.timestamp,
            "query_id": self.query_id,
        }


class AlertLevel(str, Enum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class EvalAlert:
    """评测告警"""
    level: AlertLevel = AlertLevel.NORMAL
    message: str = ""
    consecutive_below_threshold: int = 0
    current_overall: float = 0.0
    threshold: float = CONTINUOUS_EVAL_ALERT_THRESHOLD
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "level": self.level.value,
            "message": self.message,
            "consecutive_below_threshold": self.consecutive_below_threshold,
            "current_overall": self.current_overall,
            "threshold": self.threshold,
            "timestamp": self.timestamp,
        }


# =============================================================================
# 工具函数
# =============================================================================

def _tokenize(text: str, lowercase: bool = True) -> list[str]:
    """简单分词: 按非字母数字拆分，过滤短 token 和停用词。"""
    if not text:
        return []
    tokens = re.findall(r'[a-zA-Z\u4e00-\u9fff0-9]+', text.lower() if lowercase else text)
    return [t for t in tokens if len(t) > 1 and t not in CONTINUOUS_EVAL_RELEVANCY_STOP_WORDS_EN]


def _split_sentences(text: str) -> list[str]:
    """按句号/问号/感叹号/换行拆分句子。"""
    if not text:
        return []
    raw = re.split(r'[.!?。！？\n]+', text)
    return [s.strip() for s in raw if s.strip()]


def _jaccard_similarity(tokens_a: list[str], tokens_b: list[str]) -> float:
    """Jaccard 系数。"""
    if not tokens_a or not tokens_b:
        return 0.0
    set_a, set_b = set(tokens_a), set(tokens_b)
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union if union > 0 else 0.0


def _extract_entities(text: str) -> set[str]:
    """
    简单实体提取: 大写开头连续词、中文双字以上名词短语、数字+单位。
    对标 NER 启发式，不依赖外部模型。
    """
    entities: set[str] = set()

    # 英文大写开头连续词
    en_matches = re.findall(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\b', text)
    entities.update(e.lower() for e in en_matches)

    # 中文双字以上片段
    zh_matches = re.findall(r'[\u4e00-\u9fff]{2,}', text)
    entities.update(zh_matches)

    # 数字+单位
    num_unit = re.findall(r'\d+[kKmMbB%]?\w*', text)
    entities.update(n.lower() for n in num_unit if len(n) >= 2)

    return entities


def _cosine_token_overlap(query: str, answer: str) -> float:
    """基于 token 重叠的余弦近似（无 embedding 依赖）。"""
    q_tokens = _tokenize(query)
    a_tokens = _tokenize(answer)
    if not q_tokens or not a_tokens:
        return 0.0

    q_freq: dict[str, int] = defaultdict(int)
    a_freq: dict[str, int] = defaultdict(int)
    for t in q_tokens:
        q_freq[t] += 1
    for t in a_tokens:
        a_freq[t] += 1

    # Dot product
    dot = sum(q_freq[t] * a_freq.get(t, 0) for t in q_freq)
    norm_q = math.sqrt(sum(v * v for v in q_freq.values()))
    norm_a = math.sqrt(sum(v * v for v in a_freq.values()))

    if norm_q == 0 or norm_a == 0:
        return 0.0

    return dot / (norm_q * norm_a)


# =============================================================================
# ContinuousEvalEngine — 持续评测引擎
# =============================================================================

class ContinuousEvalEngine:
    """
    CB55: ContinuousEvalEngine — RAGAS 风格持续评测引擎。

    在每次检索后自动计算四个质量指标，支持滚动统计和异常告警。
    通过钩子集成到 TrinityRetrievalPipeline.search()。
    """

    def __init__(self,
                 enabled: bool = CONTINUOUS_EVAL_ENABLED,
                 window: int = CONTINUOUS_EVAL_WINDOW,
                 alert_threshold: float = CONTINUOUS_EVAL_ALERT_THRESHOLD,
                 alert_consecutive: int = CONTINUOUS_EVAL_ALERT_CONSECUTIVE,
                 buffer_size: int = CONTINUOUS_EVAL_BUFFER_SIZE):
        self.enabled = enabled
        self.window = window
        self.alert_threshold = alert_threshold
        self.alert_consecutive = alert_consecutive

        # 结果存储
        self.store = EvalResultStore(buffer_size)

        # 检索管线引用
        self.pipeline_ref: Any = None

        # 统计
        self._lock = threading.RLock()
        self.total_evaluations: int = 0
        self._below_threshold_streak: int = 0
        self._last_alert: Optional[EvalAlert] = None

        # 时序数据用于趋势分析
        self._recent_overalls: list[float] = []

    # ------------------------------------------------------------------
    # evaluate — 核心评测方法
    # ------------------------------------------------------------------

    def evaluate(self,
                 query: str,
                 answer: str,
                 contexts: list[str],
                 ground_truth: Optional[str] = None,
                 query_id: str = "",
                 dimension: str = "retrieval_accuracy") -> Optional[RagasMetrics]:
        """
        单次检索评测。

        Args:
            query: 用户查询
            answer: 检索+生成后的答案
            contexts: 检索返回的上下文列表
            ground_truth: 标注答案（可选，用于 precision/recall 增强）
            query_id: 检索唯一标识
            dimension: BEAMLIGHT 10 维度名称

        Returns:
            RagasMetrics 或 None（eval 禁用时）
        """
        if not self.enabled:
            return None

        with self._lock:
            self.total_evaluations += 1

            # 1. faithfulness
            faithfulness = self._compute_faithfulness(answer, contexts)

            # 2. answer_relevancy
            answer_relevancy = self._compute_answer_relevancy(query, answer)

            # 3. context_precision
            context_precision = self._compute_context_precision(query, answer, contexts)

            # 4. context_recall
            context_recall = self._compute_context_recall(answer, contexts, ground_truth)

            metrics = RagasMetrics(
                faithfulness=faithfulness,
                answer_relevancy=answer_relevancy,
                context_precision=context_precision,
                context_recall=context_recall,
                query_id=query_id,
            )

            # 存储
            dimension = dimension or "retrieval_accuracy"
            if dimension not in BEAMLIGHT_DIMENSIONS:
                dimension = "retrieval_accuracy"
            self.store.add(metrics, dimension)

            # 滑动窗口
            self._recent_overalls.append(metrics.overall_score)
            if len(self._recent_overalls) > self.window:
                self._recent_overalls.pop(0)

            # 异常检测
            self._check_alert(metrics.overall_score)

            return metrics

    # ------------------------------------------------------------------
    # 四指标计算
    # ------------------------------------------------------------------

    def _compute_faithfulness(self, answer: str,
                               contexts: list[str]) -> float:
        """
        faithfulness: 答案中能映射到上下文的语句占比。

        启发式: 将答案拆句，每句与拼接后的上下文做 token Jaccard，
        >= FAITHFULNESS_SIM_THRESHOLD 视为 faithful。
        """
        if not answer or not contexts:
            return 0.0

        sentences = _split_sentences(answer)
        if not sentences:
            return 0.0

        # 拼接上下文为一个 token 集合
        ctx_text = " ".join(contexts)
        ctx_tokens = set(_tokenize(ctx_text))

        faithful_count = 0
        for sent in sentences:
            sent_tokens = set(_tokenize(sent))
            if not sent_tokens:
                faithful_count += 1  # 空句视为 faithful
                continue
            overlap = len(sent_tokens & ctx_tokens)
            score = overlap / len(sent_tokens) if sent_tokens else 1.0
            if score >= CONTINUOUS_EVAL_FAITHFULNESS_SIM_THRESHOLD:
                faithful_count += 1

        return round(faithful_count / len(sentences), 4)

    def _compute_answer_relevancy(self, query: str,
                                   answer: str) -> float:
        """
        answer_relevancy: 答案与问题的语义相似度。

        启发式: token-level cosine + 实体重叠双通道加权。
        """
        if not query or not answer:
            return 0.0

        # 通道1: token cosine 相似度
        cosine_sim = _cosine_token_overlap(query, answer)

        # 通道2: 实体重叠
        q_entities = _extract_entities(query)
        a_entities = _extract_entities(answer)
        if q_entities:
            entity_overlap = len(q_entities & a_entities) / len(q_entities)
        else:
            entity_overlap = 0.0

        # 加权: cosine 60%, entity overlap 40%
        relevancy = 0.60 * cosine_sim + 0.40 * entity_overlap
        return round(min(1.0, max(0.0, relevancy)), 4)

    def _compute_context_precision(self, query: str,
                                    answer: str,
                                    contexts: list[str]) -> float:
        """
        context_precision: 相关上下文在检索结果中的排位质量 (MRR)。

        启发式: 对每个 context 计算与 answer 的 token Jaccard，
        取 MRR = 1/rank_of_first_relevant。
        """
        if not contexts:
            return 0.0

        answer_tokens = set(_tokenize(answer))
        if not answer_tokens:
            return 0.0

        for rank, ctx in enumerate(contexts, start=1):
            ctx_tokens = set(_tokenize(ctx))
            overlap = len(answer_tokens & ctx_tokens)
            score = overlap / len(answer_tokens) if answer_tokens else 0.0
            if score >= 0.20:  # 至少 20% token 匹配视为相关
                return round(1.0 / rank, 4)

        return 0.0

    def _compute_context_recall(self, answer: str,
                                 contexts: list[str],
                                 ground_truth: Optional[str] = None) -> float:
        """
        context_recall: 答案覆盖上下文中关键实体的比例。

        启发式: 上下文实体集合中，有多少比例出现在答案里。
        有 ground_truth 时，以 ground_truth 为参照点。
        """
        all_ctx_text = " ".join(contexts)
        ctx_entities = _extract_entities(all_ctx_text)

        if not ctx_entities:
            return 0.0

        if ground_truth:
            # 以 ground_truth 为参照，答案实体 vs 上下文实体的交集
            gt_entities = _extract_entities(ground_truth)
            answer_entities = _extract_entities(answer)
            if not gt_entities:
                return 0.0
            covered = len(gt_entities & answer_entities)
            return round(covered / len(gt_entities), 4)
        else:
            # 无 ground_truth: 答案实体覆盖上下文实体的比例
            answer_entities = _extract_entities(answer)
            covered = len(ctx_entities & answer_entities)
            return round(covered / len(ctx_entities), 4)

    # ------------------------------------------------------------------
    # 滚动报告与异常检测
    # ------------------------------------------------------------------

    def rolling_report(self) -> dict:
        """
        滑动窗口统计报告。

        Returns:
            包含 avg / min / max / p50 / p95 / p99 / trend of overall_score
        """
        with self._lock:
            if not self._recent_overalls:
                return {"error": "No data yet"}

            scores = sorted(self._recent_overalls)
            n = len(scores)
            p50_idx = int(n * 0.50)
            p95_idx = int(n * 0.95)
            p99_idx = int(n * 0.99)

            # 趋势: 最近 20% vs 前 80% 的均值差
            split = max(1, int(n * 0.80))
            recent_mean = sum(scores[split:]) / max(1, n - split)
            older_mean = sum(scores[:split]) / max(1, split)
            trend = "up" if recent_mean > older_mean + 0.02 else (
                "down" if recent_mean < older_mean - 0.02 else "flat")

            return {
                "window": self.window,
                "count": n,
                "avg": round(sum(scores) / n, 4),
                "min": round(scores[0], 4),
                "max": round(scores[-1], 4),
                "p50": round(scores[p50_idx], 4),
                "p95": round(scores[min(p95_idx, n - 1)], 4),
                "p99": round(scores[min(p99_idx, n - 1)], 4),
                "trend": trend,
                "recent_mean": round(recent_mean, 4),
                "older_mean": round(older_mean, 4),
                "alert": self._last_alert.to_dict() if self._last_alert else None,
            }

    def _check_alert(self, overall: float) -> Optional[EvalAlert]:
        """异常检测: 连续 N 轮低于阈值触发告警。"""
        if overall < self.alert_threshold:
            self._below_threshold_streak += 1
        else:
            self._below_threshold_streak = 0

        if self._below_threshold_streak >= self.alert_consecutive:
            level = AlertLevel.CRITICAL if self._below_threshold_streak >= self.alert_consecutive * 2 else AlertLevel.WARNING
            alert = EvalAlert(
                level=level,
                message=(
                    f"Continuous eval alert: overall_score({overall:.4f}) "
                    f"below threshold({self.alert_threshold}) for "
                    f"{self._below_threshold_streak} consecutive rounds."
                ),
                consecutive_below_threshold=self._below_threshold_streak,
                current_overall=overall,
                threshold=self.alert_threshold,
                timestamp=time.time(),
            )
            self._last_alert = alert
            logger.warning(alert.message)
            return alert
        return None

    def clear_alert(self) -> None:
        """清除告警状态。"""
        with self._lock:
            self._below_threshold_streak = 0
            self._last_alert = None

    # ------------------------------------------------------------------
    # 检索管线钩子
    # ------------------------------------------------------------------

    def hook_search(self, query: str, results: list, **kwargs) -> Optional[RagasMetrics]:
        """
        检索管线钩子: 在 search() 返回后自动调用。

        从 results 中提取 contexts 和 answer（如存在），
        自动触发 evaluate()。

        Args:
            query: 原始查询
            results: search() 返回结果列表（dict 或 SearchResult 对象）
            **kwargs: 可包含 answer / dimension / query_id / ground_truth

        Returns:
            RagasMetrics 或 None
        """
        # 提取 contexts
        contexts: list[str] = []
        for r in results:
            if isinstance(r, dict):
                text = r.get("text") or r.get("content") or r.get("chunk") or ""
            else:
                text = getattr(r, "text", "") or getattr(r, "content", "") or getattr(r, "chunk", "") or ""
            if text:
                contexts.append(text)

        answer = kwargs.get("answer", "")
        query_id = kwargs.get("query_id", f"q_{int(time.time() * 1000)}")
        ground_truth = kwargs.get("ground_truth", None)
        dimension = kwargs.get("dimension", "retrieval_accuracy")

        # 无答案时仅计算 context_precision（基于 query 的近似）
        if not answer and contexts:
            answer = " ".join(r.get("text", "") if isinstance(r, dict)
                              else getattr(r, "text", "")
                              for r in results[:3])

        return self.evaluate(
            query=query,
            answer=answer,
            contexts=contexts,
            ground_truth=ground_truth,
            query_id=query_id,
            dimension=dimension,
        )

    def statistics(self) -> dict:
        with self._lock:
            return {
                "total_evaluations": self.total_evaluations,
                "enabled": self.enabled,
                "window": self.window,
                "alert_threshold": self.alert_threshold,
                "below_threshold_streak": self._below_threshold_streak,
                "alert_active": self._last_alert is not None,
                "rolling_report": self.rolling_report(),
            }


# =============================================================================
# EvalResultStore — 环形缓冲区存储
# =============================================================================

class EvalResultStore:
    """
    CB55: EvalResultStore — 评测结果环形缓冲区。

    保留最近 N 条结果，支持按 BEAMLIGHT 维度分组导出 Markdown 报告。
    """

    def __init__(self, buffer_size: int = CONTINUOUS_EVAL_BUFFER_SIZE):
        self.buffer_size = buffer_size
        self._buffer: list[tuple[RagasMetrics, str]] = []  # (metrics, dimension)
        self._lock = threading.RLock()
        self.total_stored: int = 0

    def add(self, metrics: RagasMetrics, dimension: str) -> None:
        """追加评测结果。"""
        with self._lock:
            self._buffer.append((metrics, dimension))
            self.total_stored += 1
            # 环形淘汰
            while len(self._buffer) > self.buffer_size:
                self._buffer.pop(0)

    def get_recent(self, n: int = 100) -> list[RagasMetrics]:
        """获取最近 N 条结果。"""
        with self._lock:
            return [m for m, _ in self._buffer[-n:]]

    def get_by_dimension(self, dimension: str) -> list[RagasMetrics]:
        """按维度筛选。"""
        with self._lock:
            return [m for m, d in self._buffer if d == dimension]

    def dimension_summary(self) -> dict:
        """按 BEAMLIGHT 10 维度分组统计。"""
        with self._lock:
            dim_scores: dict[str, list[float]] = defaultdict(list)
            for m, d in self._buffer:
                dim_scores[d].append(m.overall_score)

            summary = {}
            for dim in BEAMLIGHT_DIMENSIONS:
                scores = dim_scores.get(dim, [])
                if scores:
                    summary[dim] = {
                        "label": BEAMLIGHT_DIMENSIONS[dim],
                        "count": len(scores),
                        "avg": round(sum(scores) / len(scores), 4),
                        "min": round(min(scores), 4),
                        "max": round(max(scores), 4),
                    }
                else:
                    summary[dim] = {
                        "label": BEAMLIGHT_DIMENSIONS[dim],
                        "count": 0,
                        "avg": 0.0,
                        "min": 0.0,
                        "max": 0.0,
                    }
            return summary

    def export_report(self, output_path: Optional[str] = None) -> str:
        """
        生成 Markdown 评测报告。

        Args:
            output_path: 输出文件路径，不传则仅返回字符串。

        Returns:
            Markdown 格式报告
        """
        with self._lock:
            now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

            # 维度汇总
            dim_summary = self.dimension_summary()

            # 全量统计
            all_overalls = [m.overall_score for m, _ in self._buffer]
            all_faith = [m.faithfulness for m, _ in self._buffer]
            all_rel = [m.answer_relevancy for m, _ in self._buffer]
            all_prec = [m.context_precision for m, _ in self._buffer]
            all_rec = [m.context_recall for m, _ in self._buffer]

            n = len(all_overalls) or 1

            lines = [
                "# Trinity Continuous Evaluation Report",
                f"Generated: {now}",
                f"Buffer: {len(self._buffer)} / {self.buffer_size} (total stored: {self.total_stored})",
                "",
                "## Overall Metrics",
                "",
                "| Metric | Avg | Min | Max |",
                "|--------|-----|-----|-----|",
                f"| faithfulness | {self._safe_avg(all_faith)} | {self._safe_min(all_faith)} | {self._safe_max(all_faith)} |",
                f"| answer_relevancy | {self._safe_avg(all_rel)} | {self._safe_min(all_rel)} | {self._safe_max(all_rel)} |",
                f"| context_precision | {self._safe_avg(all_prec)} | {self._safe_min(all_prec)} | {self._safe_max(all_prec)} |",
                f"| context_recall | {self._safe_avg(all_rec)} | {self._safe_min(all_rec)} | {self._safe_max(all_rec)} |",
                f"| **overall_score** | **{self._safe_avg(all_overalls)}** | **{self._safe_min(all_overalls)}** | **{self._safe_max(all_overalls)}** |",
                "",
                "## BEAMLIGHT 10-Dimension Breakdown",
                "",
                "| # | Dimension | Label | Count | Avg Score | Min | Max |",
                "|---|-----------|-------|-------|-----------|-----|-----|",
            ]

            for idx, (dim, label) in enumerate(BEAMLIGHT_DIMENSIONS.items(), 1):
                d = dim_summary[dim]
                lines.append(
                    f"| {idx} | {dim} | {label} | {d['count']} | "
                    f"{d['avg']} | {d['min']} | {d['max']} |"
                )

            lines += [
                "",
                "---",
                f"*Report generated by Trinity ContinuousEvalEngine (CB55) — RAGAS-aligned*",
                "",
            ]

            report = "\n".join(lines)

            if output_path:
                with open(output_path, 'w', encoding='utf-8') as f:
                    f.write(report)

            return report

    @staticmethod
    def _safe_avg(values: list[float]) -> float:
        return round(sum(values) / max(1, len(values)), 4)

    @staticmethod
    def _safe_min(values: list[float]) -> float:
        return round(min(values), 4) if values else 0.0

    @staticmethod
    def _safe_max(values: list[float]) -> float:
        return round(max(values), 4) if values else 0.0


# =============================================================================
# 工厂函数
# =============================================================================

def create_eval_engine(
    enabled: bool = CONTINUOUS_EVAL_ENABLED,
    window: int = CONTINUOUS_EVAL_WINDOW,
    alert_threshold: float = CONTINUOUS_EVAL_ALERT_THRESHOLD,
    alert_consecutive: int = CONTINUOUS_EVAL_ALERT_CONSECUTIVE,
    buffer_size: int = CONTINUOUS_EVAL_BUFFER_SIZE,
    pipeline_ref: Any = None,
) -> ContinuousEvalEngine:
    """创建 ContinuousEvalEngine 工厂函数。"""
    engine = ContinuousEvalEngine(
        enabled=enabled,
        window=window,
        alert_threshold=alert_threshold,
        alert_consecutive=alert_consecutive,
        buffer_size=buffer_size,
    )
    engine.pipeline_ref = pipeline_ref
    return engine


# =============================================================================
# Self-Test
# =============================================================================

def self_test() -> bool:
    """自测: 验证四指标计算 + 滑动窗口 + 异常告警 + 报告导出。"""
    import uuid

    try:
        # === 1. RagasMetrics 基础 ===
        m = RagasMetrics(faithfulness=0.85, answer_relevancy=0.90,
                         context_precision=0.75, context_recall=0.80)
        assert 0.83 < m.overall_score < 0.85, f"Overall={m.overall_score}"
        print(f"TEST-1 PASS: RagasMetrics overall={m.overall_score}")

        # === 2. faithfulness ===
        engine = ContinuousEvalEngine(enabled=True)
        faith = engine._compute_faithfulness(
            answer="Paris is the capital of France. It is known for the Eiffel Tower.",
            contexts=["Paris is the capital of France.",
                      "The Eiffel Tower is a famous landmark in Paris."],
        )
        assert faith >= 0.5, f"faithfulness={faith} too low"
        print(f"TEST-2 PASS: faithfulness={faith}")

        # === 3. answer_relevancy ===
        rel = engine._compute_answer_relevancy(
            query="What is the capital of France?",
            answer="Paris is the capital of France, known for the Eiffel Tower.",
        )
        assert rel > 0.0, f"relevancy={rel}"
        print(f"TEST-3 PASS: answer_relevancy={rel}")

        # === 4. context_precision ===
        prec = engine._compute_context_precision(
            query="capital of France",
            answer="Paris is the capital of France.",
            contexts=[
                "Random text about weather",
                "Paris is the capital of France.",
                "More unrelated content",
            ],
        )
        assert prec == 0.5, f"precision should be 1/2 = 0.5, got {prec}"
        print(f"TEST-4 PASS: context_precision={prec}")

        # === 5. context_recall ===
        rec = engine._compute_context_recall(
            answer="Paris and Lyon are major French cities.",
            contexts=["Paris is the capital. Lyon is known for cuisine. Marseille is a port city."],
        )
        assert rec > 0.0, f"recall={rec}"
        print(f"TEST-5 PASS: context_recall={rec}")

        # === 6. evaluate 全流程 ===
        qid = f"test_{uuid.uuid4().hex[:8]}"
        metrics = engine.evaluate(
            query="What is Python?",
            answer="Python is a high-level programming language known for readability.",
            contexts=[
                "Python is a high-level, interpreted programming language.",
                "Python emphasizes code readability with significant indentation.",
                "Java is a statically-typed language.",
            ],
            query_id=qid,
        )
        assert metrics is not None
        assert metrics.query_id == qid
        assert metrics.faithfulness >= 0.0
        assert metrics.overall_score > 0.0
        print(f"TEST-6 PASS: evaluate={metrics.to_dict()}")

        # === 7. sliding window ===
        for i in range(50):
            engine.evaluate(
                query="test", answer=f"answer {i}", contexts=["context"],
                query_id=f"q_{i}",
            )
        report = engine.rolling_report()
        assert "avg" in report
        assert report["count"] <= engine.window
        print(f"TEST-7 PASS: rolling_report count={report['count']} avg={report['avg']}")

        # === 8. alert 异常检测 ===
        engine2 = ContinuousEvalEngine(
            enabled=True, alert_threshold=0.90, alert_consecutive=3)
        # Inject all low scores
        for i in range(5):
            engine2.evaluate(
                query="test", answer=f"bad answer {i}", contexts=["bad context"],
                query_id=f"alert_{i}",
            )
        assert engine2._below_threshold_streak >= 3
        alert = engine2._check_alert(0.30)
        assert alert is not None or engine2._last_alert is not None
        print(f"TEST-8 PASS: alert streak={engine2._below_threshold_streak}")

        # === 9. EvalResultStore + export report ===
        store = engine.store
        dim_summary = store.dimension_summary()
        assert len(dim_summary) == 10, f"Expected 10 dims, got {len(dim_summary)}"

        import tempfile, os
        tmp = os.path.join(tempfile.gettempdir(), "trinity_eval_test_report.md")
        report_md = store.export_report(tmp)
        assert os.path.exists(tmp)
        with open(tmp, 'r', encoding='utf-8') as f:
            assert "BEAMLIGHT 10-Dimension Breakdown" in f.read()
        os.remove(tmp)
        print(f"TEST-9 PASS: report exported ({len(report_md)} chars)")

        # === 10. hook_search ===
        fake_results = [
            {"text": "Python is a programming language.", "score": 0.9},
            {"text": "Java is also a language.", "score": 0.5},
        ]
        hooked = engine.hook_search(
            query="What is Python?",
            results=fake_results,
            answer="Python is a high-level programming language.",
        )
        assert hooked is not None
        assert hooked.faithfulness >= 0.0
        print(f"TEST-10 PASS: hook_search overall={hooked.overall_score}")

        # === 11. factory ===
        eng3 = create_eval_engine(enabled=False)
        assert eng3.evaluate("q", "a", ["c"]) is None
        print("TEST-11 PASS: factory disabled")

        # === 12. store capacity ===
        small_store = EvalResultStore(buffer_size=5)
        for i in range(10):
            small_store.add(RagasMetrics(overall_score=i / 10), "retrieval_accuracy")
        assert len(small_store._buffer) == 5
        assert small_store._buffer[-1][0].overall_score == 0.9
        print("TEST-12 PASS: ring buffer capacity")

        logger.info("self_test: ALL 12 ASSERTIONS PASSED")
        return True
    except Exception as e:
        logger.error("self_test: FAILED — %s", e)
        import traceback
        traceback.print_exc()
        raise


print("[P127] ContinuousEvalEngine (CB55) initialized — RAGAS-aligned")
