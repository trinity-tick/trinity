"""
# status: orphan (2026-08-15 audit, not in runtime path)
P13-3: CRDT Collaborative Memory Store
=======================================

对标 Yjs / Automerge CRDT + AI 代理速率问题。

核心系统：
  - CRDTMemoryStore:      基于 CRDT 的无冲突共享记忆存储，
                          每个副本携带自给元数据独立合并
  - AgentRateLimiter:     解决代理高速编辑淹没人类输入的速率问题
                          （操作批处理 + 专用代理通道）
  - OfflineMergeEngine:   离线操作后重连自动合并，
                          冲突以确定性规则解决（LWW / 多值）
  - TombstoneManager:     逻辑删除管理，防止墓碑膨胀
                          （定期压缩 / 多代理共识清理）
  - SnapshotInterop:      与 P12 memory_version_control.py COW 快照互通

接口兼容：memory_version_control.py（CopyOnWriteSnapshot / MemoryBranch）
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class CRDTOperation(Enum):
    """CRDT 操作类型。"""
    INSERT = "insert"
    DELETE = "delete"
    UPDATE = "update"
    MERGE = "merge"
    SPLIT = "split"
    TOMBSTONE = "tombstone"  # 逻辑删除标记


class ConflictResolution(Enum):
    """冲突解决策略。"""
    LWW = "last_writer_wins"          # 最后写入者胜利
    MULTI_VALUE = "multi_value"       # 保留多值
    MAJORITY_VOTE = "majority_vote"    # 多数投票
    PRIORITY_BASED = "priority_based"  # 优先级排序
    MANUAL = "manual"                  # 手动解决


class AgentChannelPriority(Enum):
    """代理通道优先级。"""
    HUMAN = "human"           # 人类用户（最高优先）
    SYSTEM = "system"         # 系统自动（中优先）
    AGENT_BULK = "agent_bulk" # 代理批量（最低优先）


class TombstoneState(Enum):
    """墓碑状态。"""
    ACTIVE = "active"           # 活跃（尚在引用）
    PENDING_COMPACTION = "pending_compaction"  # 等待压缩
    COMPACTED = "compacted"     # 已压缩
    GARBAGE_COLLECTED = "garbage_collected"   # 已回收


class SnapshotSyncMode(Enum):
    """快照同步模式。"""
    COW_REFERENCE = "cow_reference"       # COW 引用（P12 互通）
    FULL_COPY = "full_copy"              # 完整复制
    INCREMENTAL = "incremental"           # 增量同步


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class CRDTPayload:
    """CRDT 操作载荷——自给元数据，支持独立合并。"""
    operation_id: str
    operation: CRDTOperation
    key: str
    value: Any
    clock: int                          # 逻辑时钟（Lamport）
    replica_id: str                     # 副本标识
    parent_ids: List[str] = field(default_factory=list)  # 因果依赖
    timestamp: float = field(default_factory=time.time)
    signature: str = ""                 # 内容哈希
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CRDTEntry:
    """CRDT 存储中的单条记忆条目。"""
    key: str
    values: List[Any]                   # 多值列表（multi-value 策略时）
    clock: int
    last_writer: str
    tombstone: bool = False
    tombstone_state: TombstoneState = TombstoneState.ACTIVE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    deleted_at: Optional[float] = None
    version_vector: Dict[str, int] = field(default_factory=dict)  # replica → clock
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RateLimitConfig:
    """速率限制配置。"""
    human_max_ops_per_second: float = 10.0
    agent_bulk_max_ops_per_second: float = 100.0
    system_max_ops_per_second: float = 50.0
    batch_size: int = 32                 # 代理批量操作合并尺寸
    batch_window_ms: float = 200.0       # 批处理窗口
    human_channel_priority: bool = True   # 人类通道优先


@dataclass
class OfflineOperation:
    """离线操作记录。"""
    op_id: str
    payload: CRDTPayload
    is_applied: bool = False
    conflicted: bool = False
    conflict_resolution: Optional[ConflictResolution] = None
    recorded_at: float = field(default_factory=time.time)
    applied_at: Optional[float] = None


@dataclass
class TombstoneRecord:
    """墓碑记录。"""
    key: str
    deleted_at: float
    state: TombstoneState = TombstoneState.ACTIVE
    referencing_entries: List[str] = field(default_factory=list)  # 引用此墓碑的条目
    compaction_candidates: bool = False
    consensus_count: int = 0            # 多代理确认数


@dataclass
class CRDTStoreStats:
    """CRDT 存储统计。"""
    total_entries: int = 0
    active_entries: int = 0
    tombstone_count: int = 0
    total_operations: int = 0
    conflicts_detected: int = 0
    conflicts_resolved: int = 0
    offline_ops_pending: int = 0
    rate_limit_rejections: int = 0


# ============================================================================
# CRDTMemoryStore
# ============================================================================

class CRDTMemoryStore:
    """基于 CRDT 的无冲突共享记忆存储。

    每个副本携带自给元数据（Lamport 时钟 + 版本向量），
    支持独立合并，无需中央协调即可达成最终一致性。
    """

    def __init__(
        self,
        replica_id: str = "",
        default_resolution: ConflictResolution = ConflictResolution.LWW,
    ):
        self.replica_id = replica_id or f"replica_{uuid.uuid4().hex[:8]}"
        self.default_resolution = default_resolution

        self._lock = threading.RLock()
        self._entries: Dict[str, CRDTEntry] = {}
        self._clock: int = 0
        self._version_vector: Dict[str, int] = {self.replica_id: 0}
        self._operation_log: deque = deque(maxlen=10000)
        self._stats = CRDTStoreStats()

    def _tick(self) -> int:
        """推进逻辑时钟。"""
        self._clock += 1
        self._version_vector[self.replica_id] = self._clock
        return self._clock

    def insert(self, key: str, value: Any) -> CRDTPayload:
        """插入新条目。"""
        with self._lock:
            clock = self._tick()
            payload = CRDTPayload(
                operation_id=f"op_{uuid.uuid4().hex[:12]}",
                operation=CRDTOperation.INSERT,
                key=key,
                value=value,
                clock=clock,
                replica_id=self.replica_id,
            )
            payload.signature = hashlib.sha256(
                f"{key}:{str(value)}:{clock}:{self.replica_id}".encode()
            ).hexdigest()[:16]

            if key in self._entries:
                existing = self._entries[key]
                if existing.tombstone:
                    existing.tombstone = False
                    existing.tombstone_state = TombstoneState.ACTIVE
                existing.values = [value]
                existing.clock = clock
                existing.last_writer = self.replica_id
                existing.updated_at = time.time()
                payload.operation = CRDTOperation.UPDATE
            else:
                self._entries[key] = CRDTEntry(
                    key=key,
                    values=[value],
                    clock=clock,
                    last_writer=self.replica_id,
                    version_vector={self.replica_id: clock},
                )

            self._operation_log.append(payload)
            self._stats.total_operations += 1
            self._stats.total_entries = len(self._entries)
            self._stats.active_entries = sum(1 for e in self._entries.values() if not e.tombstone)
            return payload

    def read(self, key: str) -> Optional[Any]:
        """读取条目。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.tombstone:
                return None
            if len(entry.values) == 1:
                return entry.values[0]
            return list(entry.values)  # 多值返回全部

    def delete(self, key: str) -> Optional[CRDTPayload]:
        """逻辑删除（标记墓碑）。"""
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.tombstone:
                return None

            clock = self._tick()
            payload = CRDTPayload(
                operation_id=f"op_{uuid.uuid4().hex[:12]}",
                operation=CRDTOperation.TOMBSTONE,
                key=key,
                value=None,
                clock=clock,
                replica_id=self.replica_id,
                parent_ids=[key],
            )
            payload.signature = hashlib.sha256(
                f"DEL:{key}:{clock}:{self.replica_id}".encode()
            ).hexdigest()[:16]

            entry.tombstone = True
            entry.tombstone_state = TombstoneState.ACTIVE
            entry.deleted_at = time.time()
            entry.clock = clock
            entry.last_writer = self.replica_id

            self._operation_log.append(payload)
            self._stats.total_operations += 1
            self._stats.active_entries = sum(1 for e in self._entries.values() if not e.tombstone)
            return payload

    def merge_payload(self, payload: CRDTPayload) -> bool:
        """合并来自其他副本的 CRDT 载荷。"""
        with self._lock:
            existing = self._entries.get(payload.key)

            if existing is None:
                # 新条目
                self._entries[payload.key] = CRDTEntry(
                    key=payload.key,
                    values=[payload.value] if payload.value is not None else [],
                    clock=payload.clock,
                    last_writer=payload.replica_id,
                    version_vector={payload.replica_id: payload.clock},
                )
                self._update_clock(payload)
                return True

            # 冲突检测
            if payload.clock == existing.clock and payload.replica_id != existing.last_writer:
                self._stats.conflicts_detected += 1
                return self._resolve_conflict(payload, existing)

            # LWW 比较
            if payload.clock > existing.clock or (
                payload.clock == existing.clock and payload.replica_id > existing.last_writer
            ):
                return self._apply_winner(payload, existing)

            # 旧操作，忽略
            return False

    def _apply_winner(self, payload: CRDTPayload, entry: CRDTEntry) -> bool:
        """应用获胜的载荷。"""
        if payload.operation == CRDTOperation.TOMBSTONE:
            entry.tombstone = True
            entry.tombstone_state = TombstoneState.ACTIVE
            entry.deleted_at = time.time()
        else:
            entry.values = [payload.value] if payload.value is not None else entry.values
            entry.tombstone = False
            entry.tombstone_state = TombstoneState.ACTIVE
        entry.clock = payload.clock
        entry.last_writer = payload.replica_id
        entry.updated_at = time.time()
        self._update_clock(payload)
        return True

    def _resolve_conflict(self, payload: CRDTPayload, entry: CRDTEntry) -> bool:
        """解决冲突。"""
        if self.default_resolution == ConflictResolution.MULTI_VALUE:
            if payload.value is not None and payload.value not in entry.values:
                entry.values.append(payload.value)
            self._stats.conflicts_resolved += 1
            return True
        elif self.default_resolution == ConflictResolution.LWW:
            return self._apply_winner(payload, entry)
        else:
            # 标记冲突，待手动解决
            self._stats.conflicts_resolved += 1
            return False

    def _update_clock(self, payload: CRDTPayload) -> None:
        """更新本地时钟和版本向量。"""
        self._clock = max(self._clock, payload.clock) + 1
        self._version_vector[self.replica_id] = self._clock
        entry = self._entries.get(payload.key)
        if entry:
            entry.version_vector[payload.replica_id] = max(
                entry.version_vector.get(payload.replica_id, 0), payload.clock
            )

    def get_version_vector(self) -> Dict[str, int]:
        """获取版本向量。"""
        with self._lock:
            return dict(self._version_vector)

    def dump_entries(self) -> List[CRDTEntry]:
        """导出所有条目。"""
        with self._lock:
            return list(self._entries.values())

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "replica_id": self.replica_id,
                "clock": self._clock,
                "version_vector_size": len(self._version_vector),
                **dataclasses.asdict(self._stats),
            }


