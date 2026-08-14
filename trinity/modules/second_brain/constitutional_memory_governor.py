"""
ConstitutionalMemoryGovernor — Constitutional Memory Governance Engine
=======================================================================
v2.1, Jul 2026 · P41-4

实现 Constitutional Memory 治理引擎: policy_enforcement 策略即代码引擎,
credential_tiering 分层凭证管理 (session/task/agent/system级),
lifecycle_control 记忆创建→活跃→归档→删除完整生命周期,
full_observability 每步记忆操作可审计可追溯。

设计要点:
  - MemoryPolicy: 策略即代码, 声明式规则
  - CredentialTiering: 四级凭证, 最小权限
  - LifecycleControl: 状态机驱动的生命周期
  - FullObservability: 每步操作记录到 AuditLog
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class CredentialLevel(Enum):
    """凭证等级——分层访问控制。"""
    SESSION = 1      # 会话级: 单次对话
    TASK = 2          # 任务级: 跨多次对话
    AGENT = 3         # Agent级: 跨任务
    SYSTEM = 4        # 系统级: 全局


class MemoryLifecycle(Enum):
    """记忆生命周期状态。"""
    CREATED = auto()
    ACTIVE = auto()
    DORMANT = auto()
    ARCHIVED = auto()
    DELETED = auto()


class PolicyAction(Enum):
    """策略动作。"""
    ALLOW = auto()
    DENY = auto()
    QUARANTINE = auto()
    LOG_ONLY = auto()
    ESCALATE = auto()


class AuditEventType(Enum):
    """审计事件类型。"""
    MEMORY_CREATE = auto()
    MEMORY_READ = auto()
    MEMORY_UPDATE = auto()
    MEMORY_DELETE = auto()
    MEMORY_ARCHIVE = auto()
    POLICY_CHECK = auto()
    CREDENTIAL_CHECK = auto()
    LIFECYCLE_TRANSITION = auto()
    ACCESS_DENIED = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryPolicy:
    """一条记忆策略——策略即代码的声明式规则。"""
    policy_id: str
    name: str
    description: str
    condition: str           # 触发条件 (可评估表达式或自然语言)
    action: PolicyAction
    priority: int = 0        # 优先级 (越高越先评估)
    enabled: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class Credential:
    """分层凭证。"""
    credential_id: str
    level: CredentialLevel
    owner: str               # 凭证持有者
    permissions: List[str] = field(default_factory=list)
    scope: str = ""          # 作用域 (如 task_id, session_id)
    expires_at: Optional[float] = None
    created_at: float = field(default_factory=time.time)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


@dataclass
class MemoryObject:
    """被治理的记忆对象。"""
    memory_id: str
    content: Dict[str, Any] = field(default_factory=dict)
    lifecycle: MemoryLifecycle = MemoryLifecycle.CREATED
    credential_level: CredentialLevel = CredentialLevel.SESSION
    owner: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    transition_history: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class AuditLog:
    """审计日志条目——每步操作可追踪。"""
    audit_id: str
    event_type: AuditEventType
    memory_id: str
    credential_id: str
    detail: str
    timestamp: float = field(default_factory=time.time)
    approved: bool = True


# ---------------------------------------------------------------------------
# PolicyEnforcement
# ---------------------------------------------------------------------------

class PolicyEnforcement:
    """策略执行引擎——策略即代码。

    Parameters
    ----------
    policies : Optional[List[MemoryPolicy]]
        初始策略列表。
    """

    def __init__(self, policies: Optional[List[MemoryPolicy]] = None) -> None:
        self._policies: Dict[str, MemoryPolicy] = {}
        self._lock = threading.RLock()
        if policies:
            for p in policies:
                self._policies[p.policy_id] = p

    def add_policy(self, policy: MemoryPolicy) -> None:
        with self._lock:
            self._policies[policy.policy_id] = policy

    def remove_policy(self, policy_id: str) -> bool:
        with self._lock:
            return self._policies.pop(policy_id, None) is not None

    def evaluate(
        self, memory: MemoryObject, credential: Credential, operation: str
    ) -> Tuple[PolicyAction, List[str]]:
        """评估所有适用策略——返回最高优先级的动作 + 匹配策略列表。

        Parameters
        ----------
        memory : MemoryObject
            被操作记忆。
        credential : Credential
            操作凭证。
        operation : str
            操作类型 (create/read/update/delete)。

        Returns
        -------
        Tuple[PolicyAction, List[str]]
            (最终动作, 匹配的策略 ID)。
        """
        matched: List[Tuple[MemoryPolicy, int]] = []

        for policy in sorted(self._policies.values(), key=lambda p: p.priority, reverse=True):
            if not policy.enabled:
                continue
            if _evaluate_condition(policy.condition, memory, credential, operation):
                matched.append((policy, policy.priority))

        if not matched:
            return PolicyAction.ALLOW, []

        # 返回最高优先级动作
        matched.sort(key=lambda x: x[1], reverse=True)
        best_action = matched[0][0].action

        # DENY 最高优先级时可能被其他策略 ALLOW 覆盖? 不, DENY 优先
        deny_policies = [p for p, _ in matched if p.action == PolicyAction.DENY]
        if deny_policies:
            return PolicyAction.DENY, [p.policy_id for p in deny_policies]

        return best_action, [p.policy_id for p, _ in matched]

    def statistics(self) -> Dict[str, Any]:
        return {"total_policies": len(self._policies),
                "enabled": sum(1 for p in self._policies.values() if p.enabled)}


# ---------------------------------------------------------------------------
# CredentialTiering
# ---------------------------------------------------------------------------

class CredentialTiering:
    """分层凭证管理——session/task/agent/system 四级。

    Parameters
    ----------
    default_expiry : Dict[CredentialLevel, Optional[float]]
        每级默认过期时间 (秒); None 表示永不过期。
    """

    DEFAULT_EXPIRY = {
        CredentialLevel.SESSION: 3600.0,       # 1 小时
        CredentialLevel.TASK: 86400.0,          # 24 小时
        CredentialLevel.AGENT: None,            # 永不过期
        CredentialLevel.SYSTEM: None,           # 永不过期
    }

    def __init__(
        self,
        default_expiry: Optional[Dict[CredentialLevel, Optional[float]]] = None,
    ) -> None:
        self.default_expiry = default_expiry or self.DEFAULT_EXPIRY
        self._credentials: Dict[str, Credential] = {}
        self._lock = threading.RLock()
        self._cred_count: int = 0

    def issue_credential(
        self,
        owner: str,
        level: CredentialLevel,
        permissions: Optional[List[str]] = None,
        scope: str = "",
        custom_expiry: Optional[float] = None,
    ) -> Credential:
        """颁发凭证。

        Parameters
        ----------
        owner : str
            持有者标识。
        level : CredentialLevel
            凭证等级。
        permissions : Optional[List[str]]
            权限列表。
        scope : str
            作用域。
        custom_expiry : Optional[float]
            自定义过期时间 (秒)。

        Returns
        -------
        Credential
        """
        with self._lock:
            self._cred_count += 1
            expires_at = custom_expiry if custom_expiry is not None else self.default_expiry.get(level)
            if expires_at is not None:
                expires_at = time.time() + expires_at

            cred = Credential(
                credential_id=f"cred_{self._cred_count}_{int(time.time()*1e6)}",
                level=level,
                owner=owner,
                permissions=permissions or ["read"],
                scope=scope,
                expires_at=expires_at,
            )
            self._credentials[cred.credential_id] = cred
            logger.info("Credential issued: %s level=%s owner=%s", cred.credential_id, level.name, owner)
            return cred

    def validate_credential(self, credential_id: str, required_level: CredentialLevel) -> Tuple[bool, str]:
        """验证凭证——检查等级与过期。

        Returns
        -------
        Tuple[bool, str]
            (是否有效, 原因)。
        """
        with self._lock:
            cred = self._credentials.get(credential_id)
            if cred is None:
                return False, "Credential not found"

            if cred.is_expired():
                return False, "Credential expired"

            if cred.level.value < required_level.value:
                return False, f"Insufficient level: {cred.level.name} < {required_level.name}"

            return True, "OK"

    def revoke_credential(self, credential_id: str) -> bool:
        """吊销凭证。"""
        with self._lock:
            return self._credentials.pop(credential_id, None) is not None

    def statistics(self) -> Dict[str, Any]:
        return {"active_credentials": len(self._credentials)}


# ---------------------------------------------------------------------------
# LifecycleControl
# ---------------------------------------------------------------------------

class LifecycleControl:
    """记忆生命周期控制——创建→活跃→归档→删除。

    Parameters
    ----------
    dormant_threshold : float
        未访问超过此时长进入 DORMANT (秒)。
    archive_threshold : float
        DORMANT 超过此时长进入 ARCHIVED (秒)。
    delete_threshold : float
        ARCHIVED 超过此时长标记清理 (秒)。
    """

    def __init__(
        self,
        dormant_threshold: float = 3600.0,
        archive_threshold: float = 86400.0,
        delete_threshold: float = 604800.0,
    ) -> None:
        self.dormant_threshold = dormant_threshold
        self.archive_threshold = archive_threshold
        self.delete_threshold = delete_threshold

        self._transitions: Dict[MemoryLifecycle, Dict[MemoryLifecycle, bool]] = {
            MemoryLifecycle.CREATED:  {MemoryLifecycle.ACTIVE: True, MemoryLifecycle.DELETED: True},
            MemoryLifecycle.ACTIVE:   {MemoryLifecycle.DORMANT: True, MemoryLifecycle.ARCHIVED: True, MemoryLifecycle.DELETED: True},
            MemoryLifecycle.DORMANT:  {MemoryLifecycle.ACTIVE: True, MemoryLifecycle.ARCHIVED: True, MemoryLifecycle.DELETED: True},
            MemoryLifecycle.ARCHIVED: {MemoryLifecycle.ACTIVE: True, MemoryLifecycle.DELETED: True},
            MemoryLifecycle.DELETED:  {},
        }

    def can_transition(self, from_state: MemoryLifecycle, to_state: MemoryLifecycle) -> bool:
        """检查状态转换是否合法。"""
        return self._transitions.get(from_state, {}).get(to_state, False)

    def transition(self, memory: MemoryObject, to_state: MemoryLifecycle) -> Tuple[bool, str]:
        """执行生命周期状态转换。"""
        if not self.can_transition(memory.lifecycle, to_state):
            return False, f"Invalid transition: {memory.lifecycle.name} → {to_state.name}"

        old_state = memory.lifecycle
        memory.lifecycle = to_state
        memory.transition_history.append({
            "from": old_state.name,
            "to": to_state.name,
            "timestamp": time.time(),
        })
        logger.debug("Lifecycle: %s %s → %s", memory.memory_id, old_state.name, to_state.name)
        return True, "OK"

    def check_auto_transition(self, memory: MemoryObject) -> Optional[MemoryLifecycle]:
        """检查自动状态转移——基于时间阈值。"""
        now = time.time()
        idle = now - memory.last_accessed

        if memory.lifecycle == MemoryLifecycle.ACTIVE and idle > self.dormant_threshold:
            return MemoryLifecycle.DORMANT

        if memory.lifecycle == MemoryLifecycle.DORMANT and idle > self.archive_threshold:
            return MemoryLifecycle.ARCHIVED

        if memory.lifecycle == MemoryLifecycle.ARCHIVED and idle > self.delete_threshold:
            return MemoryLifecycle.DELETED

        return None


# ---------------------------------------------------------------------------
# FullObservability
# ---------------------------------------------------------------------------

class FullObservability:
    """审计日志——每步记忆操作可追溯。

    Parameters
    ----------
    capacity : int
        最大审计日志条目数。
    """

    def __init__(self, capacity: int = 1000) -> None:
        self.capacity = capacity
        self._logs: deque = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._audit_count: int = 0

    def log(
        self,
        event_type: AuditEventType,
        memory_id: str,
        credential_id: str,
        detail: str,
        approved: bool = True,
    ) -> AuditLog:
        """记录一条审计日志。"""
        with self._lock:
            self._audit_count += 1
            entry = AuditLog(
                audit_id=f"audit_{self._audit_count}_{int(time.time()*1e6)}",
                event_type=event_type,
                memory_id=memory_id,
                credential_id=credential_id,
                detail=detail,
                approved=approved,
            )
            self._logs.append(entry)
            return entry

    def query(
        self,
        memory_id: Optional[str] = None,
        event_type: Optional[AuditEventType] = None,
        limit: int = 50,
    ) -> List[AuditLog]:
        """查询审计日志。"""
        results = list(self._logs)
        if memory_id:
            results = [l for l in results if l.memory_id == memory_id]
        if event_type:
            results = [l for l in results if l.event_type == event_type]
        return results[-limit:]

    def statistics(self) -> Dict[str, Any]:
        return {"total_audit_entries": len(self._logs)}


# ---------------------------------------------------------------------------
# ConstitutionalMemoryGovernor
# ---------------------------------------------------------------------------

class ConstitutionalMemoryGovernor:
    """Constitutional Memory 治理引擎 (v2.1)。

    Parameters
    ----------
    dormant_threshold : float
        Dormant 阈值 (秒)。
    archive_threshold : float
        Archive 阈值 (秒)。
    audit_capacity : int
        审计日志容量。
    """

    def __init__(
        self,
        dormant_threshold: float = 3600.0,
        archive_threshold: float = 86400.0,
        audit_capacity: int = 1000,
    ) -> None:
        self.policy_enforcement = PolicyEnforcement()
        self.credential_tiering = CredentialTiering()
        self.lifecycle_control = LifecycleControl(
            dormant_threshold=dormant_threshold,
            archive_threshold=archive_threshold,
        )
        self.full_observability = FullObservability(capacity=audit_capacity)
        self._memories: Dict[str, MemoryObject] = {}
        self._lock = threading.RLock()
        self._mem_count: int = 0

        logger.info(
            "ConstitutionalMemoryGovernor initialized [dormant=%.0fh archive=%.0fh audit=%d]",
            dormant_threshold / 3600, archive_threshold / 3600, audit_capacity,
        )

    # ------------------------------------------------------------------
    # Memory Operations with Governance
    # ------------------------------------------------------------------

    def create_memory(
        self,
        content: Dict[str, Any],
        credential_id: str,
        owner: str = "",
        tags: Optional[List[str]] = None,
    ) -> Tuple[Optional[MemoryObject], str]:
        """创建记忆——带策略和凭证检查。"""
        with self._lock:
            # 1. 凭证验证
            valid, reason = self.credential_tiering.validate_credential(credential_id, CredentialLevel.SESSION)
            if not valid:
                self.full_observability.log(
                    AuditEventType.ACCESS_DENIED, "", credential_id,
                    f"Memory create denied: {reason}", approved=False,
                )
                return None, reason

            # 2. 创建记忆对象
            self._mem_count += 1
            mem = MemoryObject(
                memory_id=f"mem_{self._mem_count}_{int(time.time()*1e6)}",
                content=content,
                lifecycle=MemoryLifecycle.CREATED,
                credential_level=CredentialLevel.SESSION,
                owner=owner,
                tags=tags or [],
            )

            # 3. 策略检查
            cred = self.credential_tiering._credentials.get(credential_id)
            action, matched = self.policy_enforcement.evaluate(mem, cred or Credential("", CredentialLevel.SESSION, ""), "create")
            if action == PolicyAction.DENY:
                self.full_observability.log(
                    AuditEventType.ACCESS_DENIED, mem.memory_id, credential_id,
                    f"Create denied by policies: {matched}", approved=False,
                )
                return None, f"Denied by policies: {matched}"

            # 4. 激活
            self.lifecycle_control.transition(mem, MemoryLifecycle.ACTIVE)
            self._memories[mem.memory_id] = mem

            self.full_observability.log(
                AuditEventType.MEMORY_CREATE, mem.memory_id, credential_id,
                f"Memory created with {len(matched)} policy checks",
            )

            return mem, "OK"

    def access_memory(self, memory_id: str, credential_id: str) -> Tuple[Optional[MemoryObject], str]:
        """访问记忆——带凭证与生命周期检查。"""
        with self._lock:
            mem = self._memories.get(memory_id)
            if not mem:
                return None, "Memory not found"

            valid, reason = self.credential_tiering.validate_credential(credential_id, mem.credential_level)
            if not valid:
                self.full_observability.log(
                    AuditEventType.ACCESS_DENIED, memory_id, credential_id,
                    f"Access denied: {reason}", approved=False,
                )
                return None, reason

            # 自动生命周期检查
            auto_state = self.lifecycle_control.check_auto_transition(mem)
            if auto_state:
                ok, _ = self.lifecycle_control.transition(mem, auto_state)

            mem.last_accessed = time.time()

            self.full_observability.log(
                AuditEventType.MEMORY_READ, memory_id, credential_id, "Access granted",
            )
            return mem, "OK"

    def archive_memory(self, memory_id: str, credential_id: str) -> Tuple[bool, str]:
        """归档记忆。"""
        with self._lock:
            mem = self._memories.get(memory_id)
            if not mem:
                return False, "Memory not found"

            valid, reason = self.credential_tiering.validate_credential(credential_id, CredentialLevel.TASK)
            if not valid:
                return False, reason

            ok, msg = self.lifecycle_control.transition(mem, MemoryLifecycle.ARCHIVED)
            if ok:
                self.full_observability.log(
                    AuditEventType.MEMORY_ARCHIVE, memory_id, credential_id, "Archived",
                )
            return ok, msg

    def delete_memory(self, memory_id: str, credential_id: str) -> Tuple[bool, str]:
        """删除记忆。"""
        with self._lock:
            mem = self._memories.get(memory_id)
            if not mem:
                return False, "Memory not found"

            valid, reason = self.credential_tiering.validate_credential(credential_id, CredentialLevel.SYSTEM)
            if not valid:
                self.full_observability.log(
                    AuditEventType.ACCESS_DENIED, memory_id, credential_id,
                    f"Delete denied: {reason}", approved=False,
                )
                return False, reason

            self.lifecycle_control.transition(mem, MemoryLifecycle.DELETED)
            del self._memories[memory_id]

            self.full_observability.log(
                AuditEventType.MEMORY_DELETE, memory_id, credential_id, "Deleted",
            )
            return True, "OK"

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def audit_trail(self, memory_id: Optional[str] = None, limit: int = 50) -> List[AuditLog]:
        """获取审计追踪。"""
        return self.full_observability.query(memory_id=memory_id, limit=limit)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            lc_dist = {s.name: 0 for s in MemoryLifecycle}
            for m in self._memories.values():
                lc_dist[m.lifecycle.name] = lc_dist.get(m.lifecycle.name, 0) + 1
            return {
                "active_memories": len(self._memories),
                "lifecycle_distribution": lc_dist,
                "policies": self.policy_enforcement.statistics()["total_policies"],
                "credentials": self.credential_tiering.statistics()["active_credentials"],
                "audit_entries": self.full_observability.statistics()["total_audit_entries"],
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _evaluate_condition(
    condition: str,
    memory: MemoryObject,
    credential: Credential,
    operation: str,
) -> bool:
    """评估策略条件。

    支持简单模式: "operation==delete", "tags contains sensitive", "*" (匹配所有)
    """
    if condition == "*":
        return True

    if condition == f"operation=={operation}":
        return True

    if condition.startswith("tags contains "):
        target = condition[len("tags contains "):]
        return target in memory.tags

    if condition.startswith("level<="):
        target = condition[len("level<="):]
        target_level = getattr(CredentialLevel, target.strip(), None)
        if target_level:
            return credential.level.value <= target_level.value

    return False
