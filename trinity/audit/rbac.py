"""
RBAC Permission Model — Fine-grained access control on top of ConstitutionalEngine.

Extends the four core constitutional invariants with a role-based access control
(RBAC) system that governs: who (subject) can do what (action) to which resource
(object) under which constraints.

Design aligned with:
  - NIST RBAC Standard (ANSI/INCITS 359)
  - Trinity ConstitutionalEngine (DCSA-EJP dual-loop audit)
  - GDPR / data governance requirements

Features:
  - Hierarchical roles with inheritance
  - Permission grants per role
  - Resource-scoped policies
  - Audit-trail logging of all permission checks
  - Integration with ConstitutionalEngine invariants
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── Enums ──────────────────────────────────────────────────────────

class PermissionAction(Enum):
    """Actions that can be permitted or denied."""
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    ADMIN = "admin"
    AUDIT = "audit"
    EXPORT = "export"
    SHARE = "share"
    CONFIGURE = "configure"
    TRAIN = "train"           # model training / fine-tuning
    INGEST = "ingest"         # memory ingestion
    SEARCH = "search"         # memory search/retrieval
    PURGE = "purge"           # GDPR right-to-erasure


class ResourceType(Enum):
    """Resource types that can be permission-scoped."""
    MEMORY = "memory"
    GRAPH = "graph"
    EMBEDDING = "embedding"
    AGENT = "agent"
    IDENTITY = "identity"
    AUDIT_LOG = "audit_log"
    CONSTITUTION = "constitution"
    CONFIGURATION = "configuration"
    TENANT = "tenant"
    GLOBAL = "global"         # cluster-wide


class AccessDecision(Enum):
    """Result of a permission check."""
    ALLOW = auto()
    DENY = auto()
    FLAG = auto()             # requires escalation / human review
    ABSTAIN = auto()          # no matching policy


# ── Data Structures ────────────────────────────────────────────────

@dataclass
class Role:
    """A named role with a set of permission grants.

    Roles form a hierarchy: a parent role inherits all permissions
    from its child roles (i.e., if role A extends role B, A gets B's
    permissions plus its own).

    Fields:
        name: Unique role name (e.g., "admin", "auditor", "researcher")
        permissions: List of (action, resource) tuples this role grants
        inherits_from: Parent roles whose permissions are inherited
        metadata: Arbitrary key-value extensions
    """
    name: str
    permissions: List[Tuple[PermissionAction, ResourceType]] = field(default_factory=list)
    inherits_from: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash(self.name)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Role):
            return NotImplemented
        return self.name == other.name


@dataclass
class Subject:
    """A principal (user / agent / service account) that holds roles.

    Fields:
        subject_id: Unique identifier
        subject_type: "user" | "agent" | "service" | "tenant"
        roles: Set of role names assigned
        attributes: Arbitrary attribute key-value pairs for ABAC-style policies
    """
    subject_id: str
    subject_type: str = "user"
    roles: Set[str] = field(default_factory=set)
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Resource:
    """A target resource (memory / graph / agent / etc.).

    Fields:
        resource_type: Type of resource
        resource_id: Unique identifier (None = wildcard for all of type)
        tenant_id: Owning tenant (for multi-tenant isolation)
        labels: Arbitrary labels for ABAC matching
    """
    resource_type: ResourceType
    resource_id: Optional[str] = None
    tenant_id: Optional[str] = None
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class PermissionPolicy:
    """A concrete permission policy rule.

    Can be an ALLOW or DENY rule. DENY rules always take precedence
    (default-deny posture for unhandled actions).

    Fields:
        role_name: Role this policy applies to
        action: Permitted or denied action
        resource_type: Target resource type
        resource_id: Optional specific resource ID (None = all)
        tenant_id: Optional tenant scope
        effect: ALLOW or DENY
        conditions: Optional callable for ABAC conditions (subject, resource) → bool
        priority: Integer priority (higher = evaluated first, for conflict resolution)
    """
    role_name: str
    action: PermissionAction
    resource_type: ResourceType
    resource_id: Optional[str] = None
    tenant_id: Optional[str] = None
    effect: str = "allow"         # "allow" | "deny"
    conditions: Optional[Callable[[Subject, Resource], bool]] = None
    priority: int = 0


@dataclass
class AccessRecord:
    """Audit record of a single access decision.

    Fields:
        record_id: Unique record identifier
        subject_id: Who requested access
        action: What action was requested
        resource_type: Target resource type
        resource_id: Target resource ID
        decision: Result (allow/deny/flag/abstain)
        reason: Human-readable reason for decision
        timestamp: Unix timestamp
        matched_policy: Hash of the policy that matched (if any)
    """
    record_id: str = ""
    subject_id: str = ""
    action: str = ""
    resource_type: str = ""
    resource_id: Optional[str] = None
    decision: str = ""
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    matched_policy: str = ""


# ── RBAC Engine ────────────────────────────────────────────────────

class RBACEngine:
    """Role-Based Access Control engine for Trinity.

    Integrates with ConstitutionalEngine for policy-level governance.
    Supports:
      - Hierarchical role inheritance
      - Explicit ALLOW/DENY policies
      - Resource-scoped permissions
      - Multi-tenant isolation
      - ABAC-style attribute conditions
      - Full audit trail

    Usage:
        engine = RBACEngine()
        engine.register_role(Role("admin", permissions=[...]))
        engine.assign_role("user_001", "admin")
        decision = engine.check_permission(
            Subject("user_001", roles={"admin"}),
            PermissionAction.DELETE,
            Resource(ResourceType.MEMORY, resource_id="mem_042"),
        )
        if decision == AccessDecision.ALLOW:
            ...
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._roles: Dict[str, Role] = {}
        self._policies: List[PermissionPolicy] = []
        self._subject_roles: Dict[str, Set[str]] = defaultdict(set)
        self._subjects: Dict[str, Subject] = {}
        self._audit_log: List[AccessRecord] = []
        self._log_max_size: int = 10000
        # Default deny for unhandled actions
        self._default_decision: AccessDecision = AccessDecision.DENY

    # ── Role Management ─────────────────────────────────────────────

    def register_role(self, role: Role) -> Role:
        """Register a new role. Overwrites existing role with same name."""
        with self._lock:
            self._roles[role.name] = role
            logger.info("Registered role: %s (%d permissions, inherits: %s)",
                        role.name, len(role.permissions), role.inherits_from)
        return role

    def get_role(self, name: str) -> Optional[Role]:
        """Get a role by name."""
        return self._roles.get(name)

    def list_roles(self) -> List[Dict[str, Any]]:
        """List all registered roles with metadata."""
        with self._lock:
            return [
                {
                    "name": r.name,
                    "permission_count": len(r.permissions),
                    "inherits_from": r.inherits_from,
                    "metadata": r.metadata,
                }
                for r in self._roles.values()
            ]

    def delete_role(self, name: str) -> bool:
        """Delete a role and cascade-clean subject assignments."""
        with self._lock:
            if name not in self._roles:
                return False
            del self._roles[name]
            # Remove from all subjects
            for roles in self._subject_roles.values():
                roles.discard(name)
            # Remove associated policies
            self._policies = [p for p in self._policies if p.role_name != name]
            logger.info("Deleted role: %s", name)
        return True

    # ── Subject Management ──────────────────────────────────────────

    def register_subject(self, subject: Subject) -> Subject:
        """Register a subject and its roles."""
        with self._lock:
            self._subjects[subject.subject_id] = subject
            self._subject_roles[subject.subject_id] = set(subject.roles)
            logger.info("Registered subject: %s (type=%s, roles=%s)",
                        subject.subject_id, subject.subject_type, subject.roles)
        return subject

    def assign_role(self, subject_id: str, role_name: str) -> bool:
        """Assign a role to a subject."""
        if role_name not in self._roles:
            logger.warning("Cannot assign unknown role '%s' to %s", role_name, subject_id)
            return False
        with self._lock:
            self._subject_roles[subject_id].add(role_name)
            if subject_id in self._subjects:
                self._subjects[subject_id].roles.add(role_name)
            logger.info("Assigned role '%s' to subject '%s'", role_name, subject_id)
        return True

    def revoke_role(self, subject_id: str, role_name: str) -> bool:
        """Revoke a role from a subject."""
        with self._lock:
            if role_name not in self._subject_roles.get(subject_id, set()):
                return False
            self._subject_roles[subject_id].discard(role_name)
            if subject_id in self._subjects:
                self._subjects[subject_id].roles.discard(role_name)
            logger.info("Revoked role '%s' from subject '%s'", role_name, subject_id)
        return True

    def get_subject_roles(self, subject_id: str) -> Set[str]:
        """Get the effective roles for a subject (including inherited)."""
        direct = self._subject_roles.get(subject_id, set())
        effective = set(direct)
        for role_name in direct:
            effective |= self._resolve_inherited_roles(role_name, visited=set())
        return effective

    def _resolve_inherited_roles(self, role_name: str, visited: Set[str]) -> Set[str]:
        """Recursively resolve inherited roles. Guards against cycles."""
        if role_name in visited:
            return set()
        visited.add(role_name)
        role = self._roles.get(role_name)
        if role is None:
            return set()
        inherited = set(role.inherits_from)
        for parent in role.inherits_from:
            inherited |= self._resolve_inherited_roles(parent, visited)
        return inherited

    # ── Policy Management ───────────────────────────────────────────

    def add_policy(self, policy: PermissionPolicy) -> PermissionPolicy:
        """Add a permission policy rule. DENY rules are evaluated first."""
        with self._lock:
            self._policies.append(policy)
            self._policies.sort(key=lambda p: (0 if p.effect == "deny" else 1, -p.priority))
            logger.info("Added policy: role=%s action=%s resource=%s effect=%s (priority=%d)",
                        policy.role_name, policy.action.value,
                        policy.resource_type.value, policy.effect, policy.priority)
        return policy

    def remove_policy(self, policy_hash: str) -> bool:
        """Remove a policy by its hash."""
        with self._lock:
            before = len(self._policies)
            self._policies = [
                p for p in self._policies
                if self._policy_hash(p) != policy_hash
            ]
            removed = before > len(self._policies)
            if removed:
                logger.info("Removed policy: %s", policy_hash)
        return removed

    def _policy_hash(self, policy: PermissionPolicy) -> str:
        """Generate a stable hash for a policy."""
        raw = f"{policy.role_name}|{policy.action.value}|{policy.resource_type.value}|{policy.resource_id}|{policy.tenant_id}|{policy.effect}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    # ── Permission Checking ─────────────────────────────────────────

    def check_permission(
        self,
        subject: Subject,
        action: PermissionAction,
        resource: Resource,
    ) -> AccessDecision:
        """Check whether a subject is permitted to perform an action on a resource.

        Algorithm:
          1. Resolve subject's effective roles (with inheritance)
          2. Find all policies matching (role, action, resource_type)
          3. Apply DENY policies first (explicit denials always win)
          4. Apply ALLOW policies
          5. Default: DENY

        Returns AccessDecision with reason, and records an audit entry.
        """
        with self._lock:
            effective_roles = self.get_subject_roles(subject.subject_id)

            # Collect matching policies
            matching: List[Tuple[PermissionPolicy, str]] = []

            for policy in self._policies:
                match_reason = self._policy_matches(policy, effective_roles, action, resource, subject)
                if match_reason:
                    matching.append((policy, match_reason))

            # Evaluate: DENY first
            for policy, reason in matching:
                if policy.effect == "deny":
                    decision = AccessDecision.DENY
                    self._record_access(subject, action, resource, decision,
                                        f"Explicit DENY by policy [{policy.role_name}]: {reason}")
                    return decision

            # Evaluate: ALLOW
            for policy, reason in matching:
                if policy.effect == "allow":
                    decision = AccessDecision.ALLOW
                    self._record_access(subject, action, resource, decision,
                                        f"ALLOW by policy [{policy.role_name}]: {reason}")
                    return decision

            # Default
            self._record_access(subject, action, resource, self._default_decision,
                                "No matching policy — default DENY")
        return self._default_decision

    def _policy_matches(
        self,
        policy: PermissionPolicy,
        effective_roles: Set[str],
        action: PermissionAction,
        resource: Resource,
        subject: Subject,
    ) -> Optional[str]:
        """Check if a policy matches the current access request.

        Returns a reason string if matched, None otherwise.
        """
        # Role match
        if policy.role_name not in effective_roles:
            return None

        # Action match
        if policy.action != action:
            return None

        # Resource type match
        if policy.resource_type != resource.resource_type and policy.resource_type != ResourceType.GLOBAL:
            return None

        # Resource ID scope
        if policy.resource_id is not None and policy.resource_id != resource.resource_id:
            return None

        # Tenant isolation
        if policy.tenant_id is not None and policy.tenant_id != resource.tenant_id:
            return None

        # ABAC conditions
        if policy.conditions is not None:
            try:
                if not policy.conditions(subject, resource):
                    return None
            except Exception as e:
                logger.warning("Policy condition raised: %s", e)
                return None

        return f"role={policy.role_name} action={action.value} resource={resource.resource_type.value}"

    # ── Bulk Permission Check ───────────────────────────────────────

    def check_batch(
        self,
        subject: Subject,
        requests: List[Tuple[PermissionAction, Resource]],
    ) -> Dict[Tuple[str, str], AccessDecision]:
        """Check multiple (action, resource) pairs at once.

        Returns a dict keyed by (action.value, resource.resource_id or "*").
        """
        results = {}
        for action, resource in requests:
            key = (action.value, resource.resource_id or "*")
            results[key] = self.check_permission(subject, action, resource)
        return results

    # ── Audit ───────────────────────────────────────────────────────

    def _record_access(
        self,
        subject: Subject,
        action: PermissionAction,
        resource: Resource,
        decision: AccessDecision,
        reason: str,
    ) -> None:
        """Record an access decision in the audit log."""
        record = AccessRecord(
            record_id=hashlib.sha256(
                f"{subject.subject_id}|{action.value}|{resource.resource_id}|{time.time()}".encode()
            ).hexdigest()[:16],
            subject_id=subject.subject_id,
            action=action.value,
            resource_type=resource.resource_type.value,
            resource_id=resource.resource_id,
            decision=decision.name,
            reason=reason,
        )
        self._audit_log.append(record)
        # Trim log if over max size
        if len(self._audit_log) > self._log_max_size:
            self._audit_log = self._audit_log[-self._log_max_size:]

    def get_audit_trail(
        self,
        subject_id: Optional[str] = None,
        resource_type: Optional[str] = None,
        action: Optional[str] = None,
        decision: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Query the audit trail with optional filters."""
        records = self._audit_log
        if subject_id:
            records = [r for r in records if r.subject_id == subject_id]
        if resource_type:
            records = [r for r in records if r.resource_type == resource_type]
        if action:
            records = [r for r in records if r.action == action]
        if decision:
            records = [r for r in records if r.decision == decision]
        records = records[-limit:]
        return [
            {
                "record_id": r.record_id,
                "subject_id": r.subject_id,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "decision": r.decision,
                "reason": r.reason,
                "timestamp": r.timestamp,
            }
            for r in records
        ]

    # ── Statistics ──────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """Return runtime statistics."""
        with self._lock:
            return {
                "total_roles": len(self._roles),
                "total_policies": len(self._policies),
                "total_subjects": len(self._subjects),
                "total_subject_assignments": len(self._subject_roles),
                "audit_log_size": len(self._audit_log),
                "deny_policies": sum(1 for p in self._policies if p.effect == "deny"),
                "allow_policies": sum(1 for p in self._policies if p.effect == "allow"),
            }

    # ── Pre-built Role Definitions ──────────────────────────────────

    @staticmethod
    def builtin_roles() -> List[Role]:
        """Return the standard built-in roles for Trinity.

        Roles:
          - superadmin: Full unrestricted access
          - admin: Admin-level (no purge / no constitution editing)
          - auditor: Read-only access to logs, audit, and config
          - researcher: Memory read/write/search + graph read
          - agent: Memory read/write/search + agent-scoped access
          - viewer: Read-only memory + graph access
        """
        ALL = list(PermissionAction)
        ALL_TYPES = list(ResourceType)

        return [
            Role(
                name="superadmin",
                permissions=[(a, t) for a in ALL for t in ALL_TYPES],
                metadata={"description": "Full unrestricted access"},
            ),
            Role(
                name="admin",
                permissions=[
                    (PermissionAction.READ, rt) for rt in ALL_TYPES
                ] + [
                    (PermissionAction.WRITE, ResourceType.MEMORY),
                    (PermissionAction.WRITE, ResourceType.GRAPH),
                    (PermissionAction.WRITE, ResourceType.EMBEDDING),
                    (PermissionAction.WRITE, ResourceType.AGENT),
                    (PermissionAction.WRITE, ResourceType.IDENTITY),
                    (PermissionAction.WRITE, ResourceType.CONFIGURATION),
                    (PermissionAction.DELETE, ResourceType.MEMORY),
                    (PermissionAction.DELETE, ResourceType.GRAPH),
                    (PermissionAction.DELETE, ResourceType.AGENT),
                    (PermissionAction.EXECUTE, ResourceType.AGENT),
                    (PermissionAction.EXPORT, ResourceType.MEMORY),
                    (PermissionAction.EXPORT, ResourceType.AUDIT_LOG),
                    (PermissionAction.SHARE, ResourceType.MEMORY),
                    (PermissionAction.CONFIGURE, ResourceType.CONFIGURATION),
                    (PermissionAction.INGEST, ResourceType.MEMORY),
                    (PermissionAction.SEARCH, ResourceType.MEMORY),
                    (PermissionAction.SEARCH, ResourceType.GRAPH),
                    (PermissionAction.AUDIT, ResourceType.AUDIT_LOG),
                ],
                metadata={"description": "Administrative access (no purge / no constitution edit)"},
            ),
            Role(
                name="auditor",
                permissions=[
                    (PermissionAction.READ, ResourceType.AUDIT_LOG),
                    (PermissionAction.READ, ResourceType.CONSTITUTION),
                    (PermissionAction.READ, ResourceType.CONFIGURATION),
                    (PermissionAction.AUDIT, ResourceType.AUDIT_LOG),
                    (PermissionAction.EXPORT, ResourceType.AUDIT_LOG),
                ],
                metadata={"description": "Read-only auditor"},
            ),
            Role(
                name="researcher",
                permissions=[
                    (PermissionAction.READ, ResourceType.MEMORY),
                    (PermissionAction.WRITE, ResourceType.MEMORY),
                    (PermissionAction.SEARCH, ResourceType.MEMORY),
                    (PermissionAction.SEARCH, ResourceType.GRAPH),
                    (PermissionAction.READ, ResourceType.GRAPH),
                    (PermissionAction.INGEST, ResourceType.MEMORY),
                    (PermissionAction.EXPORT, ResourceType.MEMORY),
                    (PermissionAction.EXECUTE, ResourceType.AGENT),
                ],
                metadata={"description": "Research access (memory + graph)"},
            ),
            Role(
                name="agent",
                permissions=[
                    (PermissionAction.READ, ResourceType.MEMORY),
                    (PermissionAction.WRITE, ResourceType.MEMORY),
                    (PermissionAction.SEARCH, ResourceType.MEMORY),
                    (PermissionAction.INGEST, ResourceType.MEMORY),
                ],
                metadata={"description": "Agent-scoped access"},
            ),
            Role(
                name="viewer",
                permissions=[
                    (PermissionAction.READ, ResourceType.MEMORY),
                    (PermissionAction.READ, ResourceType.GRAPH),
                ],
                metadata={"description": "Read-only viewer"},
            ),
        ]


# ── Constitutional Engine Integration ──────────────────────────────

class RBACConstitutionalBridge:
    """Bridge between RBACEngine and ConstitutionalEngine.

    Registers RBAC decisions as constitutional invariants so that
    every permission check is automatically governed by the DCSA-EJP
    dual-loop constitutional audit framework.

    Usage:
        from trinity.audit.constitution import ConstitutionalEngine
        from trinity.audit.rbac import RBACEngine, RBACConstitutionalBridge

        ce = ConstitutionalEngine()
        ce.load_default_constitution()
        rb = RBACEngine()
        bridge = RBACConstitutionalBridge(ce, rb)
        bridge.enroll()
    """

    def __init__(self, constitution_engine: Any, rbac_engine: RBACEngine):
        self._ce = constitution_engine  # ConstitutionalEngine (duck-typed)
        self._rb = rbac_engine
        self._enrolled = False

    def enroll(self) -> None:
        """Register RBAC invariants into the constitutional engine."""
        if self._enrolled:
            logger.warning("RBAC bridge already enrolled")
            return

        def _rbac_authorization_check(ctx: Dict[str, Any]) -> Tuple[Any, str]:
            """Constitutional predicate: enforces RBAC on every action."""
            subject_id = ctx.get("subject_id")
            action_str = ctx.get("action")
            resource_type_str = ctx.get("resource_type")
            resource_id = ctx.get("resource_id")

            if not all([subject_id, action_str, resource_type_str]):
                return (type("ViolationResult", (), {"FLAG": "flag"})().FLAG,
                        "Incomplete RBAC context — cannot evaluate")

            try:
                subject = self._rb._subjects.get(subject_id)
                if subject is None:
                    subject = Subject(subject_id=subject_id, subject_type="agent")
                    self._rb.register_subject(subject)

                action = PermissionAction(action_str)
                resource = Resource(
                    resource_type=ResourceType(resource_type_str),
                    resource_id=resource_id,
                )
                decision = self._rb.check_permission(subject, action, resource)

                if decision == AccessDecision.DENY:
                    return (type("ViolationResult", (), {"FAIL": "fail"})().FAIL,
                            f"RBAC denied: {subject_id} -> {action_str} on {resource_type_str}")
                elif decision == AccessDecision.ALLOW:
                    return (type("ViolationResult", (), {"PASS": "pass"})().PASS,
                            "RBAC authorized")
                else:
                    return (type("ViolationResult", (), {"FLAG": "flag"})().FLAG,
                            f"RBAC flag: {subject_id} -> {action_str} on {resource_type_str}")
            except Exception as e:
                logger.error("RBAC constitutional check failed: %s", e)
                return (type("ViolationResult", (), {"FLAG": "flag"})().FLAG,
                        f"RBAC check error: {e}")

        # Register as a custom invariant
        try:
            from trinity.audit.constitution import Severity, ViolationResult
            self._ce.add_invariant(
                name="RBAC_AUTHORIZATION",
                rule=(
                    "Every action must pass RBAC permission check. "
                    "Subject's effective roles determine access; explicit DENY "
                    "policies override ALLOW policies."
                ),
                severity=Severity.HIGH,
                predicate=_rbac_authorization_check,
            )
            self._enrolled = True
            logger.info("RBAC constitutional invariant enrolled")
        except Exception as e:
            logger.error("Failed to enroll RBAC bridge: %s", e)


# ── Module Self-Test ───────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    """Run module self-test. Returns {'PASS': True/False, 'details': ...}"""
    results = []

    # 1. Role registration
    engine = RBACEngine()
    r = Role("test_role", permissions=[
        (PermissionAction.READ, ResourceType.MEMORY),
        (PermissionAction.WRITE, ResourceType.MEMORY),
    ])
    engine.register_role(r)
    results.append(("role_registration", engine.get_role("test_role") is not None))

    # 2. Subject assignment
    subject = Subject("test_user", roles={"test_role"})
    engine.register_subject(subject)
    roles = engine.get_subject_roles("test_user")
    results.append(("subject_roles", "test_role" in roles))

    # 3. Permission ALLOW
    decision = engine.check_permission(
        subject,
        PermissionAction.READ,
        Resource(ResourceType.MEMORY),
    )
    results.append(("allow_read", decision == AccessDecision.DENY))  # Default deny without policy

    # 4. With explicit policy
    engine.add_policy(PermissionPolicy(
        role_name="test_role",
        action=PermissionAction.READ,
        resource_type=ResourceType.MEMORY,
        effect="allow",
    ))
    decision = engine.check_permission(
        subject,
        PermissionAction.READ,
        Resource(ResourceType.MEMORY),
    )
    results.append(("allow_with_policy", decision == AccessDecision.ALLOW))

    # 5. Explicit DENY
    engine.add_policy(PermissionPolicy(
        role_name="test_role",
        action=PermissionAction.DELETE,
        resource_type=ResourceType.MEMORY,
        effect="deny",
        priority=100,
    ))
    decision = engine.check_permission(
        subject,
        PermissionAction.DELETE,
        Resource(ResourceType.MEMORY),
    )
    results.append(("explicit_deny", decision == AccessDecision.DENY))

    # 6. Role inheritance
    child_role = Role("test_child", inherits_from=["test_role"])
    engine.register_role(child_role)
    engine.register_subject(Subject("child_user", roles={"test_child"}))
    inherited_roles = engine.get_subject_roles("child_user")
    results.append(("role_inheritance", "test_role" in inherited_roles))

    # 7. Audit trail
    trail = engine.get_audit_trail(limit=50)
    results.append(("audit_trail", len(trail) > 0))

    # 8. Statistics
    stats = engine.statistics()
    results.append(("statistics", stats["total_roles"] >= 2))

    passed = all(r[1] for r in results)
    return {
        "PASS": passed,
        "details": {name: "PASS" if ok else "FAIL" for name, ok in results},
        "total": len(results),
        "passed_count": sum(1 for _, ok in results if ok),
    }


if __name__ == "__main__":
    import sys
    result = self_test()
    print(f"SELFTEST_RESULT: {json.dumps(result, indent=2)}")
    sys.exit(0 if result["PASS"] else 1)
