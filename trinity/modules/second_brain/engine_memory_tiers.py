# engine_memory_tiers — P82-P83: Multi-Head + Three-Layer Hierarchical Memory
# Auto-generated during engine_core.py split refactoring
# status: active (2026-09 EXECUTION 172: 大脑方向激活) (2026-09 EXECUTION 163)

from __future__ import annotations
import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime

SEP = "=" * 80; SUB = "-" * 60; VERSION = "v6.50"

from .engine_core_types import (
    ContextAction, MemoryHead, ExactKVEntry, ContinuityState,
    ConsolidationRecord, ConsolidationPhase,
)
from .engine_governance import MultiHeadRecurrentMemory

class MultiHeadMemoryPartition:
    """
    M105: MultiHeadMemoryPartition — 多头记忆分区 (MHM-LRU 策略)
    论文: MHM: Multi-Head Memory (arXiv:2607.01523), P82

    核心: 多 head 独立分区，select-then-update 门控

    特性:
    - 多 head 分区: 默认 8 head，每个独立维护内容
    - select-then-update: 每步仅选一个 head 更新 (MHM-LRU)
    - 其余 head 架构级屏蔽覆写: 非选中 head 的写入被拦截
    - retention_rate 监控: 追踪每 head 的保持率

    与 M40 (MultiHeadRecurrentMemory) 的区别:
    - M40: 通用多 head 读写
    - M105: 精细化分区 + select-then-update 门控 + 屏蔽机制
    """

    def __init__(self, num_heads: int = 8, partition_capacity: int = 256):
        self.num_heads = num_heads
        self.partition_capacity = partition_capacity

        # 独立分区: 每个 head 有独立的内容存储
        self.partitions: dict[int, OrderedDict[str, Any]] = {
            i: OrderedDict() for i in range(num_heads)
        }

        # MHM-LRU: select-then-update 追踪器
        self.lru_queue: list[int] = []
        self.selected_head: Optional[int] = None

        # 屏蔽状态: 非选中 head 的写入被屏蔽
        self.shielded_heads: set[int] = set()

        # retention_rate 监控
        self.head_retention: dict[int, dict] = {
            i: {"writes": 0, "overwrites": 0, "retention_rate": 1.0}
            for i in range(num_heads)
        }

        # 统计
        self.total_updates: int = 0
        self.blocked_writes: int = 0

    def select_head(self) -> int:
        """MHM-LRU: 选择一个 head 用于写入"""
        # 找到最少更新次数的 head (LRU 语义)
        if len(self.lru_queue) < self.num_heads:
            head_id = len(self.lru_queue)
        else:
            # 选择 retention_rate 最高的 head (保持最多的不更新)
            sorted_heads = sorted(
                range(self.num_heads),
                key=lambda h: self.head_retention[h]["retention_rate"],
                reverse=True
            )
            head_id = sorted_heads[0]

        # 更新 LRU 队列
        if head_id in self.lru_queue:
            self.lru_queue.remove(head_id)
        self.lru_queue.append(head_id)

        self.selected_head = head_id
        return head_id

    def update(self, key: str, content: Any) -> dict:
        """
        select-then-update:
        1. 选择目标 head
        2. 仅更新选定 head 的分区
        3. 其余 head 写入被屏蔽
        """
        head_id = self.select_head()

        # 屏蔽所有非选中 head 的写入
        self.shielded_heads = set(range(self.num_heads)) - {head_id}

        # 检查分区容量
        partition = self.partitions[head_id]
        if key in partition:
            # 覆盖 → overwrite
            self.head_retention[head_id]["overwrites"] += 1
            partition.move_to_end(key)
        elif len(partition) >= self.partition_capacity:
            # LRU 淘汰
            partition.popitem(last=False)

        partition[key] = content
        partition.move_to_end(key)

        self.head_retention[head_id]["writes"] += 1
        self.total_updates += 1

        # 更新 retention_rate
        self._update_retention_rate(head_id)

        return {
            "selected_head": head_id,
            "shielded_heads": list(self.shielded_heads),
            "partition_size": len(partition),
        }

    def is_write_blocked(self, head_id: int) -> bool:
        """检查 head 是否被架构级屏蔽"""
        return head_id in self.shielded_heads

    def read_head(self, head_id: int) -> OrderedDict[str, Any]:
        """读取指定 head 的分区内容"""
        return self.partitions.get(head_id, OrderedDict())

    def read_all(self) -> dict[int, OrderedDict]:
        """读取所有 head 分区 (屏蔽 head 也返回)"""
        return self.partitions

    def _update_retention_rate(self, head_id: int):
        """更新每 head 的保持率"""
        stats = self.head_retention[head_id]
        total = stats["writes"]
        overwrites = stats["overwrites"]
        if total > 0:
            stats["retention_rate"] = 1.0 - (overwrites / total)

    def get_retention_report(self) -> dict:
        """获取所有 head 的 retention_rate 报告"""
        return {
            f"head_{i}": {
                "writes": self.head_retention[i]["writes"],
                "overwrites": self.head_retention[i]["overwrites"],
                "retention_rate": f"{self.head_retention[i]['retention_rate'] * 100:.2f}%",
            }
            for i in range(self.num_heads)
        }

    def diagnostics(self) -> dict:
        report = self.get_retention_report()
        avg_retention = statistics.mean(
            [self.head_retention[i]["retention_rate"] for i in range(self.num_heads)]
        )
        return {
            "num_heads": self.num_heads,
            "partition_capacity": self.partition_capacity,
            "total_updates": self.total_updates,
            "blocked_writes": self.blocked_writes,
            "avg_retention_rate": f"{avg_retention * 100:.2f}%",
            "head_report": report,
        }

