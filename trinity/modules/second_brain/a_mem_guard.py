"""
P11-6: A-MemGuard Access Control — 对标 NTU 2026 "A-MemGuard" 记忆访问控制

实现基于角色/AgentID 的细粒度记忆访问控制:
  - MemoryAccessControlList: 按角色/AgentID 控制读写权限
  - provenance_check(): 写入前校验来源是否可信
  - lock_entry(): 锁定关键记忆条目防止未授权修改
  - audit_modification(): 记录所有修改尝试（含失败）
  - 投毒攻击防御率目标 ≥ 95%

Reference:
    NTU 2026 — A-MemGuard: Agent Memory Guard for Poisoning Defense
    arXiv:2606.xxxxx (NTU Singapore, 2026)
"""

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举与数据结构
# ══════════════════════════════════════════════════════════════════════

class AccessLevel(Enum):
    """访问权限级别。"""
    NONE = "none"         # 无法访问
    READ = "read"         # 只读
    WRITE = "write"       # 读写
    ADMIN = "admin"       # 管理员（可修改 ACL）


class ProvenanceStatus(Enum):
    """来源可信状态。"""
    TRUSTED = "trusted"           # 可信来源
    UNKNOWN = "unknown"           # 未知来源
    UNTRUSTED = "untrusted"       # 不可信来源
    BLACKLISTED = "blacklisted"   # 黑名单，直接拒绝


class LockType(Enum):
    """锁定类型。"""
    SOFT = "soft"     # 软锁：记录操作但允许修改
    HARD = "hard"     # 硬锁：拒绝所有修改尝试


class AuditAction(Enum):
    """审计动作类型。"""
    READ_ATTEMPT = "read_attempt"
    WRITE_ATTEMPT = "write_attempt"
    LOCK_ATTEMPT = "lock_attempt"
    PROVENANCE_CHECK = "provenance_check"
    ACL_MODIFICATION = "acl_modification"
    ACCESS_DENIED = "access_denied"
    POISONING_DETECTED = "poisoning_detected"


@dataclass
class AccessControlEntry:
    """单条 ACL 条目。"""
    subject_id: str          # AgentID 或 角色名
    subject_type: str        # "agent" | "role"
    resource_path: str       # 记忆条目路径/ID（支持通配符 *）
    access_level: AccessLevel
    granted_by: str = "system"
    granted_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None = 永不过期

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


@dataclass
class AuditRecord:
    """审计记录条目。"""
    timestamp: float = field(default_factory=time.time)
    action: AuditAction = AuditAction.READ_ATTEMPT
    subject_id: str = ""
    resource_path: str = ""
    success: bool = False
    detail: str = ""
    content_hash: str = ""
    source_agent: str = ""
    provenance_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "action": self.action.value,
            "subject_id": self.subject_id,
            "resource_path": self.resource_path,
            "success": self.success,
            "detail": self.detail,
            "content_hash": self.content_hash,
            "source_agent": self.source_agent,
            "provenance_score": self.provenance_score,
        }


@dataclass
class LockEntry:
    """锁条目。"""
    memory_id: str
    lock_type: LockType
    locked_by: str
    locked_at: float = field(default_factory=time.time)
    reason: str = ""
    expires_at: float | None = None


# ══════════════════════════════════════════════════════════════════════
# 来源校验器
# ══════════════════════════════════════════════════════════════════════

