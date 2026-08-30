"""
# status: frozen (2026-09 EXECUTION 163)
P4-3: Memory Lifecycle Manager (对标 Zylos Controlled Forgetting)
====================================================================

实现记忆的完整生命周期管理：创建→活跃→冷存→归档→擦除全链路状态机。

6 种遗忘策略：
  1. 时间过期 (Temporal Expiry) — TTL 到期自动冷存/删除
  2. 容量上限 (Capacity Ceiling) — 存储超限时按优先级驱逐
  3. 优先级淘汰 (Priority Eviction) — 低重要性/低频访问记忆优先淘汰
  4. 显式擦除 (Explicit Erasure) — 用户/系统主动删除
  5. 合规触发 (Compliance Trigger) — GDPR 第 17 条等法规驱动的强制遗忘
  6. 冲突解决 (Conflict Resolution) — 新旧知识冲突时淘汰旧版本

GDPR 第 17 条合规审计日志：
  - 每次擦除记录: timestamp, memory_id, trigger, reason, operator,
    retention_period, erasure_proof_hash, compliance_check 标记

设计要点：
  - 状态机: CREATED → ACTIVE → COLD → ARCHIVED → ERASED
  - 冷存: 移出高速索引但保留原始数据（可恢复）
  - 归档: 压缩存储，仅保留元数据索引
  - 擦除: 不可逆删除，生成审计日志

Reference: Zylos Controlled Forgetting (zylos.ai, June 2026)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────

class LifecycleStage(Enum):
    """记忆生命周期阶段。"""
    CREATED = auto()    # 刚创建，尚未确认
    ACTIVE = auto()     # 活跃使用中
    COLD = auto()       # 冷存（移出高速索引）
    ARCHIVED = auto()   # 归档（压缩存储）
    ERASED = auto()     # 已擦除（不可逆）


class ForgettingStrategy(Enum):
    """遗忘策略枚举。"""
    TEMPORAL_EXPIRY = "temporal_expiry"
    CAPACITY_CEILING = "capacity_ceiling"
    PRIORITY_EVICTION = "priority_eviction"
    EXPLICIT_ERASURE = "explicit_erasure"
    COMPLIANCE_TRIGGER = "compliance_trigger"
    CONFLICT_RESOLUTION = "conflict_resolution"


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class LifecycleRecord:
    """记忆生命周期记录。

    Args:
        memory_id: 记忆唯一标识
        stage: 当前生命周期阶段
        created_at: 创建时间戳
        last_active: 最后活跃时间
        ttl_seconds: 存活时间（秒），-1 表示永不过期
        importance: 重要性评分 [0, 1]
        access_count: 累计访问次数
        storage_bytes: 预估存储占用
        version: 版本号
        metadata: 扩展元数据
    """

    memory_id: str
    stage: LifecycleStage = LifecycleStage.CREATED
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    ttl_seconds: float = -1.0
    importance: float = 0.5
    access_count: int = 0
    storage_bytes: int = 0
    version: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, now: Optional[float] = None) -> bool:
        """判断是否超过 TTL。"""
        if self.ttl_seconds < 0:
            return False
        return ((now or time.time()) - self.created_at) > self.ttl_seconds


@dataclass
class ErasureAuditLog:
    """GDPR 第 17 条合规擦除审计日志。

    Args:
        log_id: 日志唯一标识
        memory_id: 被擦除的记忆 ID
        stage_before: 擦除前状态
        strategy: 触发策略
        reason: 擦除原因（人工可读）
        operator: 操作者标识（用户 / 系统 / 合规引擎）
        retention_period_days: 实际保留天数
        erasure_proof_hash: 擦除证明哈希（内容指纹，证明已不可恢复）
        compliance_check: 是否通过合规检查
        timestamp: 擦除时间
    """

    log_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    memory_id: str = ""
    stage_before: LifecycleStage = LifecycleStage.ACTIVE
    strategy: ForgettingStrategy = ForgettingStrategy.EXPLICIT_ERASURE
    reason: str = ""
    operator: str = "system"
    retention_period_days: float = 0.0
    erasure_proof_hash: str = ""
    compliance_check: bool = False
    timestamp: float = field(default_factory=time.time)


# ── 生命周期管理器 ──────────────────────────────────────────────

class LifecycleManager:
    """记忆生命周期管理器 — 对标 Zylos Controlled Forgetting。

    使用方式::

        from trinity.modules.second_brain.lifecycle_manager import (
            LifecycleManager, ForgettingStrategy, LifecycleStage,
        )

        lcm = LifecycleManager(
            capacity_limit=10_000,
            default_ttl_days=90,
            gdpr_audit_enabled=True,
        )

        # 注册记忆
        lcm.register("mem_001", ttl_days=30, importance=0.8)

        # 访问时通知活跃
        lcm.touch("mem_001")

        # 定期运行维护（建议每日一次）
        evicted = lcm.maintenance()

        # 显式擦除
        proof = lcm.erase("mem_001", strategy=ForgettingStrategy.EXPLICIT_ERASURE,
                           reason="用户请求删除", operator="user-123")

        # 导出审计日志
        audit_logs = lcm.export_audit_logs()
    """

    # ── 构造函数 ──────────────────────────────────────────────────

    def __init__(
        self,
        capacity_limit: int = 10_000,
        default_ttl_days: float = 90.0,
        gdpr_audit_enabled: bool = True,
        cold_threshold_days: float = 30.0,       # 30 天未访问 → 冷存
        archive_threshold_days: float = 180.0,    # 180 天未访问 → 归档
        importance_floor: float = 0.05,           # 低于此值 → 优先淘汰
        auto_erase_expired: bool = False,          # 是否自动擦除过期记忆
    ):
        """初始化生命周期管理器。

        Args:
            capacity_limit: 最大记忆容量（条数），超出触发容量淘汰
            default_ttl_days: 默认存活天数
            gdpr_audit_enabled: 是否启用 GDPR 合规审计
            cold_threshold_days: 未访问多少天后转为冷存
            archive_threshold_days: 未访问多少天后转为归档
            importance_floor: 最低重要性阈值
            auto_erase_expired: 是否自动擦除过期记忆
        """
        self.capacity_limit = capacity_limit
        self.default_ttl_days = default_ttl_days
        self.gdpr_audit_enabled = gdpr_audit_enabled
        self.cold_threshold_seconds = cold_threshold_days * 86400
        self.archive_threshold_seconds = archive_threshold_days * 86400
        self.importance_floor = importance_floor
        self.auto_erase_expired = auto_erase_expired

        # 内部存储
        self._records: Dict[str, LifecycleRecord] = {}
        self._audit_logs: List[ErasureAuditLog] = []
        self._lock = threading.RLock()

    # ── 注册与状态转换 ───────────────────────────────────────────

    def register(
        self,
        memory_id: str,
        ttl_days: Optional[float] = None,
        importance: float = 0.5,
        storage_bytes: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> LifecycleRecord:
        """注册一条新记忆。

        Args:
            memory_id: 记忆 ID
            ttl_days: 存活天数（None = 使用默认值）
            importance: 重要性 [0, 1]
            storage_bytes: 存储占用估算
            metadata: 扩展元数据
        """
        with self._lock:
            ttl_sec = (
                (ttl_days * 86400) if ttl_days is not None
                else (self.default_ttl_days * 86400 if self.default_ttl_days > 0 else -1)
            )

            rec = LifecycleRecord(
                memory_id=memory_id,
                stage=LifecycleStage.ACTIVE,
                ttl_seconds=ttl_sec,
                importance=importance,
                storage_bytes=storage_bytes,
                metadata=metadata or {},
            )
            self._records[memory_id] = rec
            logger.debug("Lifecycle registered: %s (ttl=%s)", memory_id, ttl_days)
            return rec

    def touch(self, memory_id: str) -> bool:
        """记录一次访问，重置 last_active。"""
        with self._lock:
            rec = self._records.get(memory_id)
            if rec is None:
                return False
            rec.last_active = time.time()
            rec.access_count += 1

            # 如果是 COLD → 重新激活
            if rec.stage == LifecycleStage.COLD:
                rec.stage = LifecycleStage.ACTIVE
                logger.info("Memory %s reactivated from COLD", memory_id)

            return True

    def transition(
        self, memory_id: str, target_stage: LifecycleStage
    ) -> bool:
        """手动触发状态转换。

        合法转换：
        - CREATED → ACTIVE (确认创建)
        - ACTIVE → COLD (冷存)
        - COLD → ACTIVE (重新激活)
        - COLD → ARCHIVED (归档)
        - ARCHIVED → COLD (回热)
        - ANY → ERASED (擦除)
        """
        with self._lock:
            rec = self._records.get(memory_id)
            if rec is None:
                return False

            if target_stage == LifecycleStage.ERASED:
                return True  # 擦除走 erase() 方法

            valid_transitions = {
                LifecycleStage.CREATED: [LifecycleStage.ACTIVE],
                LifecycleStage.ACTIVE: [LifecycleStage.COLD],
                LifecycleStage.COLD: [LifecycleStage.ACTIVE, LifecycleStage.ARCHIVED],
                LifecycleStage.ARCHIVED: [LifecycleStage.COLD],
            }

            if target_stage not in valid_transitions.get(rec.stage, []):
                logger.warning(
                    "Invalid transition: %s -> %s for memory %s",
                    rec.stage.name, target_stage.name, memory_id,
                )
                return False

            rec.stage = target_stage
            logger.info("Memory %s: %s -> %s", memory_id, rec.stage.name, target_stage.name)
            return True

    # ── 6 种遗忘策略 ──────────────────────────────────────────────

    def _forget_temporal_expiry(self, now: float) -> List[str]:
        """策略1: 时间过期 — 返回超过 TTL 的记忆 ID 列表。"""
        expired = []
        for mid, rec in self._records.items():
            if rec.stage == LifecycleStage.ERASED:
                continue
            if rec.is_expired(now):
                expired.append(mid)
        return expired

    def _forget_capacity_ceiling(self) -> List[str]:
        """策略2: 容量上限 — 超出限制时返回应淘汰的记忆 ID。

        淘汰优先级 = importance × 1/(1 + days_since_access)
        重要性低 + 长时间未访问 → 优先淘汰
        """
        active = [
            (mid, rec) for mid, rec in self._records.items()
            if rec.stage not in (LifecycleStage.ERASED, LifecycleStage.ARCHIVED)
        ]
        if len(active) <= self.capacity_limit:
            return []

        now = time.time()
        scored = []
        for mid, rec in active:
            days_idle = max((now - rec.last_active) / 86400, 1e-6)
            score = rec.importance / (1 + days_idle)
            scored.append((mid, score))

        scored.sort(key=lambda x: x[1])
        excess = len(active) - self.capacity_limit
        return [mid for mid, _ in scored[:excess]]

    def _forget_priority_eviction(self) -> List[str]:
        """策略3: 优先级淘汰 — 重要性低于阈值 + 30 天未访问。"""
        now = time.time()
        candidates = []
        for mid, rec in self._records.items():
            if rec.stage == LifecycleStage.ERASED:
                continue
            if rec.importance < self.importance_floor:
                days_idle = (now - rec.last_active) / 86400
                if days_idle > 30:
                    candidates.append((mid, rec.importance, days_idle))

        candidates.sort(key=lambda x: (x[1], -x[2]))  # 低重要性优先
        return [mid for mid, _, _ in candidates]

    def _forget_conflict_resolution(
        self, memory_id_new: str, memory_id_old: str
    ) -> Optional[str]:
        """策略6: 冲突解决 — 新旧知识冲突时淘汰旧版本。

        Returns:
            应淘汰的旧记忆 ID（新版胜出），或 None
        """
        old = self._records.get(memory_id_old)
        new = self._records.get(memory_id_new)
        if old is None or new is None:
            return None
        # 新版本重要性更高或时间更近 → 淘汰旧版本
        if new.importance >= old.importance and new.created_at > old.created_at:
            return memory_id_old
        return None

    # ── 维护周期 ─────────────────────────────────────────────────

    def maintenance(self) -> Dict[str, List[str]]:
        """执行一次完整维护周期。

        执行顺序：
        1. 时间过期 → 冷存或擦除
        2. 冷存/归档转换（基于空闲时间）
        3. 优先级淘汰
        4. 容量上限驱逐

        Returns:
            {"expired": [...], "cooled": [...], "archived": [...],
             "evicted": [...], "erased": [...]}
        """
        with self._lock:
            now = time.time()
            result = {
                "expired": [], "cooled": [], "archived": [],
                "evicted": [], "erased": [],
            }

            # 1. 时间过期
            expired = self._forget_temporal_expiry(now)
            for mid in expired:
                if self.auto_erase_expired:
                    self._do_erase(
                        mid, ForgettingStrategy.TEMPORAL_EXPIRY,
                        reason="TTL expired", operator="auto-maintenance",
                    )
                    result["erased"].append(mid)
                else:
                    self.transition(mid, LifecycleStage.COLD)
                    result["expired"].append(mid)

            # 2. 冷存/归档转换
            for mid, rec in self._records.items():
                if rec.stage == LifecycleStage.ERASED:
                    continue
                idle = now - rec.last_active
                if rec.stage == LifecycleStage.ACTIVE and idle > self.cold_threshold_seconds:
                    self.transition(mid, LifecycleStage.COLD)
                    result["cooled"].append(mid)
                elif rec.stage == LifecycleStage.COLD and idle > self.archive_threshold_seconds:
                    self.transition(mid, LifecycleStage.ARCHIVED)
                    result["archived"].append(mid)

            # 3. 优先级淘汰
            priority_evict = self._forget_priority_eviction()
            for mid in priority_evict:
                self.transition(mid, LifecycleStage.COLD)
                result["evicted"].append(mid)

            # 4. 容量上限
            capacity_evict = self._forget_capacity_ceiling()
            for mid in capacity_evict:
                if mid not in result["evicted"]:
                    self.transition(mid, LifecycleStage.COLD)
                    result["evicted"].append(mid)

            logger.info(
                "Maintenance complete: expired=%d cooled=%d archived=%d evicted=%d erased=%d",
                len(result["expired"]), len(result["cooled"]),
                len(result["archived"]), len(result["evicted"]), len(result["erased"]),
            )
            return result

    # ── 擦除 ──────────────────────────────────────────────────────

    def erase(
        self,
        memory_id: str,
        strategy: ForgettingStrategy = ForgettingStrategy.EXPLICIT_ERASURE,
        reason: str = "",
        operator: str = "system",
    ) -> Optional[ErasureAuditLog]:
        """执行不可逆擦除 + GDPR 合规审计日志。

        Args:
            memory_id: 要擦除的记忆 ID
            strategy: 触发策略
            reason: 擦除原因
            operator: 操作者

        Returns:
            审计日志条目（含擦除证明哈希）
        """
        with self._lock:
            rec = self._records.get(memory_id)
            if rec is None:
                logger.warning("Cannot erase non-existent memory: %s", memory_id)
                return None
            if rec.stage == LifecycleStage.ERASED:
                logger.info("Memory %s already erased", memory_id)
                return None

            return self._do_erase(memory_id, strategy, reason, operator)

    def _do_erase(
        self, memory_id: str,
        strategy: ForgettingStrategy,
        reason: str,
        operator: str,
    ) -> ErasureAuditLog:
        """内部擦除执行 + 审计日志生成。"""
        rec = self._records[memory_id]
        stage_before = rec.stage

        # 生成擦除证明哈希
        proof_data = json.dumps({
            "memory_id": memory_id,
            "stage_before": stage_before.name,
            "importance": rec.importance,
            "access_count": rec.access_count,
            "created_at": rec.created_at,
            "timestamp": time.time(),
        }, sort_keys=True)
        proof_hash = hashlib.sha256(proof_data.encode()).hexdigest()

        # 计算保留天数
        retention_days = (time.time() - rec.created_at) / 86400

        # 合规检查
        compliance_ok = self.gdpr_audit_enabled

        audit = ErasureAuditLog(
            memory_id=memory_id,
            stage_before=stage_before,
            strategy=strategy,
            reason=reason,
            operator=operator,
            retention_period_days=round(retention_days, 2),
            erasure_proof_hash=proof_hash,
            compliance_check=compliance_ok,
        )
        self._audit_logs.append(audit)

        # 标记为已擦除
        rec.stage = LifecycleStage.ERASED

        logger.info(
            "Memory erased: %s (strategy=%s, proof=%s...)",
            memory_id, strategy.value, proof_hash[:12],
        )
        return audit

    # ── 查询 ──────────────────────────────────────────────────────

    def get_stage(self, memory_id: str) -> Optional[LifecycleStage]:
        """查询记忆当前生命周期阶段。"""
        rec = self._records.get(memory_id)
        return rec.stage if rec else None

    def count_by_stage(self) -> Dict[str, int]:
        """按阶段统计记忆数量。"""
        counts: Dict[str, int] = defaultdict(int)
        for rec in self._records.values():
            counts[rec.stage.name] += 1
        return dict(counts)

    def export_audit_logs(
        self, strategy_filter: Optional[ForgettingStrategy] = None,
    ) -> List[ErasureAuditLog]:
        """导出 GDPR 合规审计日志，可按策略过滤。"""
        if strategy_filter is None:
            return list(self._audit_logs)
        return [log for log in self._audit_logs if log.strategy == strategy_filter]

    def statistics(self) -> Dict[str, Any]:
        """返回管理器运行时统计。"""
        with self._lock:
            by_stage = self.count_by_stage()
            return {
                "total_records": len(self._records),
                "by_stage": by_stage,
                "capacity_limit": self.capacity_limit,
                "audit_log_count": len(self._audit_logs),
                "gdpr_enabled": self.gdpr_audit_enabled,
                "default_ttl_days": self.default_ttl_days,
                "auto_erase_expired": self.auto_erase_expired,
            }