print("[P82] MultiHeadMemoryPartition (M105) initialized")


# ============ M106: ThreeLayerHierarchicalMemory [NEW, P83] ============

class ThreeLayerHierarchicalMemory:
    """
    M106: ThreeLayerHierarchicalMemory — 三层分层记忆
    论文: Ensemble QSP: Query-Specific Partitioning (arXiv:2607.07666), P83

    三层结构:
    1. short_term: active buffer (循环缓冲区, 无容量上限但 LRU)
    2. mid_term: project state (上限 4096 token)
       - 按类别设上限
       - 已完成任务驱逐到 long_term
    3. long_term: archived (持久化归档)

    关键属性:
    - 逐层驱逐: mid_term 超过类别上限时驱逐到 long_term
    - 跨会话恒定上下文: 确保 mid_term 注入量有界 (≤4096 token)
    - 查询时按层级优先级检索: short → mid → long
    """

    MID_TERM_TOKEN_LIMIT: int = 4096

    def __init__(self, short_capacity: int = 32, mid_token_limit: int = 4096):
        self.short_capacity = short_capacity
        self.mid_token_limit = mid_token_limit

        # 三层结构
        self.short_term: deque = deque(maxlen=short_capacity)       # active buffer
        self.mid_term: dict[str, list[dict]] = {}   # project state (by category)
        self.long_term: dict[str, list[dict]] = {}   # archived

        # mid_term 类别上限 (token-based)
        self.mid_term_category_limits: dict[str, int] = {}

        # 统计
        self.evictions_to_long: int = 0
        self.mid_term_token_usage: dict[str, int] = {}

    def _estimate_tokens(self, content: str) -> int:
        """粗略 token 估算 (char/2)"""
        return max(1, len(str(content)) // 2)

    def _mid_term_total_tokens(self) -> int:
        """计算 mid_term 总 token 用量"""
        total = 0
        for cat, entries in self.mid_term.items():
            for entry in entries:
                total += self._estimate_tokens(str(entry.get("content", "")))
        return total

    def _category_token_usage(self, category: str) -> int:
        """计算某类别 token 用量"""
        entries = self.mid_term.get(category, [])
        return sum(self._estimate_tokens(str(e.get("content", ""))) for e in entries)

    def add_to_short_term(self, entry: dict):
        """添加到 short_term (active buffer)"""
        entry["layer"] = "short_term"
        entry["timestamp"] = time.time()
        self.short_term.append(entry)

        # 如果 short_term 满，最旧条目迁移到 mid_term
        if len(self.short_term) >= self.short_capacity:
            oldest = self.short_term[0]
            category = oldest.get("category", "general")
            self.add_to_mid_term(category, dict(oldest))

    def add_to_mid_term(self, category: str, entry: dict):
        """添加到 mid_term (project state)"""
        entry["layer"] = "mid_term"
        entry["timestamp"] = time.time()

        if category not in self.mid_term:
            self.mid_term[category] = []
            self.mid_term_category_limits[category] = self.mid_token_limit // max(1, len(self.mid_term))

        # 检查类别上限
        limit = self.mid_term_category_limits.get(category, 1024)
        current_usage = self._category_token_usage(category)
        new_entry_tokens = self._estimate_tokens(str(entry.get("content", "")))

        # 逐层驱逐: 超过类别上限时驱逐到 long_term
        while current_usage + new_entry_tokens > limit and self.mid_term[category]:
            evicted = self.mid_term[category].pop(0)
            self._archive_to_long_term(category, evicted)
            current_usage = self._category_token_usage(category)
            self.evictions_to_long += 1

        self.mid_term[category].append(entry)

        # 全局 mid_term token 检查
        total = self._mid_term_total_tokens()
        if total > self.mid_token_limit:
            # 驱逐最低优先级的类别中最旧的条目
            self._enforce_mid_term_limit()

        # 更新 token 使用统计
        self.mid_term_token_usage[category] = self._category_token_usage(category)

    def _archive_to_long_term(self, category: str, entry: dict):
        """归档到 long_term"""
        entry["layer"] = "long_term"
        entry["archived_at"] = time.time()

        if category not in self.long_term:
            self.long_term[category] = []
        self.long_term[category].append(entry)

    def _enforce_mid_term_limit(self):
        """强制 mid_term token 上限"""
        total = self._mid_term_total_tokens()
        while total > self.mid_token_limit:
            # 按最后更新时间排序，驱逐最旧的
            all_entries = []
            for cat, entries in self.mid_term.items():
                for i, entry in enumerate(entries):
                    all_entries.append((cat, i, entry.get("timestamp", 0)))

            if not all_entries:
                break

            all_entries.sort(key=lambda x: x[2])  # 按时间戳升序
            cat, idx, _ = all_entries[0]
            evicted = self.mid_term[cat].pop(idx)
            self._archive_to_long_term(cat, evicted)
            self.evictions_to_long += 1
            total = self._mid_term_total_tokens()

    def complete_task(self, category: str, task_id: str):
        """标记任务完成，将其从 mid_term 驱逐到 long_term"""
        if category not in self.mid_term:
            return

        remaining = []
        for entry in self.mid_term[category]:
            if entry.get("task_id") == task_id:
                self._archive_to_long_term(category, entry)
                self.evictions_to_long += 1
            else:
                remaining.append(entry)
        self.mid_term[category] = remaining

        # 重新校准类别上限
        if self.mid_term:
            per_cat_limit = self.mid_token_limit // len(self.mid_term)
            for cat in self.mid_term:
                self.mid_term_category_limits[cat] = per_cat_limit

    def retrieve(self, query_category: str = "", layers: list[str] = None) -> list[dict]:
        """
        按层级优先级检索: short → mid → long
        """
        if layers is None:
            layers = ["short_term", "mid_term", "long_term"]

        results = []

        # short_term
        if "short_term" in layers:
            for entry in self.short_term:
                if not query_category or entry.get("category") == query_category:
                    results.append(entry)

        # mid_term
        if "mid_term" in layers:
            if query_category and query_category in self.mid_term:
                results.extend(self.mid_term[query_category])
            elif not query_category:
                for entries in self.mid_term.values():
                    results.extend(entries)

        # long_term
        if "long_term" in layers:
            if query_category and query_category in self.long_term:
                results.extend(self.long_term[query_category])
            elif not query_category:
                for entries in self.long_term.values():
                    results.extend(entries)

        return results

    def get_mid_term_bounds(self) -> dict:
        """确保 mid_term 注入量有界"""
        total = self._mid_term_total_tokens()
        return {
            "mid_term_limit": self.mid_token_limit,
            "current_usage": total,
            "bounded": total <= self.mid_token_limit,
            "usage_ratio": f"{total / self.mid_token_limit * 100:.2f}%",
            "categories": list(self.mid_term.keys()),
            "per_category_usage": {
                cat: {
                    "entries": len(entries),
                    "tokens": self.mid_term_token_usage.get(cat, 0),
                    "limit": self.mid_term_category_limits.get(cat, 0),
                }
                for cat, entries in self.mid_term.items()
            },
        }

    def diagnostics(self) -> dict:
        bounds = self.get_mid_term_bounds()
        return {
            "short_term_size": len(self.short_term),
            "short_term_capacity": self.short_capacity,
            "mid_term_categories": len(self.mid_term),
            "mid_term_total_tokens": bounds["current_usage"],
            "mid_term_limit": self.mid_token_limit,
            "mid_term_bounded": bounds["bounded"],
            "mid_term_usage_ratio": bounds["usage_ratio"],
            "long_term_archived": sum(len(v) for v in self.long_term.values()),
            "evictions_to_long": self.evictions_to_long,
        }

print("[P83] ThreeLayerHierarchicalMemory (M106) initialized")


# ============ CB45: ProgressiveCascade (NEW, P117, Round 6) ============

