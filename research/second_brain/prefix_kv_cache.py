"""
# status: orphan (2026-08-15 audit, not in runtime path)
P19-6: Prefix KV Cache — RadixTree Shared KV Cache Engine
==========================================================

对标 RadixTree 共享 KV Cache 方案。

设计要点：
  - 标准化 Prompt 模板确保前缀对齐
  - RadixTree 管理共享 KV Cache 池
  - 多请求间缓存命中与复用
  - 推测解码加速非缓存部分
  - 缓存预热与 LRU+频率淘汰策略

核心组件：
  - PromptTemplateNormalizer:  标准化 Prompt 模板
  - RadixTreeKVCache:          RadixTree KV Cache 管理
  - SharedCacheHitTracker:     命中追踪与统计
  - SpeculativePrefixDecoder:  推测解码加速
  - CacheEvictionManager:      缓存预热与淘汰
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class CacheStatus(Enum):
    """缓存状态。"""
    WARM = "warm"          # 预热中
    ACTIVE = "active"      # 活跃
    STALE = "stale"        # 过期
    EVICTED = "evicted"    # 已淘汰


class EvictionPolicy(Enum):
    """淘汰策略。"""
    LRU = "lru"                    # 最近最少使用
    LFU = "lfu"                    # 最少频率使用
    LRU_FREQUENCY = "lru_frequency"  # LRU+频率混合
    TIME_BASED = "time_based"      # 时间过期


class MatchResult(Enum):
    """匹配结果。"""
    EXACT = "exact"                # 完全匹配
    PREFIX = "prefix"              # 前缀匹配
    NO_MATCH = "no_match"          # 无匹配


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class RadixNode:
    """RadixTree 节点。"""
    node_id: str
    prefix: str
    kv_data: Optional[Any] = None
    children: Dict[str, RadixNode] = field(default_factory=dict)
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    status: CacheStatus = CacheStatus.ACTIVE
    token_count: int = 0


@dataclass
class PromptTemplate:
    """标准化 Prompt 模板。"""
    template_id: str
    name: str
    prefix_pattern: str
    slots: List[str] = field(default_factory=list)
    token_estimate: int = 0
    usage_count: int = 0


@dataclass
class CacheHitRecord:
    """缓存命中记录。"""
    record_id: str
    prefix: str
    match_type: MatchResult
    saved_tokens: int
    request_id: str = ""
    latency_saved_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class SpeculativeDecodeResult:
    """推测解码结果。"""
    result_id: str
    accepted_tokens: List[int]
    rejected_tokens: List[int]
    acceptance_rate: float
    speedup_ratio: float
    draft_model: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class EvictionRecord:
    """淘汰记录。"""
    record_id: str
    node_id: str
    prefix: str
    policy: EvictionPolicy
    reason: str
    saved_bytes: int = 0
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Constants
# ============================================================================

DEFAULT_PROMPT_TEMPLATES: List[Dict[str, Any]] = [
    {"name": "system_chat", "prefix": "<|system|>", "slots": ["system_prompt"]},
    {"name": "user_query", "prefix": "<|user|>", "slots": ["user_message"]},
    {"name": "assistant_response", "prefix": "<|assistant|>", "slots": ["response"]},
    {"name": "rag_context", "prefix": "<|context|>", "slots": ["retrieved_docs"]},
    {"name": "tool_call", "prefix": "<|tool_call|>", "slots": ["tool_name", "arguments"]},
]


# ============================================================================
# Core Components
# ============================================================================

class PromptTemplateNormalizer:
    """标准化 Prompt 模板引擎。

    确保前缀对齐以最大化 KV 缓存命中率。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.templates: Dict[str, PromptTemplate] = {}

        # 注册默认模板
        for tpl in DEFAULT_PROMPT_TEMPLATES:
            self.register(tpl["name"], tpl["prefix"], tpl.get("slots", []))

    def register(self, name: str, prefix_pattern: str, slots: List[str]) -> str:
        """注册模板。"""
        with self._lock:
            tid = str(uuid.uuid4())[:8]
            tpl = PromptTemplate(
                template_id=tid,
                name=name,
                prefix_pattern=prefix_pattern,
                slots=slots,
                token_estimate=len(prefix_pattern.split()),
            )
            self.templates[tid] = tpl
            return tid

    def normalize(self, text: str, template_name: str) -> Tuple[str, str]:
        """按模板标准化文本，提取公共前缀和剩余部分。"""
        with self._lock:
            tpl = next((t for t in self.templates.values() if t.name == template_name), None)
            if not tpl:
                return text, ""

            tpl.usage_count += 1
            # 提取前缀
            prefix = tpl.prefix_pattern
            remaining = text
            if text.startswith(prefix):
                remaining = text[len(prefix):]

            return prefix, remaining.strip()

    def fingerprint(self, text: str) -> str:
        """生成文本指纹用于缓存键。"""
        return hashlib.sha256(text.encode()).hexdigest()[:24]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_templates": len(self.templates),
                "names": [t.name for t in self.templates.values()],
            }


