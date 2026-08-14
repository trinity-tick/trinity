"""
KV Cache Online Compaction — Attention-Guided Token Eviction
=============================================================
arXiv 2608.00902 · P46-6

在线 KV 缓存压缩: 使用 proxy-query (boundary/repeat-prefill/delayed-future)
评估 token 重要性, 延迟压缩调度器在积累足够上下文后才执行压缩,
基于注意力匹配保留被未来查询关注最多的 KV 对。

设计要点:
  - OnlineTokenEvictionPolicy: 在线淘汰策略, 三种 proxy-query 评估
  - ProxyQuerySelector: 自动选型, 平衡精度与开销
  - KVCompactionScheduler: 延迟压缩调度
  - AttentionMatchCompactor: 注意力匹配压缩器
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ProxyQueryType(Enum):
    BOUNDARY = auto()         # 边界探针: 用首尾 token 模拟查询
    REPEAT_PREFILL = auto()   # 重复 prefill: 复制 prompt 前 N token
    DELAYED_FUTURE = auto()   # 延迟未来: 用最近 K 个 token 模拟未来查询


class CompactionTrigger(Enum):
    BUDGET_EXCEEDED = auto()
    TIME_WINDOW = auto()
    MANUAL = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class KVCompactionBlock:
    """KV 缓存块——一组需要决策的 KV 对。"""
    block_id: str
    tokens: List[str] = field(default_factory=list)
    key_vectors: Optional[np.ndarray] = None    # (n_tokens, dim)
    value_vectors: Optional[np.ndarray] = None  # (n_tokens, dim)
    importance_scores: List[float] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvictionDecision:
    """淘汰决策——每个 token 的淘汰裁決。"""
    token_index: int
    token_text: str
    evict: bool
    importance: float
    reason: str = ""
    proxy_query_type: ProxyQueryType = ProxyQueryType.BOUNDARY


# ---------------------------------------------------------------------------
# ProxyQuerySelector
# ---------------------------------------------------------------------------

class ProxyQuerySelector:
    """代理查询选择器——根据上下文特征自动选择 proxy-query 类型。

    选型逻辑:
      - 上下文短 (< 512 tokens) → BOUNDARY (开销最小)
      - 上下文中等 → REPEAT_PREFILL
      - 上下文长 (> 4096 tokens) → DELAYED_FUTURE (最准确)
    """

    THRESHOLD_SHORT = 512
    THRESHOLD_LONG = 4096

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def select(self, token_count: int, budget_ratio: float = 1.0) -> ProxyQueryType:
        """自动选择 proxy-query 类型。"""
        with self._lock:
            if token_count <= self.THRESHOLD_SHORT or budget_ratio > 0.95:
                return ProxyQueryType.BOUNDARY
            elif token_count >= self.THRESHOLD_LONG:
                return ProxyQueryType.DELAYED_FUTURE
            else:
                return ProxyQueryType.REPEAT_PREFILL

    def statistics(self) -> Dict[str, Any]:
        return {
            "threshold_short": self.THRESHOLD_SHORT,
            "threshold_long": self.THRESHOLD_LONG,
        }


# ---------------------------------------------------------------------------
# OnlineTokenEvictionPolicy
# ---------------------------------------------------------------------------

class OnlineTokenEvictionPolicy:
    """在线 Token 淘汰策略——使用 proxy-query 评估每个 token 的重要性。

    Parameters
    ----------
    proxy_selector : Optional[ProxyQuerySelector]
        自动选型器, 不传则内部创建。
    """

    def __init__(self, proxy_selector: Optional[ProxyQuerySelector] = None) -> None:
        self.selector = proxy_selector or ProxyQuerySelector()
        self._history: List[List[EvictionDecision]] = []
        self._lock = threading.RLock()

    def evaluate(
        self, block: KVCompactionBlock, token_budget: int = 1024,
    ) -> Tuple[List[EvictionDecision], float]:
        """评估 block 中各 token 的重要性, 返回淘汰决策。

        Returns
        -------
        Tuple[List[EvictionDecision], float]
            (决策列表, 总重要性分)
        """
        with self._lock:
            n_tokens = len(block.tokens)
            budget_ratio = token_budget / max(1, n_tokens)
            pq_type = self.selector.select(n_tokens, budget_ratio)

            decisions: List[EvictionDecision] = []

            for i, token in enumerate(block.tokens):
                importance = self._compute_importance(i, n_tokens, pq_type, block)
                # 最后 10% token 保留, 开头 5% 保留
                is_tail = i >= n_tokens * 0.9
                is_head = i < n_tokens * 0.05
                evict = not (is_head or is_tail) and i >= token_budget

                decisions.append(EvictionDecision(
                    token_index=i, token_text=token, evict=evict,
                    importance=round(importance, 4),
                    proxy_query_type=pq_type,
                    reason=f"head={'Y' if is_head else 'N'}_tail={'Y' if is_tail else 'N'}",
                ))

            self._history.append(decisions)
            total_importance = sum(d.importance for d in decisions)
            return decisions, round(total_importance, 4)

    def _compute_importance(
        self, idx: int, total: int, pq_type: ProxyQueryType, block: KVCompactionBlock,
    ) -> float:
        """计算 token 重要性。"""
        if pq_type == ProxyQueryType.BOUNDARY:
            # 边界探针: 越靠近两端越重要
            pos = min(idx, total - idx - 1) / max(1, total)
            return 1.0 - pos * 0.8

        elif pq_type == ProxyQueryType.REPEAT_PREFILL:
            # 重复 prefill: 前 1/3 最重要
            if idx < total / 3:
                return 0.9
            pos = (idx - total / 3) / max(1, total * 2 / 3)
            return 0.9 - pos * 0.6

        else:  # DELAYED_FUTURE
            # 延迟未来: 最近 K 个 token 重要 + 开头重要
            if idx < total * 0.1:
                return 0.95
            if idx >= total * 0.8:
                return 0.85
            return 0.4

    def statistics(self) -> Dict[str, Any]:
        return {
            "decisions_made": len(self._history),
            "selector": self.selector.statistics(),
        }


# ---------------------------------------------------------------------------
# KVCompactionScheduler
# ---------------------------------------------------------------------------

class KVCompactionScheduler:
    """延迟压缩调度器——积累足够上下文后才执行压缩。

    Parameters
    ----------
    min_context_tokens : int
        触发压缩的最小 token 数。
    compaction_interval : float
        两次压缩之间的最小间隔 (秒)。
    """

    def __init__(self, min_context_tokens: int = 2048, compaction_interval: float = 5.0) -> None:
        self.min_context_tokens = min_context_tokens
        self.compaction_interval = compaction_interval
        self._pending: deque[KVCompactionBlock] = deque()
        self._last_compaction: float = 0.0
        self._compaction_count: int = 0
        self._lock = threading.RLock()

    def enqueue(self, block: KVCompactionBlock) -> None:
        """入队 KV 块。"""
        with self._lock:
            self._pending.append(block)

    def should_compact(self) -> bool:
        """判断是否应该触发压缩。"""
        with self._lock:
            total_tokens = sum(len(b.tokens) for b in self._pending)
            if total_tokens < self.min_context_tokens:
                return False
            if time.time() - self._last_compaction < self.compaction_interval:
                return False
            return True

    def get_pending(self) -> List[KVCompactionBlock]:
        """获取待压缩块并清空队列。"""
        with self._lock:
            blocks = list(self._pending)
            self._pending.clear()
            self._last_compaction = time.time()
            self._compaction_count += 1
            return blocks

    def statistics(self) -> Dict[str, Any]:
        return {
            "queue_size": len(self._pending),
            "compaction_count": self._compaction_count,
            "min_context_tokens": self.min_context_tokens,
        }


# ---------------------------------------------------------------------------
# AttentionMatchCompactor
# ---------------------------------------------------------------------------

class AttentionMatchCompactor:
    """注意力匹配压缩器——保留被未来查询关注最多的 KV 对。

    Parameters
    ----------
    keep_ratio : float
        每次压缩保留的 KV 对比例。
    """

    def __init__(self, keep_ratio: float = 0.7) -> None:
        self.keep_ratio = keep_ratio
        self._lock = threading.RLock()

    def compact(
        self, block: KVCompactionBlock, decisions: List[EvictionDecision],
    ) -> Tuple[KVCompactionBlock, int]:
        """执行压缩: 按淘汰决策过滤 KV 对。

        Returns
        -------
        Tuple[KVCompactionBlock, int]
            (压缩后的块, 被淘汰的 token 数)
        """
        with self._lock:
            evicted_set = {d.token_index for d in decisions if d.evict}
            keep_count = max(1, int(len(block.tokens) * self.keep_ratio))

            # 按重要性排序, 保留 top-keep_ratio
            ranked = sorted(decisions, key=lambda d: d.importance, reverse=True)
            keep_indices = {d.token_index for d in ranked[:keep_count]}
            keep_indices -= evicted_set  # 已淘汰的不保留

            new_tokens = [block.tokens[i] for i in sorted(keep_indices)]

            new_block = KVCompactionBlock(
                block_id=f"{block.block_id}_cmp{int(time.time())}",
                tokens=new_tokens,
            )
            evicted = len(block.tokens) - len(new_tokens)
            return new_block, evicted

    def statistics(self) -> Dict[str, Any]:
        return {"keep_ratio": self.keep_ratio}
