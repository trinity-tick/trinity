"""
# status: orphan (2026-08-15 audit, not in runtime path)
M114 MemoryConsolidationSleep — 记忆巩固睡眠机制

基于 Sleep Paradigm (arXiv 2606.03979, Google+Cornell, 6月)

实现生物启发的记忆巩固循环：
- NREM (慢波睡眠)：离线批量重组 → 相似合并 + 弱记忆修剪
- REM (快速眼动睡眠)：随机重组生成"梦境" → 验证连贯性 + 跨领域关联
- SleepCycleScheduler：活动触发 → NREM / REM 交替循环 → 与 M99 SelfMem 协作
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class MemoryFragment:
    """记忆片段"""
    mem_id: str
    content: Any                                   # 原始内容 (字符串 / 嵌入向量 / 结构化对象)
    embedding: Optional[np.ndarray] = None         # 语义嵌入
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    importance: float = 1.0
    tags: List[str] = field(default_factory=list)
    source: str = ""                               # 来源模块
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mem_id": self.mem_id,
            "content": str(self.content)[:200],
            "access_count": self.access_count,
            "age_seconds": round(self.age_seconds, 1),
            "importance": round(self.importance, 3),
            "tags": self.tags,
            "source": self.source,
        }


@dataclass
class DreamFragment:
    """梦境片段"""
    dream_id: str
    source_mem_ids: List[str]
    narrative: str                                # 组合后的叙事
    coherence_score: float                        # 连贯性评分
    novelty_score: float                          # 新颖性评分
    discovered_links: List[Tuple[str, str, float]]  # (mem_a, mem_b, similarity)
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    if a is None or b is None:
        return 0.0
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-8 or nb < 1e-8:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _hash_content(content: Any) -> str:
    raw = json.dumps(content, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# NREMConsolidator — 慢波睡眠
# ---------------------------------------------------------------------------

class NREMConsolidator:
    """
    类比慢波睡眠 (Non-REM)：

    1. 相似记忆合并：语义相似度 > threshold → 合并为统一表示
    2. 弱记忆修剪：access_count < 阈值 + age > 阈值 → 降级到冷存储
    """

    def __init__(
        self,
        similarity_threshold: float = 0.8,
        weak_access_threshold: int = 3,
        weak_age_threshold_seconds: float = 86400.0,  # 24h
    ):
        self.similarity_threshold = similarity_threshold
        self.weak_access_threshold = weak_access_threshold
        self.weak_age_threshold = weak_age_threshold_seconds
        self.merge_log: List[Dict[str, Any]] = []
        self.prune_log: List[Dict[str, Any]] = []
        self.cold_storage: List[MemoryFragment] = []

    # ------------------------------------------------------------------
    # 相似记忆合并
    # ------------------------------------------------------------------
    def consolidate(
        self,
        memories: List[MemoryFragment],
    ) -> Tuple[List[MemoryFragment], List[MemoryFragment]]:
        """
        对记忆列表执行 NREM 巩固。

        Returns:
            (merged_memories, cold_memories)
        """
        if len(memories) < 2:
            return memories, []

        # Step 1: 相似度聚类
        clusters = self._cluster_by_similarity(memories)

        # Step 2: 每簇内合并为单个合并记忆
        merged: List[MemoryFragment] = []
        merge_records: List[Dict[str, Any]] = []

        for cluster in clusters:
            if len(cluster) == 1:
                merged.append(cluster[0])
                continue

            # 选择 importance 最高的作为主记忆，其他吸收
            sorted_cluster = sorted(cluster, key=lambda m: m.importance, reverse=True)
            primary = sorted_cluster[0]

            absorbed_ids = []
            for m in sorted_cluster[1:]:
                primary.access_count += m.access_count
                primary.importance = max(primary.importance, m.importance * 0.8)
                primary.tags = list(set(primary.tags + m.tags))
                if primary.embedding is not None and m.embedding is not None:
                    primary.embedding = (primary.embedding + m.embedding) / 2.0
                absorbed_ids.append(m.mem_id)

            merged.append(primary)
            merge_records.append({
                "primary_id": primary.mem_id,
                "absorbed_ids": absorbed_ids,
                "cluster_size": len(cluster),
                "timestamp": datetime.now().isoformat(),
            })

        self.merge_log.extend(merge_records)

        # Step 3: 弱记忆修剪
        kept, cold = self._prune_weak(merged)

        return kept, cold

    def _cluster_by_similarity(
        self,
        memories: List[MemoryFragment],
    ) -> List[List[MemoryFragment]]:
        """基于语义嵌入的单连接聚类"""
        n = len(memories)
        if n <= 1:
            return [[m] for m in memories]

        # Union-Find
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i in range(n):
            for j in range(i + 1, n):
                emb_i = memories[i].embedding
                emb_j = memories[j].embedding
                if emb_i is not None and emb_j is not None:
                    sim = _cosine_similarity(emb_i, emb_j)
                    if sim > self.similarity_threshold:
                        union(i, j)

        # 按根分组
        groups: Dict[int, List[MemoryFragment]] = defaultdict(list)
        for idx, mem in enumerate(memories):
            groups[find(idx)].append(mem)

        return list(groups.values())

    def _prune_weak(
        self,
        memories: List[MemoryFragment],
    ) -> Tuple[List[MemoryFragment], List[MemoryFragment]]:
        """弱记忆修剪 → 降级到冷存储"""
        kept: List[MemoryFragment] = []
        cold: List[MemoryFragment] = []

        for mem in memories:
            if (
                mem.access_count < self.weak_access_threshold
                and mem.age_seconds > self.weak_age_threshold
            ):
                cold.append(mem)
                self.prune_log.append({
                    "mem_id": mem.mem_id,
                    "access_count": mem.access_count,
                    "age_seconds": round(mem.age_seconds, 1),
                    "reason": "weak_memory",
                    "timestamp": datetime.now().isoformat(),
                })
            else:
                kept.append(mem)

        self.cold_storage.extend(cold)
        return kept, cold

    def recall_from_cold(self, mem_id: str) -> Optional[MemoryFragment]:
        """从冷存储中召回记忆"""
        for i, mem in enumerate(self.cold_storage):
            if mem.mem_id == mem_id:
                mem.access_count += 1
                mem.last_accessed = time.time()
                return self.cold_storage.pop(i)
        return None


# ---------------------------------------------------------------------------
# REMDreamer — 快速眼动睡眠
# ---------------------------------------------------------------------------

class REMDreamer:
    """
    类比快速眼动睡眠 (REM)：

    - 随机抽取记忆片段 → 重组为"梦境"
    - 验证记忆连贯性
    - 发现潜在跨领域关联
    """

    def __init__(
        self,
        dream_fragment_count: int = 5,
        coherence_threshold: float = 0.3,
        novelty_decay: float = 0.9,
    ):
        self.dream_fragment_count = dream_fragment_count
        self.coherence_threshold = coherence_threshold
        self.novelty_decay = novelty_decay
        self.dream_log: List[DreamFragment] = []
        self.discovered_links: List[Tuple[str, str, float, str]] = []

    def dream(
        self,
        memories: List[MemoryFragment],
    ) -> Optional[DreamFragment]:
        """
        执行一次 REM 梦境生成。

        流程：
        1. 随机采样记忆片段
        2. 计算两两连贯性
        3. 生成叙事 (narrative)
        4. 发现跨领域链接
        """
        if len(memories) < 2:
            return None

        n_sample = min(self.dream_fragment_count, len(memories))
        sampled = random.sample(memories, n_sample)

        # 两两计算连贯性 (= 语义相似度)
        links: List[Tuple[str, str, float]] = []
        coherence_scores: List[float] = []

        for i in range(len(sampled)):
            for j in range(i + 1, len(sampled)):
                if sampled[i].embedding is not None and sampled[j].embedding is not None:
                    sim = _cosine_similarity(sampled[i].embedding, sampled[j].embedding)
                    links.append((sampled[i].mem_id, sampled[j].mem_id, sim))
                    coherence_scores.append(sim)

        avg_coherence = float(np.mean(coherence_scores)) if coherence_scores else 0.0

        # 新颖性 = 1 - 平均连贯性 (越不相关越新颖)
        novelty = 1.0 - avg_coherence
        novelty *= self.novelty_decay ** len(self.dream_log)

        # 发现跨领域链接 (低连贯性但 tags 无交集 = 跨领域)
        cross_domain_links: List[Tuple[str, str, float]] = []
        for ma_id, mb_id, sim in links:
            if sim < self.coherence_threshold:
                ma = next((m for m in sampled if m.mem_id == ma_id), None)
                mb = next((m for m in sampled if m.mem_id == mb_id), None)
                if ma and mb:
                    tag_overlap = set(ma.tags) & set(mb.tags)
                    if not tag_overlap:
                        cross_domain_links.append((ma_id, mb_id, sim))
                        self.discovered_links.append(
                            (ma_id, mb_id, sim, datetime.now().isoformat())
                        )

        # 生成叙事
        tags_in_dream = list({t for m in sampled for t in m.tags})
        narrative = (
            f"Dream combining {len(sampled)} fragments from tags: {tags_in_dream}. "
            f"Avg coherence: {avg_coherence:.3f}, cross-domain links: {len(cross_domain_links)}"
        )

        dream_fragment = DreamFragment(
            dream_id=_hash_content([m.mem_id for m in sampled]),
            source_mem_ids=[m.mem_id for m in sampled],
            narrative=narrative,
            coherence_score=round(avg_coherence, 4),
            novelty_score=round(novelty, 4),
            discovered_links=cross_domain_links,
        )

        self.dream_log.append(dream_fragment)
        return dream_fragment

    def get_cross_domain_insights(self, min_count: int = 2) -> List[Dict[str, Any]]:
        """
        获取反复出现的跨领域链接 (≥ min_count 次) → 潜在概念桥接。
        """
        link_counter: Dict[Tuple[str, str], int] = defaultdict(int)
        for ma_id, mb_id, sim, ts in self.discovered_links:
            key = tuple(sorted([ma_id, mb_id]))
            link_counter[key] += 1

        insights = []
        for (a, b), count in link_counter.items():
            if count >= min_count:
                insights.append({
                    "mem_a": a,
                    "mem_b": b,
                    "occurrences": count,
                    "category": "cross_domain_bridge",
                })
        return insights


# ---------------------------------------------------------------------------
# SleepCycleScheduler
# ---------------------------------------------------------------------------

class SleepCycleScheduler:
    """
    睡眠周期调度器。

    触发条件：
    - 累计活动时间 > 10min (600s)
    - 或 记忆变更 > 100 次

    循环：
    - NREM 周期 (merge + prune)
    - REM 周期 (dream)
    - 交替执行，与 M99 SelfMem 协作
    """

    def __init__(
        self,
        nrem: Optional[NREMConsolidator] = None,
        rem: Optional[REMDreamer] = None,
        activity_threshold_seconds: float = 600.0,
        memory_change_threshold: int = 100,
        cycles_per_sleep: int = 3,
    ):
        self.nrem = nrem or NREMConsolidator()
        self.rem = rem or REMDreamer()
        self.activity_threshold = activity_threshold_seconds
        self.memory_change_threshold = memory_change_threshold
        self.cycles_per_sleep = cycles_per_sleep

        # 运行时状态
        self.active_seconds: float = 0.0
        self.memory_change_count: int = 0
        self.last_sleep_time: float = time.time()
        self.cycle_history: List[Dict[str, Any]] = []

        # M99 SelfMem 引用 (外部注入)
        self.self_mem: Any = None

    # ------------------------------------------------------------------
    # 活动追踪
    # ------------------------------------------------------------------
    def record_activity(self, duration_seconds: float = 1.0, memory_changes: int = 0):
        """记录活动 (由 M99 或其他模块调用)"""
        self.active_seconds += duration_seconds
        self.memory_change_count += memory_changes

    def should_sleep(self) -> bool:
        """判断是否应触发睡眠周期"""
        return (
            self.active_seconds >= self.activity_threshold
            or self.memory_change_count >= self.memory_change_threshold
        )

    def sleep(
        self,
        memories: List[MemoryFragment],
    ) -> Tuple[List[MemoryFragment], List[DreamFragment]]:
        """
        执行完整睡眠周期。

        Returns:
            (consolidated_memories, dreams)
        """
        all_dreams: List[DreamFragment] = []
        current_memories = list(memories)

        cycle_record = {
            "timestamp": datetime.now().isoformat(),
            "input_memory_count": len(memories),
            "nrem_cycles": [],
            "rem_cycles": [],
        }

        for cycle in range(self.cycles_per_sleep):
            # --- NREM 阶段 ---
            kept, cold = self.nrem.consolidate(current_memories)
            nrem_record = {
                "cycle": cycle + 1,
                "kept": len(kept),
                "cold": len(cold),
                "merged": len(self.nrem.merge_log[-1:]) if self.nrem.merge_log else 0,
            }
            cycle_record["nrem_cycles"].append(nrem_record)

            # 冷存储记忆保留在队列尾 (低优先级，但不丢弃)
            current_memories = kept + cold
            # 按 importance 排序
            current_memories.sort(key=lambda m: m.importance, reverse=True)

            # --- REM 阶段 ---
            dream = self.rem.dream(current_memories)
            if dream:
                all_dreams.append(dream)
                nrem_record["dream_id"] = dream.dream_id
                nrem_record["dream_coherence"] = dream.coherence_score

        cycle_record["output_memory_count"] = len(current_memories)
        cycle_record["dream_count"] = len(all_dreams)
        self.cycle_history.append(cycle_record)

        # 重置活动计数器
        self.active_seconds = 0.0
        self.memory_change_count = 0
        self.last_sleep_time = time.time()

        return current_memories, all_dreams

    def inject_self_mem(self, self_mem: Any):
        """注入 M99 SelfMem v3 实例"""
        self.self_mem = self_mem

    def get_stats(self) -> Dict[str, Any]:
        return {
            "active_seconds": round(self.active_seconds, 1),
            "memory_change_count": self.memory_change_count,
            "seconds_since_last_sleep": round(time.time() - self.last_sleep_time, 1),
            "total_cycles_completed": len(self.cycle_history),
            "total_dreams": len(self.rem.dream_log),
            "cold_storage_size": len(self.nrem.cold_storage),
            "cross_domain_links": len(self.rem.discovered_links),
        }


# ---------------------------------------------------------------------------
# 与 M99 SelfMem v3 集成适配器
# ---------------------------------------------------------------------------

class SleepSelfMemAdapter:
    """
    将 M114 睡眠机制集成到 M99 SelfMem v3 的适配器。

    SelfMem 在每次记忆操作后调用 record_activity，
    当 scheduler.should_sleep() 返回 True 时触发 sleep 循环。
    """

    def __init__(self, scheduler: Optional[SleepCycleScheduler] = None):
        self.scheduler = scheduler or SleepCycleScheduler()

    def on_memory_write(self, mem: MemoryFragment):
        """M99 写记忆时回调"""
        self.scheduler.record_activity(duration_seconds=0.5, memory_changes=1)

    def on_memory_read(self, mem: MemoryFragment):
        """M99 读记忆时回调"""
        self.scheduler.record_activity(duration_seconds=0.1, memory_changes=0)

    def periodic_check(
        self,
        active_memories: List[MemoryFragment],
    ) -> Optional[Tuple[List[MemoryFragment], List[DreamFragment]]]:
        """
        M99 定期检查 — 如果应睡眠则执行。

        Returns:
            (consolidated_memories, dreams) 或 None
        """
        if self.scheduler.should_sleep():
            return self.scheduler.sleep(active_memories)
        return None


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== M114 MemoryConsolidationSleep 自检 ===\n")

    rng = np.random.default_rng(42)

    # 构建模拟记忆
    def make_mem(mid: str, emb: np.ndarray, acc: int, age_offset: float, tags: List[str]):
        return MemoryFragment(
            mem_id=mid,
            content=f"memory content for {mid}",
            embedding=emb,
            access_count=acc,
            created_at=time.time() - age_offset,
            last_accessed=time.time() - age_offset * 0.5,
            tags=tags,
            source="test",
        )

    dim = 4
    memories = [
        # 两个高度相似的记忆 → 应合并
        make_mem("m1", rng.normal(0, 0.3, dim), 10, 100, ["math", "algebra"]),
        make_mem("m2", rng.normal(0.05, 0.3, dim), 8, 120, ["math", "geometry"]),
        # 弱记忆 → 应修剪
        make_mem("m3_weak", rng.normal(3, 0.3, dim), 0, 100000, ["obscure"]),
        # 正常记忆
        make_mem("m4", rng.normal(5, 0.3, dim), 50, 200, ["physics", "mechanics"]),
        make_mem("m5", rng.normal(5.2, 0.3, dim), 40, 300, ["physics", "optics"]),
        # 跨领域记忆
        make_mem("m6", rng.normal(-5, 0.3, dim), 20, 500, ["biology", "neuroscience"]),
        make_mem("m7", rng.normal(5.5, 0.3, dim), 30, 400, ["art", "painting"]),
    ]

    # NREM 测试
    nrem = NREMConsolidator(similarity_threshold=0.7)
    kept, cold = nrem.consolidate(memories)
    print(f"NREM: {len(memories)} → {len(kept)} kept, {len(cold)} cold")
    for m in cold:
        print(f"  Cold: {m.mem_id} (acc={m.access_count}, age={m.age_seconds:.0f}s)")

    # REM 测试
    rem = REMDreamer(dream_fragment_count=4)
    dream = rem.dream(kept)
    if dream:
        print(f"\nREM dream: {dream.narrative}")
        print(f"  coherence={dream.coherence_score}, novelty={dream.novelty_score}")
        print(f"  cross-domain links: {len(dream.discovered_links)}")

    # Scheduler 集成测试
    scheduler = SleepCycleScheduler(nrem=nrem, rem=rem)
    scheduler.record_activity(duration_seconds=700, memory_changes=120)
    print(f"\nShould sleep: {scheduler.should_sleep()}")
    consolidated, dreams = scheduler.sleep(memories)
    print(f"After sleep: {len(consolidated)} memories, {len(dreams)} dreams")

    stats = scheduler.get_stats()
    print(f"\nScheduler stats: {json.dumps(stats, indent=2)}")

    # 跨领域链接洞察
    insights = rem.get_cross_domain_insights(min_count=0)
    print(f"\nCross-domain insights: {len(insights)}")

    print("\n=== 自检通过 ===")
