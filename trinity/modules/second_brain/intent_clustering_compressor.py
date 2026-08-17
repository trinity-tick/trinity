"""
# status: orphan (2026-08-15 audit, not in runtime path)
P11-4: Intent-Aware Memory Compressor (对标 SimpleMem ICML 2026)
=================================================================

意图聚类压缩：围绕用户意图类型（而非时间或原始上下文）组织历史记忆。
  - IntentDetector: 识别查询意图类型
  - IntentClusterer: 按意图聚类历史记忆
  - CompressedSummary: 意图对齐压缩摘要，保留关键偏好信号
  - IntentAlignedRetrieval: 查询时按意图类型对齐检索

与现有 memory_compressor / token_budget 接口兼容。

Reference:
  - SimpleMem — Intent-Clustering Memory Compression, ICML 2026
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举 ────────────────────────────────────────────────────────────

class IntentType(Enum):
    """用户意图类型"""
    FACT_QUERY = "fact_query"             # 事实查询（"X是什么"）
    PREFERENCE_MODIFY = "preference_modify"  # 偏好修改（"我更喜欢"）
    TASK_EXECUTION = "task_execution"      # 任务执行（"帮我做X"）
    EXPLORATION = "exploration"           # 探索浏览（"有什么推荐"）
    COMPARISON = "comparison"            # 对比分析（"A和B哪个好"）
    CLARIFICATION = "clarification"      # 澄清确认（"你的意思是"）
    UNKNOWN = "unknown"


class CompressionLevel(Enum):
    """压缩级别"""
    NONE = 0           # 不压缩
    LIGHT = 1          # 轻度压缩（移除冗余修饰）
    STANDARD = 2       # 标准压缩（保留关键信息）
    AGGRESSIVE = 3     # 激进压缩（仅保留偏好信号）
    TOKEN_OPTIMAL = 4  # Token 最优（硬预算约束）


class RetrievalStrategy(Enum):
    """检索策略"""
    INTENT_ALIGNED = "intent_aligned"           # 仅同意图检索
    INTENT_WEIGHTED = "intent_weighted"         # 意图加权混合
    CROSS_INTENT = "cross_intent"               # 跨意图检索
    HYBRID = "hybrid"                           # 混合策略


# ── 数据类 ──────────────────────────────────────────────────────────

@dataclass
class IntentSignal:
    """意图信号特征"""
    query: str = ""
    intent_type: IntentType = IntentType.UNKNOWN
    confidence: float = 0.0
    keywords: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    signal_strength: float = 0.0               # 信号总强度

    def to_dict(self) -> Dict[str, Any]:
        return {
            "intent_type": self.intent_type.value,
            "confidence": self.confidence,
            "keywords": self.keywords,
            "entities": self.entities,
            "signal_strength": self.signal_strength,
        }


@dataclass
class MemorySnip:
    """记忆片段"""
    snip_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    content: str = ""
    intent_type: IntentType = IntentType.UNKNOWN
    intent_confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    preference_signals: Dict[str, float] = field(default_factory=dict)  # domain -> strength
    token_count: int = 0
    importance: float = 0.5


@dataclass
class IntentCluster:
    """意图聚类"""
    cluster_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    intent_type: IntentType = IntentType.UNKNOWN
    members: List[MemorySnip] = field(default_factory=list)
    centroid_keywords: List[str] = field(default_factory=list)  # 聚类中心关键词
    total_tokens: int = 0
    member_count: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def avg_importance(self) -> float:
        if not self.members:
            return 0.0
        return sum(m.importance for m in self.members) / len(self.members)


@dataclass
class CompressedSummary:
    """意图对齐压缩摘要"""
    summary_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    intent_type: IntentType = IntentType.UNKNOWN
    compressed_text: str = ""
    original_tokens: int = 0
    compressed_tokens: int = 0
    compression_ratio: float = 0.0
    preserved_signals: Dict[str, float] = field(default_factory=dict)
    source_cluster_id: str = ""
    level: CompressionLevel = CompressionLevel.STANDARD


@dataclass
class RetrieveResult:
    """检索结果"""
    query_intent: IntentSignal = field(default_factory=IntentSignal)
    aligned_summaries: List[CompressedSummary] = field(default_factory=list)
    total_tokens_retrieved: int = 0
    strategy: RetrievalStrategy = RetrievalStrategy.INTENT_ALIGNED
    latency_ms: float = 0.0


# ── IntentDetector ──────────────────────────────────────────────────

class IntentDetector:
    """意图检测器：识别当前查询的意图类型。

    使用基于规则的信号匹配（生产环境可接入轻量分类器）：
      - 事实查询：问句结构（什么是/怎么/为什么）
      - 偏好修改：含有偏好表达词（更喜欢/不喜欢/改为/改成）
      - 任务执行：动宾结构（帮我/请/执行）
      - 探索浏览：推荐/有什么/看看/找找
      - 对比分析：比较/A和B/哪个好
    """

    # 意图信号词典
    INTENT_PATTERNS: Dict[IntentType, List[str]] = {
        IntentType.FACT_QUERY: [
            "什么是", "怎么", "为什么", "定义", "含义", "解释",
            "如何", "how", "what is", "define",
        ],
        IntentType.PREFERENCE_MODIFY: [
            "更喜欢", "不喜欢", "改为", "改成", "偏好", "偏好是",
            "我比较喜欢", "调整", "设为", "设置偏好", "换成",
        ],
        IntentType.TASK_EXECUTION: [
            "帮我", "请", "执行", "运行", "下载", "创建", "删除",
            "打开", "关闭", "发送", "生成", "做", "给",
        ],
        IntentType.EXPLORATION: [
            "推荐", "有什么", "看看", "找找", "浏览", "搜索",
            "有哪些", "列出", "展示", "发现", "explore",
        ],
        IntentType.COMPARISON: [
            "比较", "对比", "哪个好", "区别", "差异", "vs",
            "和.*哪个", "还是", "选哪个",
        ],
        IntentType.CLARIFICATION: [
            "你的意思是", "是说", "对吗", "确认", "是不是",
            "能不能", "确认一下", "再说一遍",
        ],
    }

    def __init__(self, confidence_threshold: float = 0.3):
        self.confidence_threshold = confidence_threshold
        logger.info(f"[IntentDetector] Initialized (threshold={confidence_threshold})")

    def detect(self, query: str) -> IntentSignal:
        """检测查询的意图类型。

        返回 IntentSignal 包含类型、置信度、关键词等。
        """
        query_lower = query.lower().strip()
        scores: Dict[IntentType, float] = {}
        all_matched_keywords: Dict[IntentType, List[str]] = {}

        for intent_type, patterns in self.INTENT_PATTERNS.items():
            matched = []
            score = 0.0
            for pattern in patterns:
                import re
                if re.search(pattern, query_lower):
                    matched.append(pattern)
                    # 匹配得分：模式长度越具体，得分越高
                    score += min(1.0, len(pattern) / 10.0)
            if matched:
                # 匹配得分：每个模式最多贡献 1.0，按匹配数累加后归一化
                scores[intent_type] = min(1.0, score)
                all_matched_keywords[intent_type] = matched

        if not scores:
            return IntentSignal(
                query=query,
                intent_type=IntentType.UNKNOWN,
                confidence=0.0,
                keywords=[],
            )

        best_intent = max(scores, key=scores.get)
        confidence = scores[best_intent]

        if confidence < self.confidence_threshold:
            best_intent = IntentType.UNKNOWN
            confidence = 0.0

        return IntentSignal(
            query=query,
            intent_type=best_intent,
            confidence=confidence,
            keywords=all_matched_keywords.get(best_intent, []),
            signal_strength=confidence,
        )

    def statistics(self) -> Dict[str, Any]:
        return {
            "confidence_threshold": self.confidence_threshold,
            "supported_intents": [t.value for t in IntentType],
        }


# ── IntentClusterer ─────────────────────────────────────────────────

class IntentClusterer:
    """意图聚类器：围绕意图类型聚类历史记忆。

    与时间聚类 / 上下文聚类的关键区别：
      - 聚类依据是 intent_type，不是时间窗口或对话边界
      - 同一意图下的记忆即使时间分散也会聚合
      - 跨意图记忆被隔离，避免噪音混入
    """

    def __init__(self, max_cluster_size: int = 100):
        self.max_cluster_size = max_cluster_size
        self._clusters: Dict[IntentType, IntentCluster] = {}
        self._snip_lookup: Dict[str, MemorySnip] = {}  # snip_id -> snip for fast lookup
        self._lock = threading.RLock()
        self._init_clusters()
        logger.info(f"[IntentClusterer] Initialized (max_cluster_size={max_cluster_size})")

    def _init_clusters(self) -> None:
        for intent_type in IntentType:
            self._clusters[intent_type] = IntentCluster(intent_type=intent_type)

    def add_memory(self, snip: MemorySnip) -> IntentCluster:
        """向对应意图聚类添加记忆片段。"""
        with self._lock:
            cluster = self._clusters[snip.intent_type]
            cluster.members.append(snip)
            cluster.total_tokens += snip.token_count
            cluster.member_count += 1
            self._snip_lookup[snip.snip_id] = snip

            # 更新聚类中心关键词
            all_keywords: List[str] = []
            for m in cluster.members[-50:]:
                if hasattr(m, "content"):
                    all_keywords.extend(m.content.split()[:10])

            # 简单 TF 更新 centroid
            if all_keywords:
                from collections import Counter
                counter = Counter(all_keywords)
                cluster.centroid_keywords = [w for w, _ in counter.most_common(10)]

            # 限制聚类大小
            if len(cluster.members) > self.max_cluster_size:
                # 保留重要性最高的
                cluster.members.sort(key=lambda m: m.importance, reverse=True)
                removed = cluster.members[self.max_cluster_size:]
                cluster.members = cluster.members[:self.max_cluster_size]
                for rm in removed:
                    cluster.total_tokens -= rm.token_count
                    cluster.member_count -= 1

            return cluster

    def get_cluster(self, intent_type: IntentType) -> IntentCluster:
        return self._clusters[intent_type]

    def all_clusters(self) -> Dict[IntentType, IntentCluster]:
        return dict(self._clusters)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            cluster_info = {}
            for intent_type, cluster in self._clusters.items():
                cluster_info[intent_type.value] = {
                    "member_count": cluster.member_count,
                    "total_tokens": cluster.total_tokens,
                    "avg_importance": cluster.avg_importance,
                    "centroid": cluster.centroid_keywords[:5],
                }
            return {
                "total_snips": len(self._snip_lookup),
                "max_cluster_size": self.max_cluster_size,
                "clusters": cluster_info,
            }


# ── CompressedSummaryGenerator ──────────────────────────────────────

class CompressedSummaryGenerator:
    """压缩摘要生成器：生成意图对齐的压缩摘要。

    原则：
      - 保留偏好信号（preference_signals）：永不丢弃
      - 事实类保留关键实体 + 结论
      - 任务类保留动作 + 结果
      - 探索类保留推荐项 + 用户反馈
      - 与 token_budget 兼容，支持硬 Token 上限
    """

    def __init__(
        self,
        token_budget: int = 500,
        default_level: CompressionLevel = CompressionLevel.STANDARD,
    ):
        self.token_budget = token_budget
        self.default_level = default_level
        self._summaries: Dict[str, CompressedSummary] = {}
        logger.info(
            f"[CompressedSummaryGenerator] Initialized "
            f"(budget={token_budget}, level={default_level.name})"
        )

    def compress(
        self,
        cluster: IntentCluster,
        level: Optional[CompressionLevel] = None,
    ) -> CompressedSummary:
        """将意图聚类压缩为摘要。

        压缩策略因 intent_type 而异：
          - FACT_QUERY: 保留事实结论 + 定义
          - PREFERENCE_MODIFY: 保留偏好信号 + 变更记录
          - TASK_EXECUTION: 保留动作摘要 + 结果
          - EXPLORATION: 保留推荐项 + 反馈
        """
        level = level or self.default_level

        if not cluster.members:
            return CompressedSummary(
                intent_type=cluster.intent_type,
                compressed_text="(empty)",
                original_tokens=0,
                compressed_tokens=0,
                level=level,
            )

        # 收集所有偏好信号（最高优先级保留）
        all_signals: Dict[str, float] = {}
        for snip in cluster.members:
            for domain, strength in snip.preference_signals.items():
                all_signals[domain] = max(all_signals.get(domain, 0.0), strength)

        # 按意图类型生成摘要
        compressed_lines = self._build_summary_lines(cluster, level, all_signals)

        compressed_text = "\n".join(compressed_lines)
        original_tokens = cluster.total_tokens
        compressed_tokens = len(compressed_text.split())

        # Token 预算约束
        if level == CompressionLevel.TOKEN_OPTIMAL:
            words = compressed_text.split()
            if len(words) > self.token_budget:
                compressed_text = " ".join(words[:self.token_budget]) + "…"
                compressed_tokens = self.token_budget

        compression_ratio = (
            1.0 - compressed_tokens / max(original_tokens, 1)
            if original_tokens > 0
            else 0.0
        )

        summary = CompressedSummary(
            intent_type=cluster.intent_type,
            compressed_text=compressed_text,
            original_tokens=original_tokens,
            compressed_tokens=compressed_tokens,
            compression_ratio=compression_ratio,
            preserved_signals=all_signals,
            source_cluster_id=cluster.cluster_id,
            level=level,
        )

        self._summaries[summary.summary_id] = summary
        logger.debug(
            f"[CompressedSummary] {cluster.intent_type.value}: "
            f"{original_tokens} -> {compressed_tokens} tokens "
            f"({compression_ratio:.1%})"
        )

        return summary

    def _build_summary_lines(
        self,
        cluster: IntentCluster,
        level: CompressionLevel,
        signals: Dict[str, float],
    ) -> List[str]:
        """按意图类型和压缩级别构建摘要行。"""
        lines: List[str] = [f"[{cluster.intent_type.value}] Intent Summary"]

        if cluster.intent_type == IntentType.PREFERENCE_MODIFY:
            lines.append("Preferences:")
            for domain, strength in signals.items():
                lines.append(f"  - {domain}: {strength:.2f}")
            if level.value <= CompressionLevel.STANDARD.value:
                for snip in cluster.members[:5]:
                    lines.append(f"  · {snip.content[:80]}")

        elif cluster.intent_type == IntentType.FACT_QUERY:
            lines.append("Key Facts:")
            for snip in cluster.members[:5]:
                lines.append(f"  - {snip.content[:120]}")

        elif cluster.intent_type == IntentType.TASK_EXECUTION:
            lines.append("Executed Tasks:")
            for snip in cluster.members[:5]:
                lines.append(f"  - {snip.content[:100]}")

        elif cluster.intent_type == IntentType.EXPLORATION:
            lines.append("Explored Items:")
            for snip in cluster.members[:5]:
                lines.append(f"  - {snip.content[:100]}")

        else:
            for snip in cluster.members[:3]:
                lines.append(f"  - {snip.content[:100]}")

        # 追加偏好信号（所有意图类型都保留）
        if signals and cluster.intent_type != IntentType.PREFERENCE_MODIFY:
            lines.append("Preference Signals:")
            for domain, strength in list(signals.items())[:5]:
                lines.append(f"  - {domain}: {strength:.2f}")

        return lines

    def get_summary(self, summary_id: str) -> Optional[CompressedSummary]:
        return self._summaries.get(summary_id)

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_summaries": len(self._summaries),
            "token_budget": self.token_budget,
            "default_level": self.default_level.name,
            "avg_compression_ratio": (
                sum(s.compression_ratio for s in self._summaries.values())
                / max(len(self._summaries), 1)
            ),
        }


# ── IntentAlignedRetriever ──────────────────────────────────────────

class IntentAlignedRetriever:
    """意图对齐检索器：查询时按意图类型对齐检索。

    与现有 memory/vector store 接口兼容：
      - 接收 query 字符串
      - 返回 RetrieveResult 含意图对齐的压缩摘要列表
      - 支持多种检索策略
      - 避免跨意图噪音
    """

    def __init__(
        self,
        detector: Optional[IntentDetector] = None,
        clusterer: Optional[IntentClusterer] = None,
        compressor: Optional[CompressedSummaryGenerator] = None,
        default_strategy: RetrievalStrategy = RetrievalStrategy.INTENT_ALIGNED,
    ):
        self.detector = detector or IntentDetector()
        self.clusterer = clusterer or IntentClusterer()
        self.compressor = compressor or CompressedSummaryGenerator()
        self.default_strategy = default_strategy
        self._retrieval_history: deque[RetrieveResult] = deque(maxlen=100)
        logger.info(
            f"[IntentAlignedRetriever] Initialized "
            f"(strategy={default_strategy.value})"
        )

    def retrieve(
        self,
        query: str,
        strategy: Optional[RetrievalStrategy] = None,
        max_tokens: int = 2000,
    ) -> RetrieveResult:
        """意图对齐检索。

        流程：
          1. IntentDetector 检测查询意图
          2. 按策略选择相关聚类
          3. 压缩并返回意图对齐摘要
        """
        t0 = time.time()
        strategy = strategy or self.default_strategy

        # Step 1: 意图检测
        intent_signal = self.detector.detect(query)

        # Step 2: 选择聚类
        relevant_summaries: List[CompressedSummary] = []

        if strategy == RetrievalStrategy.INTENT_ALIGNED:
            # 仅同意图检索
            cluster = self.clusterer.get_cluster(intent_signal.intent_type)
            if cluster.members:
                summary = self.compressor.compress(cluster)
                relevant_summaries.append(summary)

        elif strategy == RetrievalStrategy.INTENT_WEIGHTED:
            # 意图加权混合：主意图权重最高，按相关性递减
            cluster = self.clusterer.get_cluster(intent_signal.intent_type)
            if cluster.members:
                summary = self.compressor.compress(cluster)
                relevant_summaries.append(summary)

            # 相关意图类型（降权）
            related_intents = self._related_intents(intent_signal.intent_type)
            for related in related_intents:
                r_cluster = self.clusterer.get_cluster(related)
                if r_cluster.members:
                    r_summary = self.compressor.compress(r_cluster, CompressionLevel.AGGRESSIVE)
                    relevant_summaries.append(r_summary)

        elif strategy == RetrievalStrategy.CROSS_INTENT:
            # 跨意图检索（所有聚类都参与）
            for intent_type, cluster in self.clusterer.all_clusters().items():
                if cluster.members:
                    level = (
                        CompressionLevel.STANDARD
                        if intent_type == intent_signal.intent_type
                        else CompressionLevel.AGGRESSIVE
                    )
                    summary = self.compressor.compress(cluster, level)
                    relevant_summaries.append(summary)

        elif strategy == RetrievalStrategy.HYBRID:
            current_cluster = self.clusterer.get_cluster(intent_signal.intent_type)
            if current_cluster.members:
                summary = self.compressor.compress(current_cluster)
                relevant_summaries.append(summary)
            for it in [IntentType.PREFERENCE_MODIFY, IntentType.FACT_QUERY]:
                if it != intent_signal.intent_type:
                    c = self.clusterer.get_cluster(it)
                    if c.members:
                        s = self.compressor.compress(c, CompressionLevel.AGGRESSIVE)
                        relevant_summaries.append(s)

        # Token 预算约束
        total_tokens = sum(s.compressed_tokens for s in relevant_summaries)
        while total_tokens > max_tokens and len(relevant_summaries) > 1:
            # 移除最低优先级的
            removed = relevant_summaries.pop(-1)
            total_tokens -= removed.compressed_tokens

        latency = (time.time() - t0) * 1000

        result = RetrieveResult(
            query_intent=intent_signal,
            aligned_summaries=relevant_summaries,
            total_tokens_retrieved=total_tokens,
            strategy=strategy,
            latency_ms=latency,
        )
        self._retrieval_history.append(result)
        return result

    @staticmethod
    def _related_intents(intent_type: IntentType) -> List[IntentType]:
        """获取与给定意图相关的意图类型。"""
        related_map = {
            IntentType.FACT_QUERY: [IntentType.CLARIFICATION, IntentType.COMPARISON],
            IntentType.TASK_EXECUTION: [IntentType.FACT_QUERY, IntentType.PREFERENCE_MODIFY],
            IntentType.EXPLORATION: [IntentType.COMPARISON, IntentType.PREFERENCE_MODIFY],
            IntentType.PREFERENCE_MODIFY: [IntentType.EXPLORATION],
            IntentType.COMPARISON: [IntentType.FACT_QUERY, IntentType.EXPLORATION],
        }
        return related_map.get(intent_type, [])

    def statistics(self) -> Dict[str, Any]:
        return {
            "detector": self.detector.statistics(),
            "clusterer": self.clusterer.statistics(),
            "compressor": self.compressor.statistics(),
            "retrieval_count": len(self._retrieval_history),
            "default_strategy": self.default_strategy.value,
        }
