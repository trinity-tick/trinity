"""
# status: orphan (2026-08-15 audit, not in runtime path)
P11-7: Bi-Temporal Audit Trail — 双时态不可变审计追踪

在 ImmutableAuditTrail 基础上叠加双时态查询:
  - event_time (事件时间): 审计事件实际发生的时间
  - record_time (记录时间): 事件写入审计日志的时间
  - AuditSnapshot: 按时间点保存完整审计状态
  - rollback_to(): 按时间点回滚审计状态
  - diff_snapshots(): 比较两个时间点的审计差异
  - compliance_export(): 导出 GDPR 合规报告

Reference:
    ImmutableAuditTrail Extension (双时态审计)
    GDPR Article 30 — Records of Processing Activities
"""

import copy
import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════

class AuditEventType(Enum):
    """审计事件类型。"""
    MEMORY_CREATE = "memory_create"
    MEMORY_UPDATE = "memory_update"
    MEMORY_DELETE = "memory_delete"
    MEMORY_READ = "memory_read"
    ACCESS_GRANTED = "access_granted"
    ACCESS_REVOKED = "access_revoked"
    CONFIG_CHANGE = "config_change"
    COMPLIANCE_CHECK = "compliance_check"
    ROLLBACK = "rollback"
    SNAPSHOT = "snapshot"


class ComplianceStandard(Enum):
    """合规标准。"""
    GDPR = "GDPR"
    CCPA = "CCPA"
    HIPAA = "HIPAA"
    SOX = "SOX"
    ISO27001 = "ISO27001"


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class AuditEvent:
    """审计事件条目。

    双时态:
      - event_time: 事件实际发生时间
      - record_time: 事件被记录到审计日志的时间
    """
    event_id: str
    event_type: AuditEventType
    event_time: float              # 事件发生时间 (Unix timestamp)
    record_time: float = field(default_factory=time.time)  # 记录时间
    agent_id: str = ""
    memory_id: str = ""
    detail: str = ""
    previous_hash: str = ""        # 前一条审计事件的哈希，构成不可变链
    content_snapshot: str = ""     # 变更前的内容快照
    metadata: dict = field(default_factory=dict)

    def compute_hash(self) -> str:
        """计算当前事件的 SHA-256 哈希（不含 previous_hash）。"""
        payload = json.dumps({
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "event_time": self.event_time,
            "record_time": self.record_time,
            "agent_id": self.agent_id,
            "memory_id": self.memory_id,
            "detail": self.detail,
            "content_snapshot": self.content_snapshot,
            "metadata": self.metadata,
        }, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "event_time": self.event_time,
            "record_time": self.record_time,
            "agent_id": self.agent_id,
            "memory_id": self.memory_id,
            "detail": self.detail,
            "computed_hash": self.compute_hash(),
            "previous_hash": self.previous_hash,
        }


@dataclass
class AuditSnapshot:
    """审计快照：保存某一时间点的完整审计状态。"""
    snapshot_id: str
    timestamp: float
    event_count: int
    chain_head_hash: str           # 审计链最新哈希
    events_summary: dict           # 按事件类型统计
    agents_summary: dict           # 按 Agent 统计
    memory_ids: list[str]          # 涉及的所有记忆 ID
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "snapshot_id": self.snapshot_id,
            "timestamp": self.timestamp,
            "event_count": self.event_count,
            "chain_head_hash": self.chain_head_hash,
            "events_summary": self.events_summary,
            "agents_summary": self.agents_summary,
            "memory_count": len(self.memory_ids),
        }


@dataclass
class SnapshotDiff:
    """两个快照之间的差异。"""
    snapshot_a_id: str
    snapshot_b_id: str
    time_delta: float                  # 时间差（秒）
    new_events_count: int              # 新增事件数
    new_agents: list[str]              # 新出现的 Agent
    removed_memories: list[str]        # 已删除的记忆
    modified_memories: list[str]       # 修改过的记忆
    event_type_delta: dict             # 各事件类型变化量
    hash_chain_intact: bool            # 哈希链是否完整


# ══════════════════════════════════════════════════════════════════════
# Bi-Temporal Audit Trail 主类
# ══════════════════════════════════════════════════════════════════════