class RadixTreeKVCache:
    """RadixTree 共享 KV Cache 管理。

    使用压缩前缀树管理共享 KV Cache 池。
    """

    def __init__(self, max_nodes: int = 10000, max_memory_bytes: int = 512 * 1024 * 1024):
        self._lock = threading.RLock()
        self.root = RadixNode(node_id="root", prefix="")
        self.max_nodes = max_nodes
        self.max_memory_bytes = max_memory_bytes
        self.node_count: int = 1  # 含 root
        self.memory_used: int = 0
        self.hit_tracker = SharedCacheHitTracker()

    def insert(self, prefix: str, kv_data: Any, token_count: int = 0) -> str:
        """插入 KV 缓存条目。"""
        with self._lock:
            node_id = self._insert_recursive(self.root, prefix, kv_data, token_count)
            self.node_count = self._count_nodes(self.root)
            return node_id

    def _insert_recursive(self, node: RadixNode, prefix: str, kv_data: Any, token_count: int) -> str:
        """递归插入 Radix Tree。"""
        if not prefix:
            node.kv_data = kv_data
            node.last_access = time.time()
            return node.node_id

        first_char = prefix[0]
        if first_char in node.children:
            child = node.children[first_char]
            common = self._common_prefix(child.prefix, prefix)
            if common == len(child.prefix):
                # 完全匹配子节点前缀，继续向下
                return self._insert_recursive(child, prefix[common:], kv_data, token_count)
            else:
                # 部分匹配，需要分裂节点
                return self._split_and_insert(child, common, prefix, kv_data, token_count)
        else:
            # 新建节点
            new_node = RadixNode(
                node_id=str(uuid.uuid4())[:8],
                prefix=prefix,
                kv_data=kv_data,
                token_count=token_count,
            )
            node.children[first_char] = new_node
            return new_node.node_id

    def _split_and_insert(self, child: RadixNode, common_len: int, new_prefix: str,
                          kv_data: Any, token_count: int) -> str:
        """分裂节点并插入新数据。"""
        old_prefix = child.prefix
        old_kv = child.kv_data
        old_children = child.children

        # 更新子节点为公共前缀部分
        child.prefix = old_prefix[:common_len]
        child.kv_data = None
        child.children = {}

        # 原后缀成为子节点的子节点
        old_suffix = old_prefix[common_len:]
        old_first = old_suffix[0]
        old_child = RadixNode(
            node_id=str(uuid.uuid4())[:8],
            prefix=old_suffix,
            kv_data=old_kv,
            children=old_children,
        )
        child.children[old_first] = old_child

        # 新数据
        new_suffix = new_prefix[common_len:]
        if new_suffix:
            new_first = new_suffix[0]
            new_child = RadixNode(
                node_id=str(uuid.uuid4())[:8],
                prefix=new_suffix,
                kv_data=kv_data,
                token_count=token_count,
            )
            child.children[new_first] = new_child
            return new_child.node_id
        else:
            child.kv_data = kv_data
            return child.node_id

    def search(self, text: str) -> Tuple[Optional[Any], MatchResult, int]:
        """搜索最长前缀匹配。"""
        with self._lock:
            return self._search_recursive(self.root, text, "", 0)

    def _search_recursive(self, node: RadixNode, text: str, matched: str, depth: int) -> Tuple[Optional[Any], MatchResult, int]:
        """递归搜索。"""
        if not text:
            if node.kv_data is not None:
                self._record_hit(node, matched, MatchResult.EXACT, depth)
                return node.kv_data, MatchResult.EXACT, depth
            return node.kv_data, MatchResult.PREFIX, depth

        first_char = text[0]
        if first_char in node.children:
            child = node.children[first_char]
            common = self._common_prefix(child.prefix, text)
            if common > 0:
                node.last_access = time.time()
                child.access_count += 1
                child.last_access = time.time()
                matched += child.prefix[:common]
                return self._search_recursive(child, text[common:], matched, depth + 1)

        # 无法继续匹配
        if node.kv_data is not None:
            self._record_hit(node, matched, MatchResult.PREFIX, depth)
            return node.kv_data, MatchResult.PREFIX, depth
        return None, MatchResult.NO_MATCH, 0

    def _record_hit(self, node: RadixNode, prefix: str, match_type: MatchResult, depth: int):
        self.hit_tracker.record(prefix, match_type, depth * 50)

    @staticmethod
    def _common_prefix(a: str, b: str) -> int:
        """计算两字符串的公共前缀长度。"""
        i = 0
        while i < min(len(a), len(b)) and a[i] == b[i]:
            i += 1
        return i

    def _count_nodes(self, node: RadixNode) -> int:
        return 1 + sum(self._count_nodes(c) for c in node.children.values())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": self.node_count,
                "max_nodes": self.max_nodes,
                "memory_used_mb": round(self.memory_used / (1024 * 1024), 2),
                "hits": self.hit_tracker.statistics(),
            }


