"""
# status: orphan (2026-08-15 audit, not in runtime path)
P19-7: Query Intent Router — Multi-Partition Intent-Based Retrieval Router
===========================================================================

对标 2026 查询意图路由方案。

设计要点：
  - 意图预分类：个人知识 / 领域知识 / 操作指令 / 探索查询
  - 定向检索对应记忆分区
  - 跨分区结果融合与冲突消解
  - 路由决策学习（反馈纠偏）
  - Qwen2.5 意图分类微调接口

核心组件：
  - IntentPreClassifier:       意图预分类器
  - MemoryPartitionRouter:     记忆分区路由器
  - CrossPartitionFusion:      跨分区结果融合
  - ConflictResolver:          冲突消解引擎
  - RoutingFeedbackLearner:    反馈纠偏学习器
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class QueryIntent(Enum):
    """查询意图分类。"""
    PERSONAL_KNOWLEDGE = "personal_knowledge"    # 个人知识：我的收藏、我的笔记
    DOMAIN_KNOWLEDGE = "domain_knowledge"        # 领域知识：专业文献、技术文档
    OPERATION_COMMAND = "operation_command"      # 操作指令：执行任务、工具调用
    EXPLORATORY = "exploratory"                  # 探索查询：浏览、发现


class MemoryPartition(Enum):
    """记忆分区。"""
    PERSONAL = "personal"          # 个人记忆区
    DOMAIN = "domain"              # 领域知识区
    PROCEDURAL = "procedural"      # 程序习惯区
    EPISODIC = "episodic"          # 情景经历区
    SEMANTIC = "semantic"          # 语义网络区
    WORKING = "working"            # 工作记忆缓冲


class ConflictType(Enum):
    """冲突类型。"""
    CONTRADICTION = "contradiction"    # 矛盾：两结果互斥
    REDUNDANCY = "redundancy"          # 冗余：相同内容
    AMBIGUITY = "ambiguity"            # 歧义：含义模糊
    STALENESS = "staleness"            # 陈旧：过时信息


class FeedbackSignal(Enum):
    """反馈信号。"""
    POSITIVE = "positive"          # 正面：用户认可
    NEGATIVE = "negative"          # 负面：用户纠正
    AMBIVALENT = "ambivalent"      # 矛盾：用户犹豫
    IGNORED = "ignored"            # 忽略：用户未使用


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class IntentClassification:
    """意图分类结果。"""
    intent_id: str
    query: str
    primary_intent: QueryIntent
    secondary_intents: List[Tuple[QueryIntent, float]] = field(default_factory=list)
    confidence: float = 0.5
    complexity: float = 0.3
    expected_partitions: List[MemoryPartition] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PartitionResult:
    """分区检索结果。"""
    result_id: str
    partition: MemoryPartition
    query: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    relevance_scores: List[float] = field(default_factory=list)
    retrieval_latency_ms: float = 0.0


@dataclass
class FusionResult:
    """融合结果。"""
    fusion_id: str
    items: List[Dict[str, Any]]
    scores: List[float]
    source_partitions: List[MemoryPartition]
    conflicts: List[ConflictRecord] = field(default_factory=list)
    merged_at: float = field(default_factory=time.time)


@dataclass
class ConflictRecord:
    """冲突记录。"""
    conflict_id: str
    conflict_type: ConflictType
    item_a: Dict[str, Any]
    item_b: Dict[str, Any]
    resolution: str = ""
    resolved: bool = False


@dataclass
class RoutingFeedback:
    """路由反馈。"""
    feedback_id: str
    intent_id: str
    signal: FeedbackSignal
    original_intent: QueryIntent
    corrected_intent: Optional[QueryIntent] = None
    user_comment: str = ""
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Constants
# ============================================================================

INTENT_KEYWORDS: Dict[QueryIntent, List[str]] = {
    QueryIntent.PERSONAL_KNOWLEDGE: [
        "我的", "我", "个人", "收藏", "笔记", "my", "personal", "mine",
        "记得", "之前", "上次", "历史",
    ],
    QueryIntent.DOMAIN_KNOWLEDGE: [
        "什么是", "定义", "原理", "论文", "研究", "文档", "技术",
        "define", "research", "paper", "documentation", "how does",
    ],
    QueryIntent.OPERATION_COMMAND: [
        "执行", "运行", "创建", "删除", "修改", "设置", "帮",
        "run", "execute", "create", "delete", "update", "config",
    ],
    QueryIntent.EXPLORATORY: [
        "有哪些", "找一下", "浏览", "发现", "推荐", "搜索",
        "list", "find", "browse", "explore", "recommend", "search",
    ],
}

INTENT_PARTITION_MAP: Dict[QueryIntent, List[MemoryPartition]] = {
    QueryIntent.PERSONAL_KNOWLEDGE: [
        MemoryPartition.PERSONAL, MemoryPartition.EPISODIC,
    ],
    QueryIntent.DOMAIN_KNOWLEDGE: [
        MemoryPartition.DOMAIN, MemoryPartition.SEMANTIC,
    ],
    QueryIntent.OPERATION_COMMAND: [
        MemoryPartition.PROCEDURAL, MemoryPartition.WORKING,
    ],
    QueryIntent.EXPLORATORY: [
        MemoryPartition.SEMANTIC, MemoryPartition.EPISODIC, MemoryPartition.DOMAIN,
    ],
}


# ============================================================================
# Core Components
# ============================================================================

class IntentPreClassifier:
    """意图预分类器。

    基于关键词匹配 + 启发式规则的快速意图分类。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.classifications: List[IntentClassification] = []

    def classify(self, query: str) -> IntentClassification:
        """对查询进行意图分类。"""
        with self._lock:
            q_lower = query.lower()
            scores: Dict[QueryIntent, float] = {}

            for intent, keywords in INTENT_KEYWORDS.items():
                hits = sum(1 for kw in keywords if kw in q_lower)
                density = hits / max(len(q_lower.split()), 1)
                scores[intent] = density * 100

            # 排序取主意图和次意图
            sorted_intents = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            primary = sorted_intents[0][0] if sorted_intents else QueryIntent.EXPLORATORY
            secondaries = [(i, round(s, 4)) for i, s in sorted_intents[1:3] if s > 0]

            # 置信度
            top_score = sorted_intents[0][1]
            gap = top_score - (sorted_intents[1][1] if len(sorted_intents) > 1 else 0)
            confidence = min(0.5 + gap / 20, 0.95)

            result = IntentClassification(
                intent_id=str(uuid.uuid4())[:8],
                query=query,
                primary_intent=primary,
                secondary_intents=secondaries,
                confidence=round(confidence, 4),
                expected_partitions=INTENT_PARTITION_MAP.get(primary, []),
            )
            self.classifications.append(result)
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            intent_counts = defaultdict(int)
            for c in self.classifications:
                intent_counts[c.primary_intent.value] += 1
            return {
                "total_classified": len(self.classifications),
                "by_intent": dict(intent_counts),
                "avg_confidence": round(
                    sum(c.confidence for c in self.classifications) / max(len(self.classifications), 1), 4),
            }