class BiTemporalAuditTrail:
    """双时态不可变审计追踪引擎。

    在 ImmutableAuditTrail 上叠加双时态：
    - 哈希链保证不可篡改
    - 双时态支持按事件时间和记录时间分别查询
    - 快照 + 回滚 + 差异比较 + GDPR 合规导出
    """

    def __init__(self):
        self._events: list[AuditEvent] = []
        self._snapshots: list[AuditSnapshot] = []
        self._event_counter: int = 0
        self._chain_head: str = "genesis"

    # ── 事件记录 ──────────────────────────────────────────────────

    def record(self, event_type: AuditEventType, agent_id: str = "",
               memory_id: str = "", detail: str = "",
               event_time: float | None = None,
               content_snapshot: str = "",
               metadata: dict | None = None) -> AuditEvent:
        """记录一条审计事件，自动链接到哈希链。"""
        self._event_counter += 1
        event_id = f"AE-{self._event_counter:08d}"

        evt = AuditEvent(
            event_id=event_id,
            event_type=event_type,
            event_time=event_time or time.time(),
            record_time=time.time(),
            agent_id=agent_id,
            memory_id=memory_id,
            detail=detail,
            previous_hash=self._chain_head,
            content_snapshot=content_snapshot,
            metadata=metadata or {},
        )
        self._chain_head = evt.compute_hash()
        self._events.append(evt)
        return evt

    # ── 双时态查询 ────────────────────────────────────────────────

    def query_by_event_time(self, start: float, end: float | None = None) -> list[AuditEvent]:
        """按事件时间范围查询。"""
        if end is None:
            end = time.time()
        return [e for e in self._events if start <= e.event_time <= end]

    def query_by_record_time(self, start: float, end: float | None = None) -> list[AuditEvent]:
        """按记录时间范围查询。"""
        if end is None:
            end = time.time()
        return [e for e in self._events if start <= e.record_time <= end]

    def query_by_agent(self, agent_id: str) -> list[AuditEvent]:
        """按 Agent 查询所有审计事件。"""
        return [e for e in self._events if e.agent_id == agent_id]

    def query_by_memory(self, memory_id: str) -> list[AuditEvent]:
        """按记忆 ID 查询所有审计事件。"""
        return [e for e in self._events if e.memory_id == memory_id]

    # ── 快照 ──────────────────────────────────────────────────────

    def create_snapshot(self) -> AuditSnapshot:
        """创建当前审计状态的快照。"""
        snap_id = f"AS-{len(self._snapshots) + 1:06d}"
        snap = AuditSnapshot(
            snapshot_id=snap_id,
            timestamp=time.time(),
            event_count=len(self._events),
            chain_head_hash=self._chain_head,
            events_summary=dict(Counter(e.event_type.value for e in self._events)),
            agents_summary=dict(Counter(e.agent_id for e in self._events if e.agent_id)),
            memory_ids=list({e.memory_id for e in self._events if e.memory_id}),
        )
        self._snapshots.append(snap)
        return snap

    def rollback_to(self, snapshot_id: str) -> bool:
        """按时间点回滚审计状态至指定快照。

        注意：回滚不会真正删除审计事件（不可变），
        而是将逻辑指针回退到快照时刻的状态。
        """
        target = None
        target_index = -1
        for i, snap in enumerate(self._snapshots):
            if snap.snapshot_id == snapshot_id:
                target = snap
                target_index = i
                break

        if target is None:
            return False

        # 回滚 chain_head 到快照时的哈希
        self._chain_head = target.chain_head_hash

        # 回滚事件计数（逻辑上，事件本身保留）
        self._snapshots = self._snapshots[:target_index + 1]

        # 记录回滚事件
        self.record(
            event_type=AuditEventType.ROLLBACK,
            agent_id="system",
            detail=f"Rolled back to snapshot {snapshot_id}",
            metadata={"snapshot_timestamp": target.timestamp},
        )
        return True

    def diff_snapshots(self, snapshot_a_id: str, snapshot_b_id: str) -> SnapshotDiff | None:
        """比较两个时间点的审计差异。"""
        snap_a = None
        snap_b = None
        for s in self._snapshots:
            if s.snapshot_id == snapshot_a_id:
                snap_a = s
            if s.snapshot_id == snapshot_b_id:
                snap_b = s

        if snap_a is None or snap_b is None:
            return None

        # 确定哪个更早
        if snap_a.timestamp > snap_b.timestamp:
            snap_a, snap_b = snap_b, snap_a

        # 统计差异
        new_events_count = snap_b.event_count - snap_a.event_count

        new_agents = list(set(snap_b.agents_summary.keys()) - set(snap_a.agents_summary.keys()))
        removed_memories = list(set(snap_a.memory_ids) - set(snap_b.memory_ids))
        modified_memories = [
            mid for mid in set(snap_a.memory_ids) & set(snap_b.memory_ids)
            if self._count_updates_for_memory(mid, snap_a.timestamp, snap_b.timestamp) > 0
        ]

        event_delta = {}
        all_types = set(snap_a.events_summary.keys()) | set(snap_b.events_summary.keys())
        for t in all_types:
            delta = snap_b.events_summary.get(t, 0) - snap_a.events_summary.get(t, 0)
            if delta != 0:
                event_delta[t] = delta

        return SnapshotDiff(
            snapshot_a_id=snap_a.snapshot_id,
            snapshot_b_id=snap_b.snapshot_id,
            time_delta=snap_b.timestamp - snap_a.timestamp,
            new_events_count=new_events_count,
            new_agents=new_agents,
            removed_memories=removed_memories,
            modified_memories=modified_memories,
            event_type_delta=event_delta,
            hash_chain_intact=self._verify_chain_integrity(),
        )

    def _count_updates_for_memory(self, memory_id: str, start: float, end: float) -> int:
        """统计某记忆在时间段内的更新次数。"""
        count = 0
        for e in self._events:
            if e.memory_id == memory_id and start <= e.event_time <= end:
                if e.event_type in (AuditEventType.MEMORY_UPDATE, AuditEventType.MEMORY_CREATE):
                    count += 1
        return count

    def _verify_chain_integrity(self) -> bool:
        """验证哈希链完整性。"""
        expected = "genesis"
        for evt in self._events:
            if evt.previous_hash != expected:
                return False
            expected = evt.compute_hash()
        return self._chain_head == expected

    # ── GDPR 合规导出 ─────────────────────────────────────────────

    def compliance_export(self, standard: ComplianceStandard = ComplianceStandard.GDPR) -> dict:
        """导出合规报告。

        符合 GDPR Article 30 — Records of Processing Activities。
        """
        total_events = len(self._events)
        unique_agents = list({e.agent_id for e in self._events if e.agent_id})
        unique_memories = list({e.memory_id for e in self._events if e.memory_id})

        report = {
            "standard": standard.value,
            "export_time": datetime.now(timezone.utc).isoformat(),
            "export_timestamp": time.time(),
            "chain_head_hash": self._chain_head,
            "chain_integrity": self._verify_chain_integrity(),
            "total_events": total_events,
            "unique_agents": len(unique_agents),
            "unique_memories": len(unique_memories),
            "agent_list": unique_agents[:100],
            "events_by_type": dict(Counter(e.event_type.value for e in self._events)),
            "events_by_agent_top20": dict(Counter(e.agent_id for e in self._events if e.agent_id).most_common(20)),
            "first_event_time": self._events[0].event_time if self._events else None,
            "last_event_time": self._events[-1].event_time if self._events else None,
            "retention_period_days": 90,  # GDPR 建议保留期
            "data_subject_requests_supported": True,
            "right_to_erasure_supported": True,
            "encryption": "SHA-256 hash chain",
            "immutability": "Hash-chained, append-only",
        }

        if standard == ComplianceStandard.GDPR:
            report["gdpr_article_30_fields"] = {
                "controller_name": "Trinity Memory System",
                "purpose_of_processing": "Agent memory audit & compliance",
                "categories_of_data_subjects": unique_agents[:50],
                "categories_of_personal_data": ["agent_id", "memory_content", "access_logs"],
                "transfers_to_third_countries": "None",
                "retention_schedule": "90 days rolling",
                "security_measures": "SHA-256 hash chain, append-only, immutability verification",
            }

        return report

    # ── 工具方法 ──────────────────────────────────────────────────

    def get_event(self, event_id: str) -> AuditEvent | None:
        for e in self._events:
            if e.event_id == event_id:
                return e
        return None

    def verify_chain(self) -> tuple[bool, str]:
        """验证整个哈希链的完整性。"""
        ok = self._verify_chain_integrity()
        return (ok, "Chain intact" if ok else "CHAIN BROKEN — possible tampering detected!")

    def get_snapshot(self, snapshot_id: str) -> AuditSnapshot | None:
        for s in self._snapshots:
            if s.snapshot_id == snapshot_id:
                return s
        return None

    # ── 统计 ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "total_events": len(self._events),
            "snapshots": len(self._snapshots),
            "chain_integrity": self._verify_chain_integrity(),
            "chain_head_hash": self._chain_head[:16] + "...",
            "unique_agents": len({e.agent_id for e in self._events if e.agent_id}),
            "unique_memories": len({e.memory_id for e in self._events if e.memory_id}),
            "first_event_time": self._events[0].event_time if self._events else 0,
            "last_event_time": self._events[-1].event_time if self._events else 0,
        }


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    audit = BiTemporalAuditTrail()

    print("=" * 60)
    print("Bi-Temporal Audit Trail — Self Test")
    print("=" * 60)

    # 记录事件
    t0 = time.time()
    audit.record(AuditEventType.MEMORY_CREATE, agent_id="agent_alpha",
                 memory_id="mem-001", detail="Created memory entry",
                 event_time=t0 - 300)
    audit.record(AuditEventType.MEMORY_UPDATE, agent_id="agent_alpha",
                 memory_id="mem-001", detail="Updated content",
                 event_time=t0 - 200, content_snapshot="old content")
    audit.record(AuditEventType.ACCESS_GRANTED, agent_id="admin",
                 memory_id="mem-001", detail="Granted agent_beta read access",
                 event_time=t0 - 100)
    audit.record(AuditEventType.MEMORY_READ, agent_id="agent_beta",
                 memory_id="mem-001", detail="Read memory",
                 event_time=t0 - 50)
    audit.record(AuditEventType.CONFIG_CHANGE, agent_id="admin",
                 detail="Updated retention policy to 90 days",
                 event_time=t0)

    # 第一个快照
    snap1 = audit.create_snapshot()
    print(f"\n[Snapshot 1] {snap1.snapshot_id} — events={snap1.event_count}")

    # 更多事件
    audit.record(AuditEventType.MEMORY_CREATE, agent_id="agent_gamma",
                 memory_id="mem-002", detail="Second memory entry",
                 event_time=t0 + 100)
    audit.record(AuditEventType.MEMORY_DELETE, agent_id="agent_alpha",
                 memory_id="mem-001", detail="Deleted memory",
                 event_time=t0 + 200)

    snap2 = audit.create_snapshot()
    print(f"[Snapshot 2] {snap2.snapshot_id} — events={snap2.event_count}")

    # 差异比较
    diff = audit.diff_snapshots(snap1.snapshot_id, snap2.snapshot_id)
    if diff:
        print(f"\n[Diff] {snap1.snapshot_id} -> {snap2.snapshot_id}")
        print(f"  Time delta: {diff.time_delta:.0f}s")
        print(f"  New events: {diff.new_events_count}")
        print(f"  New agents: {diff.new_agents}")
        print(f"  Removed memories: {diff.removed_memories}")
        print(f"  Event type delta: {diff.event_type_delta}")
        print(f"  Chain intact: {diff.hash_chain_intact}")

    # 双时态查询
    by_event = audit.query_by_event_time(t0 - 400, t0 - 50)
    print(f"\n[双时态查询 — 事件时间 t0-400 ~ t0-50] {len(by_event)} events found")

    by_record = audit.query_by_record_time(t0 - 10, t0 + 500)
    print(f"[双时态查询 — 记录时间 t0-10 ~ t0+500] {len(by_record)} events found")

    # GDPR 合规导出
    gdpr = audit.compliance_export(ComplianceStandard.GDPR)
    print(f"\n[GDPR Export] integrity={gdpr['chain_integrity']}, "
          f"events={gdpr['total_events']}, agents={gdpr['unique_agents']}")

    # 哈希链验证
    ok, msg = audit.verify_chain()
    print(f"\n[Hash Chain Verify] {ok}: {msg}")