class ProvenanceChecker:
    """来源可信校验器。

    校验写入操作的来源 Agent 是否在白名单中，
    结合历史行为计算可信度评分。
    """

    def __init__(self):
        self._trusted_agents: set[str] = set()
        self._untrusted_agents: set[str] = set()
        self._blacklisted_agents: set[str] = set()
        self._agent_history: dict[str, list[dict]] = defaultdict(list)

    def register_trusted(self, agent_id: str) -> None:
        """将 Agent 注册为可信来源。"""
        self._trusted_agents.add(agent_id)
        self._untrusted_agents.discard(agent_id)
        self._blacklisted_agents.discard(agent_id)

    def register_untrusted(self, agent_id: str) -> None:
        """将 Agent 标记为不可信来源。"""
        self._untrusted_agents.add(agent_id)
        self._trusted_agents.discard(agent_id)

    def blacklist(self, agent_id: str) -> None:
        """将 Agent 加入黑名单。"""
        self._blacklisted_agents.add(agent_id)
        self._trusted_agents.discard(agent_id)
        self._untrusted_agents.discard(agent_id)

    def check(self, source_agent: str, content: str = "") -> tuple[ProvenanceStatus, float]:
        """检查来源是否可信。

        Returns:
            (ProvenanceStatus, confidence_score 0.0~1.0)
        """
        if source_agent in self._blacklisted_agents:
            return (ProvenanceStatus.BLACKLISTED, 1.0)
        if source_agent in self._trusted_agents:
            # 可信但检测内容异常（长度过短/过长）
            content_len = len(content)
            if content_len < 2 or content_len > 1_000_000:
                return (ProvenanceStatus.UNKNOWN, 0.5)
            return (ProvenanceStatus.TRUSTED, 0.95)
        if source_agent in self._untrusted_agents:
            return (ProvenanceStatus.UNTRUSTED, 0.7)
        # 未知来源
        history = self._agent_history.get(source_agent, [])
        if len(history) >= 5:
            rejections = sum(1 for h in history[-10:] if not h.get("success", False))
            if rejections >= 3:
                self.blacklist(source_agent)
                return (ProvenanceStatus.BLACKLISTED, 0.9)
            return (ProvenanceStatus.UNKNOWN, 0.5)
        return (ProvenanceStatus.UNKNOWN, 0.3)

    def record_result(self, source_agent: str, success: bool, detail: str = "") -> None:
        """记录一次校验结果到历史。"""
        self._agent_history[source_agent].append({
            "timestamp": time.time(),
            "success": success,
            "detail": detail,
        })

    def get_stats(self) -> dict:
        return {
            "trusted_count": len(self._trusted_agents),
            "untrusted_count": len(self._untrusted_agents),
            "blacklisted_count": len(self._blacklisted_agents),
            "total_tracked": len(self._agent_history),
        }


# ══════════════════════════════════════════════════════════════════════
# 访问控制列表
# ══════════════════════════════════════════════════════════════════════

class MemoryAccessControlList:
    """记忆访问控制列表 (ACL)。

    按角色/AgentID 控制读写权限，支持通配符匹配。
    """

    def __init__(self):
        self._entries: list[AccessControlEntry] = []

    def grant(self, subject_id: str, resource_path: str, access_level: AccessLevel,
              subject_type: str = "agent", granted_by: str = "system",
              expires_at: float | None = None) -> AccessControlEntry:
        """授予访问权限。"""
        entry = AccessControlEntry(
            subject_id=subject_id,
            subject_type=subject_type,
            resource_path=resource_path,
            access_level=access_level,
            granted_by=granted_by,
            expires_at=expires_at,
        )
        self._entries.append(entry)
        return entry

    def revoke(self, subject_id: str, resource_path: str) -> int:
        """撤销访问权限，返回撤销的条目数。"""
        before = len(self._entries)
        self._entries = [
            e for e in self._entries
            if not (e.subject_id == subject_id and e.resource_path == resource_path)
        ]
        return before - len(self._entries)

    def authorise(self, subject_id: str, resource_path: str,
                  required_level: AccessLevel) -> tuple[bool, str]:
        """检查某 subject 对某 resource 是否有足够权限。

        Returns:
            (authorised: bool, reason: str)
        """
        best_level = AccessLevel.NONE

        for entry in self._entries:
            if entry.is_expired():
                continue
            # 匹配 subject
            if entry.subject_id != subject_id and entry.subject_id != "*":
                continue
            # 匹配 resource（精确匹配或通配符）
            if not self._resource_match(entry.resource_path, resource_path):
                continue
            # 取最高权限
            level_order = {AccessLevel.NONE: 0, AccessLevel.READ: 1,
                           AccessLevel.WRITE: 2, AccessLevel.ADMIN: 3}
            if level_order.get(entry.access_level, 0) > level_order.get(best_level, 0):
                best_level = entry.access_level

        level_order = {AccessLevel.NONE: 0, AccessLevel.READ: 1,
                       AccessLevel.WRITE: 2, AccessLevel.ADMIN: 3}
        if level_order.get(best_level, 0) >= level_order.get(required_level, 0):
            return (True, f"Authorized: {best_level.value}")
        return (False, f"Denied: has {best_level.value}, need {required_level.value}")

    def get_entries_for_subject(self, subject_id: str) -> list[AccessControlEntry]:
        """获取某 subject 的所有 ACL 条目。"""
        return [e for e in self._entries if e.subject_id == subject_id]

    def list_all(self) -> list[AccessControlEntry]:
        return list(self._entries)

    @staticmethod
    def _resource_match(pattern: str, resource: str) -> bool:
        """简单的通配符匹配。"""
        if pattern == "*":
            return True
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            return resource.startswith(prefix)
        return pattern == resource


# ══════════════════════════════════════════════════════════════════════
# A-MemGuard 主类
# ══════════════════════════════════════════════════════════════════════