class MemoryPartitionRouter:
    """记忆分区路由器。

    根据意图将查询路由到相应的记忆分区。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.partition_results: List[PartitionResult] = []
        # 模拟分区存储
        self.partition_stores: Dict[MemoryPartition, List[Dict[str, Any]]] = {
            p: [] for p in MemoryPartition
        }

    def route(self, intent: IntentClassification) -> List[PartitionResult]:
        """路由查询到目标分区。"""
        with self._lock:
            results: List[PartitionResult] = []
            for partition in intent.expected_partitions:
                items = self._retrieve_from_partition(partition, intent.query)
                scores = [self._compute_relevance(item, intent.query) for item in items]
                result = PartitionResult(
                    result_id=str(uuid.uuid4())[:8],
                    partition=partition,
                    query=intent.query,
                    items=items,
                    relevance_scores=scores,
                )
                results.append(result)
            self.partition_results.extend(results)
            return results

    def _retrieve_from_partition(self, partition: MemoryPartition, query: str) -> List[Dict[str, Any]]:
        """从分区检索。含模拟检索逻辑。"""
        store = self.partition_stores.get(partition, [])
        # 简易关键词匹配
        q_words = set(query.lower().split())
        scored = []
        for item in store:
            item_text = str(item.get("content", "")).lower()
            score = sum(1 for w in q_words if w in item_text)
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:5]]

    @staticmethod
    def _compute_relevance(item: Dict[str, Any], query: str) -> float:
        """计算相关性分数。"""
        content = str(item.get("content", ""))
        q_words = set(query.lower().split())
        hits = sum(1 for w in q_words if w in content.lower())
        return min(hits / max(len(q_words), 1), 1.0)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            partition_counts = defaultdict(int)
            for r in self.partition_results:
                partition_counts[r.partition.value] += 1
            return {
                "total_routes": len(self.partition_results),
                "by_partition": dict(partition_counts),
            }


class ConflictResolver:
    """冲突消解引擎。

    处理跨分区融合中的矛盾、冗余、歧义、陈旧信息。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.resolved_conflicts: List[ConflictRecord] = []

    def detect(self, items: List[Dict[str, Any]]) -> List[ConflictRecord]:
        """检测冲突。"""
        with self._lock:
            conflicts: List[ConflictRecord] = []
            for i, a in enumerate(items):
                for b in items[i + 1:]:
                    conflict = self._check_pair(a, b)
                    if conflict:
                        conflicts.append(conflict)
            return conflicts

    def _check_pair(self, a: Dict[str, Any], b: Dict[str, Any]) -> Optional[ConflictRecord]:
        """检查一对结果是否存在冲突。"""
        content_a = str(a.get("content", "")).lower()
        content_b = str(b.get("content", "")).lower()

        # 冗余检测
        if self._jaccard(content_a, content_b) > 0.8:
            return ConflictRecord(
                conflict_id=str(uuid.uuid4())[:8],
                conflict_type=ConflictType.REDUNDANCY,
                item_a=a, item_b=b,
            )

        # 矛盾检测（简化：关键词否定匹配）
        negation_pairs = [
            ("是", "不是"), ("true", "false"), ("支持", "不支持"),
            ("可以", "不可以"), ("正确", "错误"),
        ]
        for pos, neg in negation_pairs:
            if pos in content_a and neg in content_b:
                return ConflictRecord(
                    conflict_id=str(uuid.uuid4())[:8],
                    conflict_type=ConflictType.CONTRADICTION,
                    item_a=a, item_b=b,
                )

        return None

    def resolve(self, conflict: ConflictRecord) -> ConflictRecord:
        """消解冲突。"""
        with self._lock:
            if conflict.conflict_type == ConflictType.REDUNDANCY:
                # 保留信息量更大的
                len_a = len(str(conflict.item_a.get("content", "")))
                len_b = len(str(conflict.item_b.get("content", "")))
                conflict.resolution = "保留 'a'" if len_a >= len_b else "保留 'b'"
            elif conflict.conflict_type == ConflictType.CONTRADICTION:
                # 保守策略：保留二者但标注
                conflict.resolution = "同时保留，标注矛盾"
            else:
                conflict.resolution = "人工审查"
            conflict.resolved = True
            self.resolved_conflicts.append(conflict)
            return conflict

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        set_a = set(a.split())
        set_b = set(b.split())
        intersection = len(set_a & set_b)
        union = len(set_a | set_b)
        return intersection / union if union > 0 else 0.0

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = defaultdict(int)
            for c in self.resolved_conflicts:
                type_counts[c.conflict_type.value] += 1
            return {
                "total_conflicts": len(self.resolved_conflicts),
                "by_type": dict(type_counts),
            }


