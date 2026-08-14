"""
ContextLifecycleManager — Agentic Context Management (Maximem)
===============================================================
arXiv 2607.21503 · P43-4

实现上下文生命周期管理: 决策记忆什么→提取结构化→按数据类型选存储→
保留/丢弃/总结/归档, 跨轮次主动管理心智信息。

设计要点:
  - DecisionEngine: 决策记忆什么 (保留/丢弃/总结/归档)
  - ContextExtractor: 提取结构化上下文
  - ContextStorer: 按数据类型选存储方式
  - LifecyclePolicy: 生命周期策略引擎
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class StorageType(Enum):
    """存储类型——按数据类型选择。"""
    VECTOR = auto()
    KEY_VALUE = auto()
    GRAPH = auto()
    RAW_LOG = auto()
    SESSION_CACHE = auto()


class ArchiveStrategy(Enum):
    """归档策略。"""
    RETAIN = auto()     # 保留在活跃上下文
    SUMMARIZE = auto()  # 压缩为摘要
    DISCARD = auto()    # 丢弃
    ARCHIVE = auto()    # 移至长期存储


class ContextRetentionPolicy(Enum):
    """上下文保留策略。"""
    KEEP_LAST_N = auto()
    TIME_BASED = auto()
    IMPORTANCE_BASED = auto()
    HYBRID = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MentalContext:
    """心智上下文——Agent的当前工作记忆状态。"""
    context_id: str
    session_id: str
    items: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    importance_map: Dict[str, float] = field(default_factory=dict)
    token_estimate: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class ContextDecision:
    """上下文决策——对单条信息的处理决定。"""
    decision_id: str
    content_id: str
    strategy: ArchiveStrategy
    reason: str = ""
    storage_type: Optional[StorageType] = None
    confidence: float = 0.5
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DecisionEngine
# ---------------------------------------------------------------------------

class DecisionEngine:
    """决策引擎——决定记忆什么: 保留/丢弃/总结/归档。

    Parameters
    ----------
    max_context_tokens : int
        活跃上下文最大令牌数。
    importance_threshold : float
        重要性阈值, 低于此值可丢弃。
    """

    def __init__(self, max_context_tokens: int = 8000, importance_threshold: float = 0.3) -> None:
        self.max_context_tokens = max_context_tokens
        self.importance_threshold = importance_threshold
        self._decision_log: deque = deque(maxlen=300)
        self._lock = threading.RLock()

    def decide(
        self, item: Dict[str, Any], current_context: MentalContext
    ) -> ContextDecision:
        """对单条信息做出生命周期决策。"""
        with self._lock:
            importance = self._assess_importance(item)
            age = time.time() - item.get("timestamp", time.time())
            recency_bonus = max(0, 1.0 - age / 86400.0)  # 24h 内衰减

            adjusted_importance = importance * (0.7 + 0.3 * recency_bonus)

            # 决策逻辑
            if adjusted_importance < 0.2:
                strategy = ArchiveStrategy.DISCARD
                reason = "Low importance and stale"
                storage = None
            elif adjusted_importance < self.importance_threshold:
                strategy = ArchiveStrategy.SUMMARIZE
                reason = "Moderate importance — summarize for space"
                storage = StorageType.SESSION_CACHE
            elif current_context.token_estimate > self.max_context_tokens:
                strategy = ArchiveStrategy.ARCHIVE
                reason = f"Context overflow ({current_context.token_estimate} tokens) — archive to long-term"
                storage = StorageType.VECTOR
            else:
                strategy = ArchiveStrategy.RETAIN
                reason = "High importance — retain in active context"
                storage = StorageType.RAW_LOG

            decision = ContextDecision(
                decision_id=f"dec_{int(time.time()*1e6)}",
                content_id=item.get("id", ""),
                strategy=strategy,
                reason=reason,
                storage_type=storage,
                confidence=adjusted_importance,
            )

            self._decision_log.append(decision)
            return decision

    def _assess_importance(self, item: Dict[str, Any]) -> float:
        """评估信息重要性 (0-1)。"""
        score = 0.3  # 基础分

        content = str(item.get("content", ""))
        # 含关键信息词 → 加分
        critical_kw = {"error", "failure", "critical", "important", "deadline", "blocker", "security"}
        for kw in critical_kw:
            if kw in content.lower():
                score += 0.15

        # 长度适中的信息更有价值
        clen = len(content)
        if 50 <= clen <= 500:
            score += 0.1
        elif clen > 2000:
            score -= 0.05

        return min(1.0, max(0.0, score))

    def statistics(self) -> Dict[str, Any]:
        return {"total_decisions": len(self._decision_log)}


# ---------------------------------------------------------------------------
# ContextExtractor
# ---------------------------------------------------------------------------

class ContextExtractor:
    """提取结构化上下文——从原始数据提取实体/关系/摘要。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def extract(self, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """提取结构化上下文。

        Returns
        -------
        Dict[str, Any]
            {entities, relationships, key_facts, summary}
        """
        with self._lock:
            entities: List[Dict[str, Any]] = []
            key_facts: List[str] = []
            all_text = " ".join(str(i.get("content", "")) for i in items)

            # 实体提取 (大写词)
            import re
            entity_matches = re.findall(r'\b([A-Z][a-z]+(?:\s[A-Z][a-z]+)*)\b', all_text)
            seen_entities: Set[str] = set()
            for em in entity_matches:
                if em not in seen_entities and len(em) > 3:
                    seen_entities.add(em)
                    entities.append({"name": em, "type": "named_entity"})

            # 关键事实 (含数值或关键词的句子)
            sentences = re.split(r'[.!?]+', all_text)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                has_number = bool(re.search(r'\d+', sent))
                has_keyword = any(
                    kw in sent.lower()
                    for kw in ("result", "decision", "action", "config", "error", "update")
                )
                if has_number or has_keyword:
                    key_facts.append(sent[:120])

            return {
                "entities": entities[:20],
                "key_facts": key_facts[:10],
                "summary": f"Extracted {len(entities)} entities and {len(key_facts)} key facts",
            }

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# ContextStorer
# ---------------------------------------------------------------------------

class ContextStorer:
    """按数据类型选择存储方式。

    Parameters
    ----------
    vector_capacity : int
        向量存储容量。
    kv_capacity : int
        键值存储容量。
    """

    def __init__(self, vector_capacity: int = 1000, kv_capacity: int = 500) -> None:
        self.vector_capacity = vector_capacity
        self.kv_capacity = kv_capacity
        self._vector_store: Dict[str, Dict[str, Any]] = {}
        self._kv_store: Dict[str, Any] = {}
        self._graph_store: Dict[str, List[Tuple[str, str, str]]] = defaultdict(list)
        self._raw_logs: deque = deque(maxlen=2000)
        self._session_cache: Dict[str, Any] = {}
        self._lock = threading.RLock()

    def store(self, item: Dict[str, Any], storage_type: StorageType) -> bool:
        """按指定类型存储。"""
        with self._lock:
            item_id = item.get("id", f"item_{int(time.time()*1e6)}")

            if storage_type == StorageType.VECTOR:
                if len(self._vector_store) < self.vector_capacity:
                    self._vector_store[item_id] = item
                    return True
                return False

            if storage_type == StorageType.KEY_VALUE:
                if len(self._kv_store) < self.kv_capacity:
                    self._kv_store[item_id] = item
                    return True
                return False

            if storage_type == StorageType.GRAPH:
                # 简单图存储: (entity, relation, entity_from_content)
                content = str(item.get("content", ""))
                import re
                caps = re.findall(r'\b([A-Z][a-z]+)\b', content)
                for i in range(len(caps) - 1):
                    self._graph_store["default"].append((caps[i], "related_to", caps[i + 1]))
                return True

            if storage_type == StorageType.RAW_LOG:
                self._raw_logs.append(item)
                return True

            if storage_type == StorageType.SESSION_CACHE:
                self._session_cache[item_id] = item
                return True

            return False

    def retrieve(self, storage_type: StorageType, limit: int = 10) -> List[Dict[str, Any]]:
        """从指定存储检索。"""
        if storage_type == StorageType.VECTOR:
            return list(self._vector_store.values())[-limit:]
        if storage_type == StorageType.KEY_VALUE:
            return list(self._kv_store.values())[-limit:]
        if storage_type == StorageType.RAW_LOG:
            return list(self._raw_logs)[-limit:]
        if storage_type == StorageType.SESSION_CACHE:
            return list(self._session_cache.values())[-limit:]
        return []

    def statistics(self) -> Dict[str, Any]:
        return {
            "vector": len(self._vector_store),
            "kv": len(self._kv_store),
            "graph_edges": sum(len(v) for v in self._graph_store.values()),
            "raw_logs": len(self._raw_logs),
            "session_cache": len(self._session_cache),
        }


# ---------------------------------------------------------------------------
# LifecyclePolicy
# ---------------------------------------------------------------------------

class LifecyclePolicy:
    """生命周期策略引擎——跨轮次主动管理心智信息。

    Parameters
    ----------
    retention_policy : ContextRetentionPolicy
        保留策略。
    max_retained : int
        最大保留条目数。
    ttl_seconds : float
        时间基准TTL。
    """

    def __init__(
        self,
        retention_policy: ContextRetentionPolicy = ContextRetentionPolicy.HYBRID,
        max_retained: int = 50,
        ttl_seconds: float = 86400.0,
    ) -> None:
        self.retention_policy = retention_policy
        self.max_retained = max_retained
        self.ttl_seconds = ttl_seconds
        self._lock = threading.RLock()

    def apply(self, contexts: Dict[str, MentalContext]) -> Dict[str, MentalContext]:
        """应用生命周期策略, 清理过期/低价值上下文。"""
        with self._lock:
            now = time.time()
            retained: Dict[str, MentalContext] = {}

            sorted_contexts = sorted(
                contexts.items(),
                key=lambda x: x[1].updated_at,
                reverse=True,
            )

            for cid, ctx in sorted_contexts:
                if len(retained) >= self.max_retained:
                    break

                if self.retention_policy == ContextRetentionPolicy.KEEP_LAST_N:
                    retained[cid] = ctx
                    continue

                if self.retention_policy == ContextRetentionPolicy.TIME_BASED:
                    if now - ctx.updated_at <= self.ttl_seconds:
                        retained[cid] = ctx
                    continue

                if self.retention_policy == ContextRetentionPolicy.IMPORTANCE_BASED:
                    avg_importance = (
                        sum(ctx.importance_map.values()) / max(len(ctx.importance_map), 1)
                        if ctx.importance_map else 0.3
                    )
                    if avg_importance > 0.2:
                        retained[cid] = ctx
                    continue

                # HYBRID: 最近 + 重要
                if self.retention_policy == ContextRetentionPolicy.HYBRID:
                    age = now - ctx.updated_at
                    avg_importance = (
                        sum(ctx.importance_map.values()) / max(len(ctx.importance_map), 1)
                        if ctx.importance_map else 0.3
                    )
                    score = avg_importance * (1.0 - min(age / self.ttl_seconds, 1.0))
                    if score > 0.15:
                        retained[cid] = ctx
                    continue

                retained[cid] = ctx

            logger.info(
                "Lifecycle applied: %d → %d contexts (policy=%s)",
                len(contexts), len(retained), self.retention_policy.name,
            )
            return retained

    def statistics(self) -> Dict[str, Any]:
        return {
            "policy": self.retention_policy.name,
            "max_retained": self.max_retained,
            "ttl_hours": round(self.ttl_seconds / 3600, 1),
        }


# ---------------------------------------------------------------------------
# ContextLifecycleManager
# ---------------------------------------------------------------------------

class ContextLifecycleManager:
    """Agentic Context Management (Maximem) 上下文生命周期管理器。

    Parameters
    ----------
    max_context_tokens : int
        活跃上下文最大令牌数。
    importance_threshold : float
        重要性阈值。
    retention_policy : ContextRetentionPolicy
        保留策略。
    """

    def __init__(
        self,
        max_context_tokens: int = 8000,
        importance_threshold: float = 0.3,
        retention_policy: ContextRetentionPolicy = ContextRetentionPolicy.HYBRID,
    ) -> None:
        self.decision_engine = DecisionEngine(
            max_context_tokens=max_context_tokens,
            importance_threshold=importance_threshold,
        )
        self.context_extractor = ContextExtractor()
        self.context_storer = ContextStorer()
        self.lifecycle_policy = LifecyclePolicy(
            retention_policy=retention_policy,
        )
        self._contexts: Dict[str, MentalContext] = {}
        self._lock = threading.RLock()
        self._ctx_count: int = 0

        logger.info(
            "ContextLifecycleManager initialized [max_tokens=%d imp=%.2f policy=%s]",
            max_context_tokens, importance_threshold, retention_policy.name,
        )

    def create_context(self, session_id: str) -> MentalContext:
        """创建新的心智上下文。"""
        with self._lock:
            self._ctx_count += 1
            ctx = MentalContext(
                context_id=f"ctx_{self._ctx_count}_{int(time.time()*1e6)}",
                session_id=session_id,
            )
            self._contexts[ctx.context_id] = ctx
            return ctx

    def add_item(
        self, context_id: str, content: str, metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[ContextDecision]:
        """向上下文添加信息并触发生命周期决策。"""
        ctx = self._contexts.get(context_id)
        if not ctx:
            return None

        item = {
            "id": f"item_{len(ctx.items)}_{int(time.time()*1e6)}",
            "content": content,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }

        decision = self.decision_engine.decide(item, ctx)

        if decision.strategy == ArchiveStrategy.DISCARD:
            return decision

        if decision.strategy == ArchiveStrategy.RETAIN:
            ctx.items.append(item)
        elif decision.strategy == ArchiveStrategy.SUMMARIZE:
            # 存储摘要到session cache
            extracted = self.context_extractor.extract([item])
            item["summary"] = extracted.get("summary", "")
            ctx.items.append(item)
        elif decision.strategy == ArchiveStrategy.ARCHIVE:
            # 归档到长期存储
            if decision.storage_type:
                item["archived"] = True
                self.context_storer.store(item, decision.storage_type)
                ctx.items.append(item)

        # 更新重要性映射
        ctx.importance_map[item["id"]] = decision.confidence
        ctx.token_estimate = sum(len(str(i.get("content", ""))) // 4 for i in ctx.items)
        ctx.updated_at = time.time()

        return decision

    def summarize_context(self, context_id: str) -> Dict[str, Any]:
        """提取上下文的整体摘要。"""
        ctx = self._contexts.get(context_id)
        if not ctx:
            return {"error": "Context not found"}

        extracted = self.context_extractor.extract(ctx.items)
        ctx.summary = extracted.get("summary", "")
        return extracted

    def lifecycle_gc(self) -> Dict[str, Any]:
        """执行生命周期垃圾回收。"""
        before = len(self._contexts)
        self._contexts = self.lifecycle_policy.apply(self._contexts)
        after = len(self._contexts)
        return {"before": before, "after": after, "removed": before - after}

    def get_context(self, context_id: str) -> Optional[MentalContext]:
        return self._contexts.get(context_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_contexts": len(self._contexts),
                "decisions": self.decision_engine.statistics()["total_decisions"],
                "storage": self.context_storer.statistics(),
                "lifecycle": self.lifecycle_policy.statistics(),
            }