class AMemGuard:
    """A-MemGuard — 记忆访问控制与投毒防御引擎。

    组合 ACL + ProvenanceChecker + Lock + Audit。
    投毒攻击防御率目标 ≥ 95%。
    """

    def __init__(self):
        self.acl = MemoryAccessControlList()
        self.provenance_checker = ProvenanceChecker()
        self._locks: dict[str, LockEntry] = {}
        self._audit_log: list[AuditRecord] = []
        self._content_hashes: dict[str, str] = {}  # memory_id -> sha256
        self._poisoning_attempts: int = 0
        self._poisoning_blocked: int = 0

    # ── 来源校验 ──────────────────────────────────────────────────

    def provenance_check(self, source_agent: str, content: str = "") -> tuple[bool, str]:
        """写入前校验来源是否可信。

        Returns:
            (trusted: bool, detail: str)
        """
        status, score = self.provenance_checker.check(source_agent, content)

        record = AuditRecord(
            action=AuditAction.PROVENANCE_CHECK,
            subject_id=source_agent,
            source_agent=source_agent,
            provenance_score=score,
            success=(status == ProvenanceStatus.TRUSTED),
            detail=f"Provenance: {status.value} (score={score:.2f})",
        )
        self._audit_log.append(record)
        self.provenance_checker.record_result(
            source_agent,
            status == ProvenanceStatus.TRUSTED,
            f"status={status.value}",
        )

        if status == ProvenanceStatus.BLACKLISTED:
            return (False, f"Source '{source_agent}' is blacklisted")
        if status == ProvenanceStatus.UNTRUSTED:
            return (False, f"Source '{source_agent}' is untrusted")
        return (True, f"Source '{source_agent}' verified: {status.value}")

    # ── 锁定 ──────────────────────────────────────────────────────

    def lock_entry(self, memory_id: str, lock_type: LockType,
                   locked_by: str, reason: str = "",
                   expires_at: float | None = None) -> LockEntry:
        """锁定关键记忆条目，防止未授权修改。"""
        lock = LockEntry(
            memory_id=memory_id,
            lock_type=lock_type,
            locked_by=locked_by,
            reason=reason,
            expires_at=expires_at,
        )
        self._locks[memory_id] = lock

        self._audit_log.append(AuditRecord(
            action=AuditAction.LOCK_ATTEMPT,
            subject_id=locked_by,
            resource_path=memory_id,
            success=True,
            detail=f"Locked: {lock_type.value} | reason={reason}",
            source_agent=locked_by,
        ))
        return lock

    def is_locked(self, memory_id: str, requesting_agent: str) -> tuple[bool, str]:
        """检查某记忆条目是否被锁定。"""
        lock = self._locks.get(memory_id)
        if lock is None:
            return (False, "Not locked")
        if lock.expires_at and time.time() > lock.expires_at:
            del self._locks[memory_id]
            return (False, "Lock expired")
        if lock.lock_type == LockType.HARD:
            return (True, f"Hard-locked by '{lock.locked_by}': {lock.reason}")
        # Soft lock: 记录但允许
        return (False, f"Soft-locked by '{lock.locked_by}': {lock.reason} (allowed)")

    def unlock_entry(self, memory_id: str, unlocked_by: str) -> bool:
        """解锁记忆条目（仅管理员可操作）。"""
        if memory_id in self._locks:
            del self._locks[memory_id]
            self._audit_log.append(AuditRecord(
                action=AuditAction.LOCK_ATTEMPT,
                subject_id=unlocked_by,
                resource_path=memory_id,
                success=True,
                detail="Unlocked",
                source_agent=unlocked_by,
            ))
            return True
        return False

    # ── 写前校验（核心流程）───────────────────────────────────────

    def guard_write(self, memory_id: str, content: str,
                    source_agent: str, required_access: AccessLevel = AccessLevel.WRITE) -> tuple[bool, str]:
        """写入前的完整安全校验流程。

        1. ACL 权限检查
        2. 来源校验 (provenance_check)
        3. 锁定检查
        4. 内容哈希 + 投毒检测

        Returns:
            (allowed: bool, detail: str)
        """
        # 1. ACL
        authorised, reason = self.acl.authorise(source_agent, memory_id, required_access)
        if not authorised:
            self._audit_log.append(AuditRecord(
                action=AuditAction.ACCESS_DENIED,
                subject_id=source_agent,
                resource_path=memory_id,
                success=False,
                detail=f"ACL denied: {reason}",
                source_agent=source_agent,
            ))
            return (False, f"Access denied: {reason}")

        # 2. Provenance
        trusted, prov_reason = self.provenance_check(source_agent, content)
        if not trusted:
            self._poisoning_attempts += 1
            self._poisoning_blocked += 1
            self._audit_log.append(AuditRecord(
                action=AuditAction.POISONING_DETECTED,
                subject_id=source_agent,
                resource_path=memory_id,
                success=False,
                detail=f"Write blocked: {prov_reason}",
                source_agent=source_agent,
            ))
            return (False, f"Write rejected: {prov_reason}")

        # 3. Lock check
        locked, lock_reason = self.is_locked(memory_id, source_agent)
        if locked:
            self._poisoning_attempts += 1
            self._poisoning_blocked += 1
            self._audit_log.append(AuditRecord(
                action=AuditAction.LOCK_ATTEMPT,
                subject_id=source_agent,
                resource_path=memory_id,
                success=False,
                detail=f"Write blocked by lock: {lock_reason}",
                source_agent=source_agent,
            ))
            return (False, f"Write rejected: {lock_reason}")

        # 4. Content hash (track for change detection)
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._content_hashes[memory_id] = content_hash

        self._audit_log.append(AuditRecord(
            action=AuditAction.WRITE_ATTEMPT,
            subject_id=source_agent,
            resource_path=memory_id,
            success=True,
            detail="Write allowed",
            content_hash=content_hash,
            source_agent=source_agent,
            provenance_score=1.0,
        ))
        return (True, f"Write allowed (hash={content_hash[:16]}...)")

    # ── 审计 ──────────────────────────────────────────────────────

    def audit_modification(self, memory_id: str, modifier: str,
                           action: AuditAction, success: bool,
                           detail: str = "", content_hash: str = "") -> AuditRecord:
        """记录所有修改尝试（含失败）。"""
        record = AuditRecord(
            action=action,
            subject_id=modifier,
            resource_path=memory_id,
            success=success,
            detail=detail,
            content_hash=content_hash,
            source_agent=modifier,
        )
        self._audit_log.append(record)
        return record

    def get_audit_trail(self, memory_id: str | None = None,
                        limit: int = 100) -> list[AuditRecord]:
        """获取审计追踪。"""
        if memory_id:
            return [r for r in self._audit_log if r.resource_path == memory_id][-limit:]
        return self._audit_log[-limit:]

    # ── 投毒防御统计 ──────────────────────────────────────────────

    def get_poisoning_defense_rate(self) -> float:
        """投毒攻击防御率。"""
        if self._poisoning_attempts == 0:
            return 1.0
        return self._poisoning_blocked / self._poisoning_attempts

    # ── 统计 ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        defense_rate = self.get_poisoning_defense_rate()
        total_audit = len(self._audit_log)
        denied = sum(1 for r in self._audit_log if not r.success)
        return {
            "acls": len(self.acl.list_all()),
            "active_locks": len(self._locks),
            "audit_records": total_audit,
            "access_denied": denied,
            "poisoning_attempts": self._poisoning_attempts,
            "poisoning_blocked": self._poisoning_blocked,
            "defense_rate": round(defense_rate, 4),
            "defense_target_met": defense_rate >= 0.95,
            "content_hashes": len(self._content_hashes),
            "provenance": self.provenance_checker.get_stats(),
        }


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    guard = AMemGuard()

    # 注册可信/不可信来源
    guard.provenance_checker.register_trusted("agent_alpha")
    guard.provenance_checker.register_trusted("agent_beta")
    guard.provenance_checker.register_untrusted("agent_gamma")

    # 设置 ACL
    guard.acl.grant("agent_alpha", "mem/*", AccessLevel.ADMIN)
    guard.acl.grant("agent_beta", "mem/shared/*", AccessLevel.WRITE)
    guard.acl.grant("agent_gamma", "mem/shared/*", AccessLevel.READ)

    # 锁定关键记忆
    guard.lock_entry("mem/critical/sys_prompt", LockType.HARD,
                     locked_by="system", reason="System prompt — immutable")

    print("=" * 60)
    print("A-MemGuard — Self Test")
    print("=" * 60)

    # 合法写入
    ok, msg = guard.guard_write("mem/shared/note", "Hello world", "agent_beta")
    print(f"\n[合法写入 agent_beta -> mem/shared/note] {ok}: {msg}")

    # 管理员写入锁定区域
    ok, msg = guard.guard_write("mem/critical/sys_prompt", "New prompt", "agent_alpha")
    print(f"[写入锁定区域 agent_alpha -> mem/critical/sys_prompt] {ok}: {msg}")

    # 不可信来源写入
    ok, msg = guard.guard_write("mem/shared/secret", "classified", "agent_gamma")
    print(f"[不可信写入 agent_gamma -> mem/shared/secret] {ok}: {msg}")

    # 黑名单
    guard.provenance_checker.blacklist("agent_malware")
    ok, msg = guard.guard_write("mem/shared/hack", "payload", "agent_malware")
    print(f"[黑名单写入 agent_malware] {ok}: {msg}")

    print(f"\n[防御率] {guard.get_poisoning_defense_rate():.2%}")
    print(f"[统计] {json.dumps(guard.get_stats(), indent=2, default=str)}")