class CrossPartitionFusion:
    """跨分区结果融合。

    多分区结果合并、去重、排序，附带冲突消解。
    """

    def __init__(self, resolver: ConflictResolver):
        self._lock = threading.RLock()
        self.resolver = resolver
        self.fusions: List[FusionResult] = []

    def fuse(self, partition_results: List[PartitionResult]) -> FusionResult:
        """融合多分区结果。"""
        with self._lock:
            # 合并所有结果
            all_items: List[Dict[str, Any]] = []
            all_scores: List[float] = []
            all_partitions: List[MemoryPartition] = []

            for pr in partition_results:
                for item, score in zip(pr.items, pr.relevance_scores):
                    all_items.append(item)
                    all_scores.append(score)
                    all_partitions.append(pr.partition)

            # 按分数排序
            scored = sorted(zip(all_items, all_scores, all_partitions), key=lambda x: x[1], reverse=True)
            items = [s[0] for s in scored]
            scores = [s[1] for s in scored]
            partitions = [s[2] for s in scored]

            # 冲突检测与消解
            conflicts = self.resolver.detect(items)
            for c in conflicts:
                self.resolver.resolve(c)

            fusion = FusionResult(
                fusion_id=str(uuid.uuid4())[:8],
                items=items,
                scores=scores,
                source_partitions=list(set(partitions)),
                conflicts=conflicts,
            )
            self.fusions.append(fusion)
            return fusion

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_fusions": len(self.fusions),
                "avg_items": round(
                    sum(len(f.items) for f in self.fusions) / max(len(self.fusions), 1), 1),
                "conflicts": self.resolver.statistics(),
            }


