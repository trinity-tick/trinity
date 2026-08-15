"""
# status: orphan (2026-08-15 audit, not in runtime path)
KVCacheMemory — AgentKVShift KV-Cache Residual Decomposition
==============================================================
arXiv 2607.21604 · P40-4

实现 AgentKVShift KV 缓存记忆: KV 残差分解为共享 memory-level offset + token-wise
波动, probe_correction() 小探针集估计偏移以单次加权修正, 10~30% 刷新比替代全量重编码,
专为 agentic memory 结构化元数据 (摘要/关键词/标签) 设计。

设计要点:
  - KVResidualDecomposition: 共享 offset + token-wise fluctuation
  - ProbeCorrector: 小探针集估计层间偏移, 单次加权修正
  - CacheSlot: 元数据感知缓存槽 (摘要/关键词/标签)
  - CacheRefreshPolicy: 低刷新比 (10-30%) 策略
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CacheStatus(Enum):
    """缓存槽状态。"""
    FRESH = auto()
    STALE = auto()
    EVICTABLE = auto()
    REFRESHING = auto()


class RefreshStrategy(Enum):
    """刷新策略。"""
    PROBE_GUIDED = auto()
    PRIORITY_BASED = auto()
    LRU_BASED = auto()
    METADATA_AWARE = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryOffset:
    """共享 memory-level offset——所有复用 token 共享的层间偏移向量。"""
    offset_id: str
    layer_from: int
    layer_to: int
    vector: np.ndarray  # shape: (hidden_dim,)
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenFluctuation:
    """单个 token 的波动——offset 修正后的残差。"""
    token_index: int
    fluctuation: np.ndarray  # shape: (hidden_dim,)
    magnitude: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class CacheSlot:
    """KV 缓存槽——元数据感知的 agentic memory 单元。

    专为结构化元数据设计: 摘要、关键词、标签。
    """
    slot_id: str
    key: np.ndarray   # shape: (hidden_dim,)
    value: np.ndarray # shape: (hidden_dim,)
    status: CacheStatus = CacheStatus.FRESH
    summary: str = ""
    keywords: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    priority: float = 0.0
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    refresh_count: int = 0


@dataclass
class ProbeSet:
    """探针集——用于估计层间偏移的小集合。"""
    probe_id: str
    key_indices: List[int]  # 探针 key 在缓存中的索引
    estimated_offset: Optional[MemoryOffset] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class KVResidualDecomposition:
    """KV 残差分解结果。"""
    decomposition_id: str
    offset: MemoryOffset
    fluctuations: List[TokenFluctuation]
    total_tokens: int
    refresh_ratio: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ProbeCorrector
# ---------------------------------------------------------------------------

class ProbeCorrector:
    """探针引导修正器——用小探针集估计偏移值, 单次加权修正所有复用 token。

    Parameters
    ----------
    hidden_dim : int
        隐藏维度。
    probe_ratio : float
        探针比例 (0.05~0.15)。
    """

    def __init__(self, hidden_dim: int = 768, probe_ratio: float = 0.1) -> None:
        self.hidden_dim = hidden_dim
        self.probe_ratio = probe_ratio
        self._probe_history: deque = deque(maxlen=50)
        self._lock = threading.RLock()

    def create_probe_set(self, cache_slots: List[CacheSlot]) -> ProbeSet:
        """从缓存中采样探针集。"""
        with self._lock:
            n_probes = max(1, int(len(cache_slots) * self.probe_ratio))
            indices = np.random.choice(len(cache_slots), size=min(n_probes, len(cache_slots)), replace=False).tolist()

            probe = ProbeSet(
                probe_id=f"probe_{int(time.time()*1e6)}",
                key_indices=indices,
            )
            self._probe_history.append(probe)
            return probe

    def estimate_offset(
        self,
        probe_set: ProbeSet,
        source_keys: np.ndarray,  # shape: (n_slots, hidden_dim)
        target_keys: np.ndarray,
    ) -> MemoryOffset:
        """用探针集估计层间偏移, 加权平均探针差异。"""
        with self._lock:
            diffs = []
            confidences = []

            for idx in probe_set.key_indices:
                if idx < len(source_keys) and idx < len(target_keys):
                    diff = target_keys[idx] - source_keys[idx]
                    mag = float(np.linalg.norm(diff))
                    diffs.append(diff)
                    confidences.append(1.0 / (1.0 + mag))

            if not diffs:
                # 零偏移
                offset = MemoryOffset(
                    offset_id=f"off_{int(time.time()*1e6)}",
                    layer_from=0,
                    layer_to=0,
                    vector=np.zeros(self.hidden_dim, dtype=np.float64),
                    confidence=0.0,
                )
            else:
                # 加权平均
                total_w = sum(confidences)
                est = sum(d * w for d, w in zip(diffs, confidences)) / total_w
                avg_confidence = total_w / len(confidences)

                offset = MemoryOffset(
                    offset_id=f"off_{int(time.time()*1e6)}",
                    layer_from=0,
                    layer_to=0,
                    vector=est.astype(np.float64),
                    confidence=avg_confidence,
                )

            probe_set.estimated_offset = offset
            logger.debug("Offset estimated: conf=%.3f norm=%.3f",
                         offset.confidence, float(np.linalg.norm(offset.vector)))
            return offset

    def probe_correction(
        self,
        source_slots: List[CacheSlot],
        target_slots: List[CacheSlot],
    ) -> KVResidualDecomposition:
        """探针引导: 用单次加权修正所有复用 token。

        仅刷新探针集 (10-30%) 的 token, 用估计的偏移修正所有 token。

        Returns
        -------
        KVResidualDecomposition
            残差分解结果 (offset + per-token fluctuation)。
        """
        with self._lock:
            if not source_slots or not target_slots:
                # 无数据, 返回空
                offset = MemoryOffset(
                    offset_id=f"off_{int(time.time()*1e6)}",
                    layer_from=0, layer_to=0,
                    vector=np.zeros(self.hidden_dim, dtype=np.float64),
                    confidence=0.0,
                )
                return KVResidualDecomposition(
                    decomposition_id=f"decomp_{int(time.time()*1e6)}",
                    offset=offset, fluctuations=[], total_tokens=0,
                )

            # 1. 创建探针集
            probe = self.create_probe_set(source_slots)

            # 2. 提取探针 key 向量
            src_keys = np.array([s.key for s in source_slots], dtype=np.float64)
            tgt_keys = np.array([s.key for s in target_slots[:len(source_slots)]], dtype=np.float64)

            # 3. 估计偏移
            offset = self.estimate_offset(probe, src_keys, tgt_keys)

            # 4. 计算 token-wise 波动
            fluctuations: List[TokenFluctuation] = []
            n = min(len(source_slots), len(target_slots))
            total_magnitude = 0.0

            for i in range(n):
                corrected = source_slots[i].key + offset.vector
                fluctuation = target_slots[i].key - corrected
                mag = float(np.linalg.norm(fluctuation))
                total_magnitude += mag

                if i in probe.key_indices:
                    # 探针 token: 记录波动
                    fluctuations.append(TokenFluctuation(
                        token_index=i, fluctuation=fluctuation, magnitude=mag,
                    ))

            avg_magnitude = total_magnitude / n if n else 0.0

            return KVResidualDecomposition(
                decomposition_id=f"decomp_{int(time.time()*1e6)}",
                offset=offset,
                fluctuations=fluctuations,
                total_tokens=n,
                refresh_ratio=len(probe.key_indices) / max(n, 1),
            )


# ---------------------------------------------------------------------------
# KVCacheMemory
# ---------------------------------------------------------------------------

class KVCacheMemory:
    """AgentKVShift KV 缓存记忆系统。

    专为 agentic memory 的结构化元数据 (摘要/关键词/标签) 设计,
    以 10-30% 刷新比达到接近全量重编码效果。

    Parameters
    ----------
    hidden_dim : int
        隐藏维度 (默认 768, 适配主流 transformer)。
    capacity : int
        最大缓存槽数。
    probe_ratio : float
        探针比例 (0.05~0.3)。
    """

    def __init__(
        self,
        hidden_dim: int = 768,
        capacity: int = 1024,
        probe_ratio: float = 0.15,
    ) -> None:
        self.hidden_dim = hidden_dim
        self.capacity = capacity
        self.probe_ratio = probe_ratio

        self._probe_corrector = ProbeCorrector(hidden_dim=hidden_dim, probe_ratio=probe_ratio)
        self._cache: OrderedDict[str, CacheSlot] = OrderedDict()
        self._decompositions: List[KVResidualDecomposition] = []
        self._lock = threading.RLock()
        self._slot_count: int = 0
        self._refresh_count: int = 0

        logger.info(
            "KVCacheMemory initialized [dim=%d cap=%d probe=%.2f]",
            hidden_dim, capacity, probe_ratio,
        )

    # ------------------------------------------------------------------
    # Cache Operations
    # ------------------------------------------------------------------

    def _make_key_vector(self, text: str) -> np.ndarray:
        """将文本映射为 key 向量 (无 torch 依赖, 哈希投影)。"""
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        vec = np.zeros(self.hidden_dim, dtype=np.float64)
        for i in range(self.hidden_dim):
            byte_idx = (i * 7) % len(h)
            vec[i] = (h[byte_idx] / 255.0) * 2.0 - 1.0
        # 归一化
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def write_cache_slot(
        self,
        content: str,
        summary: str = "",
        keywords: Optional[List[str]] = None,
        tags: Optional[List[str]] = None,
        priority: float = 0.5,
    ) -> CacheSlot:
        """写入缓存槽——agentic memory 元数据感知。

        Parameters
        ----------
        content : str
            记忆内容。
        summary : str
            结构化摘要。
        keywords : Optional[List[str]]
            关键词。
        tags : Optional[List[str]]
            标签。
        priority : float
            优先级 (0~1)。

        Returns
        -------
        CacheSlot
            创建的缓存槽。
        """
        with self._lock:
            if len(self._cache) >= self.capacity:
                # 淘汰: 最低优先级的最旧条目
                stale = sorted(
                    self._cache.items(),
                    key=lambda x: (x[1].priority, -x[1].last_access),
                )
                if stale:
                    evicted_id, _ = stale[0]
                    del self._cache[evicted_id]
                    logger.debug("Evicted slot: %s", evicted_id)

            self._slot_count += 1
            key_vec = self._make_key_vector(content)
            value_vec = key_vec.copy()  # 简化: 同源, 实际场景可不同

            slot = CacheSlot(
                slot_id=f"slot_{self._slot_count}_{int(time.time()*1e6)}",
                key=key_vec,
                value=value_vec,
                summary=summary,
                keywords=keywords or [],
                tags=tags or [],
                priority=priority,
            )
            self._cache[slot.slot_id] = slot
            logger.debug("Slot written: %s priority=%.2f keywords=%d tags=%d",
                         slot.slot_id, priority, len(slot.keywords), len(slot.tags))
            return slot

    def get_cache_slot(self, slot_id: str) -> Optional[CacheSlot]:
        """读取缓存槽。"""
        slot = self._cache.get(slot_id)
        if slot:
            slot.access_count += 1
            slot.last_access = time.time()
        return slot

    def search_by_metadata(
        self,
        keyword: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[CacheSlot]:
        """按结构化元数据搜索。

        Parameters
        ----------
        keyword : Optional[str]
            关键词匹配。
        tag : Optional[str]
            标签匹配。

        Returns
        -------
        List[CacheSlot]
            匹配的缓存槽列表。
        """
        results = []
        with self._lock:
            for slot in self._cache.values():
                if keyword and keyword.lower() in [k.lower() for k in slot.keywords]:
                    results.append(slot)
                elif tag and tag.lower() in [t.lower() for t in slot.tags]:
                    results.append(slot)
        return sorted(results, key=lambda s: s.priority, reverse=True)

    # ------------------------------------------------------------------
    # Probe-Correction Refresh
    # ------------------------------------------------------------------

    def probe_correction(
        self,
        source_slots: Optional[List[CacheSlot]] = None,
        target_slots: Optional[List[CacheSlot]] = None,
    ) -> KVResidualDecomposition:
        """探针引导修正——以低刷新比替代全量重编码。

        仅刷新探针集 (10-30%) 的 token, 用估计的 offset 修正全部。
        """
        with self._lock:
            if source_slots is None:
                source_slots = list(self._cache.values())
            if target_slots is None:
                target_slots = source_slots

            decomp = self._probe_corrector.probe_correction(source_slots, target_slots)
            self._decompositions.append(decomp)
            self._refresh_count += 1

            # 更新探针 token 所在缓存槽
            for fluct in decomp.fluctuations:
                if fluct.token_index < len(source_slots):
                    slot = source_slots[fluct.token_index]
                    slot.status = CacheStatus.REFRESHING
                    slot.refresh_count += 1

            logger.info("Probe correction: tokens=%d refresh_ratio=%.1f%% offset_confidence=%.3f",
                        decomp.total_tokens,
                        decomp.refresh_ratio * 100,
                        decomp.offset.confidence)
            return decomp

    def refresh_cache(
        self,
        strategy: RefreshStrategy = RefreshStrategy.PROBE_GUIDED,
    ) -> Dict[str, Any]:
        """按策略刷新缓存——低刷新比操作。

        Returns
        -------
        Dict[str, Any]
            刷新统计。
        """
        with self._lock:
            all_slots = list(self._cache.values())
            if not all_slots:
                return {"refreshed": 0, "strategy": strategy.name, "refresh_ratio": 0.0}

            if strategy == RefreshStrategy.PROBE_GUIDED:
                decomp = self.probe_correction(all_slots, all_slots)
                return {
                    "refreshed": len(decomp.fluctuations),
                    "total": decomp.total_tokens,
                    "refresh_ratio": round(decomp.refresh_ratio * 100, 1),
                    "strategy": strategy.name,
                    "offset_confidence": round(decomp.offset.confidence, 4),
                }
            elif strategy == RefreshStrategy.PRIORITY_BASED:
                # 刷新高优先级槽
                n_refresh = max(1, int(len(all_slots) * self.probe_ratio))
                sorted_slots = sorted(all_slots, key=lambda s: s.priority, reverse=True)[:n_refresh]
                for s in sorted_slots:
                    s.refresh_count += 1
                    s.status = CacheStatus.FRESH
                return {
                    "refreshed": len(sorted_slots),
                    "total": len(all_slots),
                    "refresh_ratio": round(len(sorted_slots) / len(all_slots) * 100, 1),
                    "strategy": strategy.name,
                }
            else:  # LRU
                n_refresh = max(1, int(len(all_slots) * self.probe_ratio))
                lru_slots = sorted(all_slots, key=lambda s: s.last_access)[:n_refresh]
                for s in lru_slots:
                    s.refresh_count += 1
                    s.status = CacheStatus.FRESH
                return {
                    "refreshed": len(lru_slots),
                    "total": len(all_slots),
                    "refresh_ratio": round(len(lru_slots) / len(all_slots) * 100, 1),
                    "strategy": strategy.name,
                }

    def get_decomposition(self, decomp_id: str) -> Optional[KVResidualDecomposition]:
        """获取残差分解记录。"""
        for d in self._decompositions:
            if d.decomposition_id == decomp_id:
                return d
        return None

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            status_dist = {s.name: 0 for s in CacheStatus}
            avg_priority = 0.0
            for slot in self._cache.values():
                status_dist[slot.status.name] = status_dist.get(slot.status.name, 0) + 1
                avg_priority += slot.priority
            n = len(self._cache)
            return {
                "total_slots": n,
                "capacity": self.capacity,
                "status_distribution": status_dist,
                "avg_priority": avg_priority / n if n else 0.0,
                "decompositions": len(self._decompositions),
                "total_refreshes": self._refresh_count,
                "avg_refresh_ratio": (
                    np.mean([d.refresh_ratio for d in self._decompositions]) * 100
                    if self._decompositions else 0.0
                ),
            }