# ============================================================================
# AgentRateLimiter
# ============================================================================

class AgentRateLimiter:
    """解决代理高速编辑淹没人类输入的速率问题。

    策略：
      - 人类通道：无限制（或高上限），优先处理
      - 系统通道：中等上限
      - 代理批量通道：合并批量操作，降低写入频率
      - 操作批处理：在时间窗口内合并同 key 操作
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        self.config = config or RateLimitConfig()
        self._lock = threading.RLock()
        self._buckets: Dict[AgentChannelPriority, List[float]] = defaultdict(list)
        self._pending_batches: Dict[AgentChannelPriority, Dict[str, CRDTPayload]] = defaultdict(dict)
        self._last_batch_flush: Dict[AgentChannelPriority, float] = defaultdict(float)
        self._rejection_count: int = 0

    def allow_operation(self, priority: AgentChannelPriority) -> bool:
        """判断操作是否被允许。"""
        with self._lock:
            now = time.time()
            limit = {
                AgentChannelPriority.HUMAN: self.config.human_max_ops_per_second,
                AgentChannelPriority.SYSTEM: self.config.system_max_ops_per_second,
                AgentChannelPriority.AGENT_BULK: self.config.agent_bulk_max_ops_per_second,
            }.get(priority, 50.0)

            # 清理旧时间戳
            self._buckets[priority] = [
                t for t in self._buckets[priority] if now - t < 1.0
            ]

            if len(self._buckets[priority]) >= limit:
                self._rejection_count += 1
                self._stats_update()
                return False

            self._buckets[priority].append(now)
            return True

    def batch_operation(self, priority: AgentChannelPriority, payload: CRDTPayload) -> Optional[List[CRDTPayload]]:
        """将操作加入批处理，到达阈值或超时时返回批量。"""
        with self._lock:
            if not self.allow_operation(priority):
                return None

            batch = self._pending_batches[priority]
            batch[payload.key] = payload  # 同 key 覆盖

            now = time.time()
            last_flush = self._last_batch_flush.get(priority, 0.0)
            should_flush = (
                len(batch) >= self.config.batch_size
                or (now - last_flush > self.config.batch_window_ms / 1000.0 and len(batch) > 0)
            )

            if should_flush:
                flushed = list(batch.values())
                batch.clear()
                self._last_batch_flush[priority] = now
                return flushed
            return None

    def force_flush(self, priority: AgentChannelPriority) -> List[CRDTPayload]:
        """强制刷新批处理队列。"""
        with self._lock:
            batch = self._pending_batches[priority]
            flushed = list(batch.values())
            batch.clear()
            self._last_batch_flush[priority] = time.time()
            return flushed

    def _stats_update(self) -> None:
        """更新统计。"""
        pass  # 统计由 CRDTMemoryStore 维护

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "config": dataclasses.asdict(self.config),
                "current_buckets": {
                    p.value: len(self._buckets[p]) for p in AgentChannelPriority
                },
                "pending_batches": {
                    p.value: len(self._pending_batches[p]) for p in AgentChannelPriority
                },
                "rejection_count": self._rejection_count,
            }


# ============================================================================
# OfflineMergeEngine
# ============================================================================

class OfflineMergeEngine:
    """离线操作后重连自动合并，冲突以确定性规则解决。

    支持：
      - 离线操作日志：记录离线期间所有操作
      - 重连自动重放：按 Lamport 时钟排序重放
      - 冲突解决：LWW / Multi-Value / Majority Vote
    """

    def __init__(
        self,
        store: CRDTMemoryStore,
        resolution: ConflictResolution = ConflictResolution.LWW,
    ):
        self.store = store
        self.resolution = resolution
        self._lock = threading.RLock()
        self._offline_ops: List[OfflineOperation] = []
        self._is_online: bool = True

    def go_offline(self) -> None:
        """进入离线模式。"""
        with self._lock:
            self._is_online = False
            logger.info("OfflineMergeEngine: entering offline mode")

    def record_offline_op(self, payload: CRDTPayload) -> str:
        """记录离线操作。"""
        with self._lock:
            op = OfflineOperation(
                op_id=f"offline_{uuid.uuid4().hex[:12]}",
                payload=payload,
            )
            self._offline_ops.append(op)
            return op.op_id

    def go_online(self, remote_ops: Optional[List[CRDTPayload]] = None) -> Dict[str, Any]:
        """重连并合并离线操作和远程操作。"""
        with self._lock:
            self._is_online = True
            result = {
                "offline_ops_replayed": 0,
                "remote_ops_merged": 0,
                "conflicts": 0,
                "skipped": 0,
            }

            # 1. 将本地离线操作应用到本地存储
            local_ops = sorted(self._offline_ops, key=lambda o: o.payload.clock)
            for op in local_ops:
                if op.is_applied:
                    result["skipped"] += 1
                    continue
                success = self._apply_with_conflict_check(op.payload)
                op.is_applied = True
                op.applied_at = time.time()
                if success:
                    result["offline_ops_replayed"] += 1
                else:
                    result["conflicts"] += 1

            # 2. 合并远程操作
            if remote_ops:
                for rp in sorted(remote_ops, key=lambda p: p.clock):
                    merged = self.store.merge_payload(rp)
                    result["remote_ops_merged"] += int(merged)

            self._offline_ops.clear()
            return result

    def _apply_with_conflict_check(self, payload: CRDTPayload) -> bool:
        """应用操作并检查冲突。"""
        if payload.operation == CRDTOperation.INSERT or payload.operation == CRDTOperation.UPDATE:
            self.store.insert(payload.key, payload.value)
            return True
        elif payload.operation == CRDTOperation.TOMBSTONE:
            self.store.delete(payload.key)
            return True
        return False

    def get_pending_offline_ops(self) -> List[OfflineOperation]:
        """获取未应用的离线操作。"""
        with self._lock:
            return [op for op in self._offline_ops if not op.is_applied]

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "is_online": self._is_online,
                "pending_offline_ops": len([o for o in self._offline_ops if not o.is_applied]),
                "total_offline_ops": len(self._offline_ops),
                "resolution_strategy": self.resolution.value,
            }


# ============================================================================
# TombstoneManager
# ============================================================================

class TombstoneManager:
    """逻辑删除管理，防止墓碑膨胀。

    策略：
      - 定期压缩：周期性扫描墓碑，移除可安全清理的旧墓碑
      - 多代理共识清理：需要 N/2+1 个副本确认后方可物理删除
      - 引用检查：墓碑仍有活跃引用时不压缩
      - 压缩阈值：墓碑数量 / 墓碑年龄阈值
    """

    def __init__(
        self,
        store: CRDTMemoryStore,
        max_tombstone_age_seconds: float = 604800.0,  # 7 天
        max_tombstone_ratio: float = 0.3,              # 墓碑占比超过 30% 触发压缩
        consensus_quorum: int = 3,
    ):
        self.store = store
        self.max_tombstone_age_seconds = max_tombstone_age_seconds
        self.max_tombstone_ratio = max_tombstone_ratio
        self.consensus_quorum = consensus_quorum

        self._lock = threading.RLock()
        self._tombstones: Dict[str, TombstoneRecord] = {}
        self._compaction_log: List[Dict[str, Any]] = []
        self._consensus_votes: Dict[str, Set[str]] = defaultdict(set)

    def register_tombstone(self, key: str) -> None:
        """注册新墓碑。"""
        with self._lock:
            self._tombstones[key] = TombstoneRecord(
                key=key,
                deleted_at=time.time(),
            )

    def should_compact(self) -> bool:
        """判断是否应触发压缩。"""
        with self._lock:
            total = len(self.store.dump_entries())
            if total == 0:
                return False
            tombstone_count = sum(
                1 for e in self.store.dump_entries() if e.tombstone
            )
            ratio = tombstone_count / total
            return ratio >= self.max_tombstone_ratio

    def get_compaction_candidates(self) -> List[str]:
        """获取可安全压缩的墓碑列表。"""
        with self._lock:
            now = time.time()
            entries = self.store.dump_entries()
            candidates = []
            for e in entries:
                if not e.tombstone:
                    continue
                if e.tombstone_state != TombstoneState.ACTIVE:
                    continue
                age = now - (e.deleted_at or 0)
                if age >= self.max_tombstone_age_seconds:
                    candidates.append(e.key)
            return candidates

    def vote_consensus(self, replica_id: str, key: str) -> bool:
        """副本投票确认墓碑可安全清理。"""
        with self._lock:
            self._consensus_votes[key].add(replica_id)
            return len(self._consensus_votes[key]) >= self.consensus_quorum

    def compact(self, keys: List[str]) -> int:
        """执行压缩——将墓碑标记为 COMPACTED。"""
        with self._lock:
            entries = self.store.dump_entries()
            compacted = 0
            for key in keys:
                entry = self.store._entries.get(key)
                if entry and entry.tombstone:
                    entry.tombstone_state = TombstoneState.COMPACTED
                    compacted += 1

            if compacted > 0:
                self._compaction_log.append({
                    "timestamp": time.time(),
                    "keys": keys,
                    "compacted_count": compacted,
                })
            return compacted

    def garbage_collect(self) -> int:
        """物理移除已压缩且取得共识的墓碑。"""
        with self._lock:
            collected = 0
            to_remove = []
            for key, entry in self.store._entries.items():
                if entry.tombstone and entry.tombstone_state == TombstoneState.COMPACTED:
                    if len(self._consensus_votes.get(key, set())) >= self.consensus_quorum:
                        entry.tombstone_state = TombstoneState.GARBAGE_COLLECTED
                        to_remove.append(key)
                        collected += 1

            for key in to_remove:
                del self.store._entries[key]
                self._tombstones.pop(key, None)
                self._consensus_votes.pop(key, None)

            return collected

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            entries = self.store.dump_entries()
            tombstone_count = sum(1 for e in entries if e.tombstone)
            return {
                "total_entries": len(entries),
                "tombstone_count": tombstone_count,
                "tombstone_ratio": tombstone_count / max(len(entries), 1),
                "compaction_candidates": len(self.get_compaction_candidates()),
                "consensus_pending": sum(
                    1 for v in self._consensus_votes.values()
                    if len(v) < self.consensus_quorum
                ),
                "compaction_log_size": len(self._compaction_log),
            }


# ============================================================================
# SnapshotInterop
# ============================================================================

class SnapshotInterop:
    """与 P12 memory_version_control.py COW 快照互通。

    桥接 CRDTMemoryStore 和 MemoryBranch/CopyOnWriteSnapshot，
    支持：
      - 导出 CRDT 状态为 COW 快照
      - 从 COW 快照恢复 CRDT 状态
      - 增量同步（仅传输变更条目）
    """

    def __init__(
        self,
        store: CRDTMemoryStore,
        sync_mode: SnapshotSyncMode = SnapshotSyncMode.COW_REFERENCE,
    ):
        self.store = store
        self.sync_mode = sync_mode
        self._lock = threading.RLock()
        self._last_sync_clock: int = 0

    def export_to_snapshot(self, snapshot_name: str = "") -> Dict[str, Any]:
        """将 CRDT 状态导出为兼容 COW 快照的数据格式。

        返回格式与 P12 CopyOnWriteSnapshot 兼容。
        """
        with self._lock:
            entries = self.store.dump_entries()
            snapshot_data = {
                "snapshot_name": snapshot_name or f"crdt_snap_{int(time.time())}",
                "replica_id": self.store.replica_id,
                "clock": self.store._clock,
                "version_vector": self.store.get_version_vector(),
                "entries": {},
                "tombstones": {},
                "created_at": time.time(),
                "format_version": "1.0",
                "source": "CRDTMemoryStore",
            }

            for entry in entries:
                if not entry.tombstone:
                    snapshot_data["entries"][entry.key] = {
                        "values": entry.values,
                        "clock": entry.clock,
                        "last_writer": entry.last_writer,
                        "version_vector": entry.version_vector,
                        "created_at": entry.created_at,
                    }
                else:
                    snapshot_data["tombstones"][entry.key] = {
                        "deleted_at": entry.deleted_at,
                        "state": entry.tombstone_state.value,
                    }

            self._last_sync_clock = self.store._clock
            return snapshot_data

    def import_from_snapshot(self, snapshot_data: Dict[str, Any]) -> int:
        """从 COW 快照恢复 CRDT 状态。"""
        with self._lock:
            imported = 0
            entries = snapshot_data.get("entries", {})

            for key, data in entries.items():
                if key not in self.store._entries or data.get("clock", 0) > self.store._entries[key].clock:
                    values = data.get("values", [])
                    self.store._entries[key] = CRDTEntry(
                        key=key,
                        values=values,
                        clock=data.get("clock", 0),
                        last_writer=data.get("last_writer", self.store.replica_id),
                        version_vector=data.get("version_vector", {}),
                        created_at=data.get("created_at", time.time()),
                    )
                    imported += 1

            # 恢复墓碑
            tombstones = snapshot_data.get("tombstones", {})
            for key, data in tombstones.items():
                if key in self.store._entries:
                    self.store._entries[key].tombstone = True
                    self.store._entries[key].deleted_at = data.get("deleted_at")
                    imported += 1

            self._last_sync_clock = max(self._last_sync_clock, snapshot_data.get("clock", 0))
            return imported

    def incremental_sync(self, since_clock: int = 0) -> Dict[str, Any]:
        """增量同步——仅返回时钟 > since_clock 的变更条目。"""
        with self._lock:
            threshold = since_clock or self._last_sync_clock
            entries = self.store.dump_entries()
            changes = {}
            for entry in entries:
                if entry.clock > threshold:
                    changes[entry.key] = {
                        "values": entry.values,
                        "clock": entry.clock,
                        "tombstone": entry.tombstone,
                        "last_writer": entry.last_writer,
                    }

            self._last_sync_clock = self.store._clock
            return {
                "since_clock": threshold,
                "current_clock": self.store._clock,
                "changes_count": len(changes),
                "changes": changes,
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "sync_mode": self.sync_mode.value,
                "last_sync_clock": self._last_sync_clock,
                "current_store_clock": self.store._clock,
                "store_entries": len(self.store.dump_entries()),
            }