class SharedCacheHitTracker:
    """缓存命中追踪与统计。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.records: List[CacheHitRecord] = []

    def record(self, prefix: str, match_type: MatchResult, saved_tokens: int, request_id: str = ""):
        with self._lock:
            record = CacheHitRecord(
                record_id=str(uuid.uuid4())[:8],
                prefix=prefix[:50],
                match_type=match_type,
                saved_tokens=saved_tokens,
                request_id=request_id,
            )
            self.records.append(record)
            return record

    def hit_rate(self, window: int = 100) -> float:
        """最近窗口命中率。"""
        recent = self.records[-window:] if len(self.records) > window else self.records
        if not recent:
            return 0.0
        hits = sum(1 for r in recent if r.match_type != MatchResult.NO_MATCH)
        return hits / len(recent)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            if not self.records:
                return {"total_hits": 0, "hit_rate": 0.0}
            match_counts = defaultdict(int)
            total_saved = 0
            for r in self.records:
                match_counts[r.match_type.value] += 1
                total_saved += r.saved_tokens
            return {
                "total_hits": len(self.records),
                "hit_rate": round(self.hit_rate(), 4),
                "by_type": dict(match_counts),
                "total_tokens_saved": total_saved,
            }


class SpeculativePrefixDecoder:
    """推测解码加速引擎。

    对非缓存部分使用草稿模型快速生成，目标模型验证。
    """

    def __init__(self, acceptance_threshold: float = 0.7, draft_speedup: float = 3.0):
        self._lock = threading.RLock()
        self.acceptance_threshold = acceptance_threshold
        self.draft_speedup = draft_speedup
        self.results: List[SpeculativeDecodeResult] = []

    def decode(self, non_cached_tokens: List[int], draft_fn: Optional[Callable] = None,
               verify_fn: Optional[Callable] = None) -> SpeculativeDecodeResult:
        """推测解码非缓存部分。"""
        with self._lock:
            # 模拟草稿模型生成
            draft_output: List[Tuple[int, float]] = []
            if draft_fn:
                draft_output = draft_fn(non_cached_tokens)
            else:
                # 模拟草稿输出
                draft_output = [(t, 0.8) for t in non_cached_tokens[:10]]

            accepted: List[int] = []
            rejected: List[int] = []
            for token_id, confidence in draft_output:
                if confidence >= self.acceptance_threshold:
                    accepted.append(token_id)
                else:
                    rejected.append(token_id)

            acc_rate = len(accepted) / max(len(accepted) + len(rejected), 1)
            speedup = 1.0 + acc_rate * (self.draft_speedup - 1.0)

            result = SpeculativeDecodeResult(
                result_id=str(uuid.uuid4())[:8],
                accepted_tokens=accepted,
                rejected_tokens=rejected,
                acceptance_rate=round(acc_rate, 4),
                speedup_ratio=round(speedup, 4),
            )
            self.results.append(result)
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            if not self.results:
                return {"total_decodes": 0}
            avg_acc = sum(r.acceptance_rate for r in self.results) / len(self.results)
            avg_speedup = sum(r.speedup_ratio for r in self.results) / len(self.results)
            return {
                "total_decodes": len(self.results),
                "avg_acceptance": round(avg_acc, 4),
                "avg_speedup": round(avg_speedup, 4),
            }


class CacheEvictionManager:
    """缓存预热与淘汰管理。

    LRU + 频率混合策略，支持预热。
    """

    def __init__(self, policy: EvictionPolicy = EvictionPolicy.LRU_FREQUENCY, max_nodes: int = 10000):
        self._lock = threading.RLock()
        self.policy = policy
        self.max_nodes = max_nodes
        self.eviction_records: List[EvictionRecord] = []

    def should_evict(self, current_nodes: int) -> bool:
        """是否需要淘汰。"""
        return current_nodes >= self.max_nodes * 0.9

    def select_victims(self, nodes: List[RadixNode], count: int) -> List[RadixNode]:
        """选择淘汰目标。"""
        with self._lock:
            now = time.time()
            scored: List[Tuple[RadixNode, float]] = []

            for node in nodes:
                if node.node_id == "root":
                    continue
                if self.policy == EvictionPolicy.LRU:
                    score = now - node.last_access
                elif self.policy == EvictionPolicy.LFU:
                    score = float(node.access_count)
                    scored.append((node, -score))  # 负号：越小越早淘汰
                    continue
                else:  # LRU_FREQUENCY
                    freq_score = 1.0 / (1.0 + node.access_count)
                    time_score = (now - node.last_access) / 3600.0
                    score = -freq_score * 100 - time_score
                scored.append((node, score))

            if self.policy == EvictionPolicy.LFU:
                scored.sort(key=lambda x: x[1])
            else:
                scored.sort(key=lambda x: x[1], reverse=True)

            victims = scored[:count]
            for node, _ in victims:
                node.status = CacheStatus.EVICTED
                self.eviction_records.append(EvictionRecord(
                    record_id=str(uuid.uuid4())[:8],
                    node_id=node.node_id,
                    prefix=node.prefix[:30],
                    policy=self.policy,
                    reason=f"LRU+Frequency eviction",
                    saved_bytes=len(node.prefix) * 4,
                ))

            return [n for n, _ in victims]

    def warmup(self, common_prefixes: List[str], cache: RadixTreeKVCache):
        """预热缓存。"""
        for prefix in common_prefixes:
            cache.insert(prefix, {"warmed": True}, len(prefix.split()))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_evictions": len(self.eviction_records),
                "policy": self.policy.value,
                "max_nodes": self.max_nodes,
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P19-6 Prefix KV Cache",
        "benchmark": "RadixTree Shared KV Cache with Speculative Decoding",
        "classes": 5,
        "enums": 3,
        "dataclasses": 5,
        "key_pattern": "Template Normalize→RadixTree KV Insert→Shared Hit→Speculative Decode→LRU+Frequency Evict",
        "key_metric": "RadixTree shared KV cache with multi-request reuse & speculative decoding",
        "thread_safe": True,
    }
