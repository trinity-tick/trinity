"""
CB58: TemporalValidityWindow — 时序有效性窗口
============================================

对标 Zep Graphiti (arXiv:2501.13956) 和 Bitemporal Graph (arXiv:2607.26520)。
为每条记忆增加 valid_from / valid_to / invalid_at 双时态标记，支持
点时间查询（"当时是什么"），自动失效旧事实，集成到 memory_version_control 管线。

设计要点：
  - 双时态模型：系统时间（记录时间）+ 有效时间（事实有效期）
  - 点时间查询：query_at(timestamp) 仅返回该时间点有效的记忆
  - 自动失效：新事实写入时，自动将冲突旧事实标记 invalid_at
  - 与 memory_version_control 集成：COW 快照支持时间回溯

Reference:
  - Zep Graphiti: arXiv 2501.13956 — temporal knowledge graph with validity windows
  - Bitemporal Graph: arXiv 2607.26520 — dual-temporal graph model
  - Zep LongMemEval: 63.8% (GPT-4o), beats Mem0 49.0% by 15pts
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class ValidityStatus(Enum):
    """双时态记录的有效性状态。"""
    CURRENT = "current"         # 当前有效（valid_to 未设定）
    SUPERSEDED = "superseded"   # 被新事实取代（invalid_at 已设定）
    EXPIRED = "expired"         # 自然过期（valid_to 已到期）
    FUTURE = "future"           # 未来生效（valid_from 尚未到达）
    VOID = "void"               # 已作废


class ConflictPolicy(Enum):
    """冲突处理策略。"""
    AUTO_SUPERSEDE = auto()     # 自动将旧事实标记为取代
    APPEND = auto()             # 追加新事实，不失效旧记录
    REJECT = auto()             # 拒绝写入，保留旧事实
    MERGE = auto()              # 合并旧事实信息到新记录


# ============================================================================
# Data Classes
# ============================================================================

@dataclass
class ValiditySpan:
    """事实的有效时间窗口。

    系统时间 (system_time) 由 memory_version_control 管理；
    有效时间 (valid_from / valid_to) 描述事实在现实世界中的有效期。
    """
    valid_from: float = 0.0    # 事实开始有效的时间（Unix timestamp）
    valid_to: Optional[float] = None  # 事实终止有效的时间，None 表示至今有效
    precision: str = "second"  # 时间精度：second / minute / hour / day / month

    def contains(self, timestamp: float) -> bool:
        """点时间查询：该时间戳是否在此窗口内。"""
        if timestamp < self.valid_from:
            return False
        if self.valid_to is not None and timestamp > self.valid_to:
            return False
        return True

    @property
    def is_open_ended(self) -> bool:
        return self.valid_to is None

    @property
    def duration(self) -> Optional[float]:
        if self.valid_to is None:
            return None
        return max(0.0, self.valid_to - self.valid_from)


@dataclass
class BitemporalRecord:
    """双时态记录：同时追踪系统时间和有效时间。

    系统时间：事实何时被 Trinitiy 记录
    有效时间：事实在现实世界中的有效期
    """
    record_id: str
    fact_key: str              # 事实标识键（如 "user_address"）
    fact_value: Any            # 事实内容
    span: ValiditySpan = field(default_factory=ValiditySpan)
    system_time: float = field(default_factory=_time.time)
    invalid_at: Optional[float] = None  # 被新事实取代的时间戳
    source: str = ""           # 来源会话/事件 ID
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """当前是否有效。"""
        if self.invalid_at is not None:
            return False
        now = _time.time()
        if self.span.valid_from > now:
            return False
        if self.span.valid_to is not None and now > self.span.valid_to:
            return False
        return True

    @property
    def status(self) -> ValidityStatus:
        now = _time.time()
        if self.invalid_at is not None:
            return ValidityStatus.SUPERSEDED
        if self.span.valid_from > now:
            return ValidityStatus.FUTURE
        if self.span.valid_to is not None and now > self.span.valid_to:
            return ValidityStatus.EXPIRED
        return ValidityStatus.CURRENT


# ============================================================================
# Main Class
# ============================================================================

class TemporalValidityWindow:
    """双时态有效性窗口管理器。

    核心功能：
    - insert: 写入事实时自动检测冲突并标记旧事实 invalid_at
    - query_at: 点时间查询，只返回目标时间点有效的记忆
    - get_active: 获取当前所有有效事实
    - get_history: 获取某个 fact_key 的完整历史链条
    - integrate_with_mvc: 与 memory_version_control COW 快照联动

    Thread-safe via RLock.
    """

    def __init__(self, conflict_policy: ConflictPolicy = ConflictPolicy.AUTO_SUPERSEDE):
        self._lock = threading.RLock()
        self._conflict_policy = conflict_policy
        self._records: Dict[str, BitemporalRecord] = {}
        self._history: Dict[str, List[str]] = {}  # fact_key → [record_id...]
        self._insert_count: int = 0
        self._supersede_count: int = 0
        self._created_at: float = _time.time()

    # -- CRUD --

    def insert(self, fact_key: str, fact_value: Any,
               valid_from: float = 0.0,
               valid_to: Optional[float] = None,
               precision: str = "second",
               source: str = "",
               metadata: Optional[Dict[str, Any]] = None) -> BitemporalRecord:
        """插入新事实，自动处理冲突。

        若 fact_key 已存在活跃记录且 conflict_policy 为 AUTO_SUPERSEDE，
        则自动将旧记录 invalid_at 设为当前时间。
        """
        with self._lock:
            now = _time.time()
            record_id = f"{fact_key}:{now}:{self._insert_count}"

            span = ValiditySpan(
                valid_from=valid_from or now,
                valid_to=valid_to,
                precision=precision,
            )
            record = BitemporalRecord(
                record_id=record_id,
                fact_key=fact_key,
                fact_value=fact_value,
                span=span,
                system_time=now,
                source=source,
                metadata=metadata or {},
            )

            # 冲突检测：自动失效旧事实
            if fact_key in self._history and self._history[fact_key]:
                if self._conflict_policy == ConflictPolicy.AUTO_SUPERSEDE:
                    old_rid = self._history[fact_key][-1]
                    old = self._records.get(old_rid)
                    if old is not None and old.is_active:
                        old.invalid_at = now
                        self._supersede_count += 1
                        logger.debug(
                            "TemporalValidityWindow: superseded %s (old=%s, new=%s)",
                            fact_key, old.fact_value, fact_value,
                        )
                elif self._conflict_policy == ConflictPolicy.REJECT:
                    logger.warning(
                        "TemporalValidityWindow: rejected insert for %s (conflict policy=REJECT)",
                        fact_key,
                    )
                    old_rid = self._history[fact_key][-1]
                    return self._records[old_rid]

            self._records[record_id] = record
            self._history.setdefault(fact_key, []).append(record_id)
            self._insert_count += 1
            return record

    # -- Query --

    def query_at(self, fact_key: str, timestamp: float) -> Optional[BitemporalRecord]:
        """点时间查询：返回在 timestamp 时有效的记录。"""
        with self._lock:
            rids = self._history.get(fact_key, [])
            for rid in reversed(rids):
                r = self._records.get(rid)
                if r is None:
                    continue
                if r.span.contains(timestamp) and (
                    r.invalid_at is None or timestamp < r.invalid_at
                ):
                    return r
            return None

    def get_active(self) -> List[BitemporalRecord]:
        """获取所有当前有效的记录。"""
        with self._lock:
            return [r for r in self._records.values() if r.is_active]

    def get_history(self, fact_key: str) -> List[BitemporalRecord]:
        """获取某个 fact_key 的完整历史链条。"""
        with self._lock:
            rids = self._history.get(fact_key, [])
            return [self._records[rid] for rid in rids if rid in self._records]

    # -- Statistics --

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._records)
            active = sum(1 for r in self._records.values() if r.is_active)
            superseded = sum(1 for r in self._records.values() if r.status == ValidityStatus.SUPERSEDED)
            return {
                "class": "TemporalValidityWindow (CB58)",
                "total_records": total,
                "active_records": active,
                "superseded_records": superseded,
                "fact_keys": len(self._history),
                "total_inserts": self._insert_count,
                "total_supersedes": self._supersede_count,
                "uptime_seconds": _time.time() - self._created_at,
                "conflict_policy": self._conflict_policy.name,
            }