class RoutingFeedbackLearner:
    """路由决策学习器。

    基于用户反馈纠偏，支持 Qwen2.5 意图分类微调数据生成。
    """

    def __init__(self, learning_rate: float = 0.1):
        self._lock = threading.RLock()
        self.learning_rate = learning_rate
        self.feedback_records: List[RoutingFeedback] = []
        self.intent_adjustments: Dict[Tuple[QueryIntent, QueryIntent], int] = defaultdict(int)

    def ingest_feedback(self, intent_id: str, signal: FeedbackSignal,
                        original: QueryIntent, corrected: Optional[QueryIntent] = None,
                        comment: str = ""):
        """摄入反馈。"""
        with self._lock:
            fb = RoutingFeedback(
                feedback_id=str(uuid.uuid4())[:8],
                intent_id=intent_id,
                signal=signal,
                original_intent=original,
                corrected_intent=corrected,
                user_comment=comment,
            )
            self.feedback_records.append(fb)

            # 记录纠偏
            if corrected and corrected != original:
                self.intent_adjustments[(original, corrected)] += 1

    def get_adjustment_weight(self, from_intent: QueryIntent, to_intent: QueryIntent) -> float:
        """获取从 from_intent 到 to_intent 的纠偏权重。"""
        count = self.intent_adjustments.get((from_intent, to_intent), 0)
        total = sum(self.intent_adjustments.get((from_intent, i), 0) for i in QueryIntent)
        if total == 0:
            return 0.0
        return count / total

    def generate_finetune_data(self) -> List[Dict[str, str]]:
        """生成 Qwen2.5 意图分类微调数据。"""
        with self._lock:
            data: List[Dict[str, str]] = []
            for fb in self.feedback_records:
                if fb.signal == FeedbackSignal.NEGATIVE and fb.corrected_intent:
                    data.append({
                        "instruction": "Classify the user query into one of: personal_knowledge, domain_knowledge, operation_command, exploratory",
                        "input": fb.original_intent.value,
                        "output": fb.corrected_intent.value,
                    })
            return data

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            signal_counts = defaultdict(int)
            for fb in self.feedback_records:
                signal_counts[fb.signal.value] += 1
            return {
                "total_feedback": len(self.feedback_records),
                "by_signal": dict(signal_counts),
                "adjustments": {f"{k[0].value}→{k[1].value}": v for k, v in self.intent_adjustments.items()},
                "finetune_samples": len(self.generate_finetune_data()),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P19-7 Query Intent Router",
        "benchmark": "2026 Multi-Partition Intent-Based Retrieval Router",
        "classes": 5,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "Intent Classify→Partition Route→Cross-Fuse→Conflict Resolve→Feedback Learn",
        "key_metric": "4-way intent routing with Qwen2.5 finetune interface & feedback correction",
        "thread_safe": True,
    }
