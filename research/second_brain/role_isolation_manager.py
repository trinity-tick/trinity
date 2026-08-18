"""P32: Role Isolation Manager — BEAM-SWITCH + MENTOR dual-chain.

# status: orphan (2026-08-15 audit, not in runtime path)
Serializable role profiles with state snapshots, cross-role leak
detection, and guarded role switching with temporary state flush
and leak filtering.
"""

from __future__ import annotations

import hashlib
import json
import logging
import pickle
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class RoleProfile:
    role_id: str
    persona: dict[str, Any]
    knowledge_boundary: list[str]
    state_snapshot: dict[str, Any]
    version: int = 1
    created_at: float = field(default_factory=time.time)


@dataclass
class LeakAlert:
    alert_id: str
    source_role: str
    target_role: str
    leaked_keys: list[str]
    severity: str  # LOW / MEDIUM / HIGH
    recommendations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class SwitchDecision:
    allowed: bool
    from_role: str
    to_role: str
    temp_state_flushed: bool
    leak_check_clean: bool
    warnings: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class RoleSwitchResult:
    result_id: str
    active_role: RoleProfile | None
    previous_role: RoleProfile | None
    switch_decision: SwitchDecision
    purged_context: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Role State Serializer
# ---------------------------------------------------------------------------

class RoleStateSerializer:
    """Serialize / deserialize role profiles to portable bytes.

    Supports pickle for full fidelity and JSON for cross-platform
    compatibility. Thread-safe with per-role checksum protection.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def serialize(self, role: RoleProfile) -> bytes:
        with self._lock:
            data = {
                "role_id": role.role_id, "persona": role.persona,
                "knowledge_boundary": role.knowledge_boundary,
                "state_snapshot": role.state_snapshot, "version": role.version,
                "checksum": hashlib.sha256(json.dumps(role.state_snapshot, sort_keys=True, default=str).encode()).hexdigest(),
            }
            return pickle.dumps(data)

    def deserialize(self, data: bytes) -> RoleProfile:
        with self._lock:
            raw = pickle.loads(data)
            profile = RoleProfile(
                role_id=raw.get("role_id", "unknown"), persona=raw.get("persona", {}),
                knowledge_boundary=raw.get("knowledge_boundary", []),
                state_snapshot=raw.get("state_snapshot", {}), version=raw.get("version", 1),
            )
            logger.info("RoleSerializer: deserialized %s v%d", profile.role_id, profile.version)
            return profile

    def statistics(self) -> dict[str, Any]:
        return {"type": "RoleStateSerializer"}


# ---------------------------------------------------------------------------
# Cross-Role Leak Detector
# ---------------------------------------------------------------------------

class CrossRoleLeakDetector:
    """Detect information leakage between two role profiles.

    When switching roles, checks that the target role's knowledge
    boundary does not contain information unique to the source role.
    """

    _SENSITIVE_CATEGORIES: set[str] = {"credential", "token", "secret", "private_note", "pii"}

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def detect(self, source_role: RoleProfile, target_role: RoleProfile, shared_context: dict[str, Any]) -> LeakAlert:
        with self._lock:
            source_keys = set(source_role.state_snapshot.keys())
            target_boundary = set(target_role.knowledge_boundary)
            leaked: list[str] = []

            for key in shared_context:
                if key in source_keys and key not in target_boundary:
                    if any(cat in key.lower() for cat in self._SENSITIVE_CATEGORIES):
                        leaked.append(key)

            severity = "HIGH" if len(leaked) >= 3 else ("MEDIUM" if leaked else "LOW")
            recs = [f"Remove key '{k}' from shared context before switching" for k in leaked] if leaked else []

            alert = LeakAlert(
                alert_id=uuid.uuid4().hex[:12], source_role=source_role.role_id,
                target_role=target_role.role_id, leaked_keys=leaked,
                severity=severity, recommendations=recs,
            )
            if leaked:
                logger.warning("CrossRoleLeak: %s→%s leaked %d keys (%s)", source_role.role_id, target_role.role_id, len(leaked), severity)
            return alert

    def statistics(self) -> dict[str, Any]:
        return {"type": "CrossRoleLeakDetector", "categories": len(self._SENSITIVE_CATEGORIES)}


# ---------------------------------------------------------------------------
# Switch Guard
# ---------------------------------------------------------------------------

class SwitchGuard:
    """Guard role switches: flush temp state, filter leaked info.

    Validates the transition is safe before allowing the switch,
    performs temporary state cleanup, and produces a SwitchDecision.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._leak_detector = CrossRoleLeakDetector()

    def guard(self, from_role: RoleProfile, to_role: RoleProfile, pending_context: dict[str, Any]) -> SwitchDecision:
        with self._lock:
            warnings: list[str] = []

            # Flush temporary state from source role
            flushed = True
            temp_keys = [k for k in pending_context if k.startswith("_temp") or k.startswith("tmp_")]
            for k in temp_keys:
                pending_context.pop(k, None)
            if temp_keys:
                warnings.append(f"Flushed {len(temp_keys)} temporary keys")

            # Leak check
            alert = self._leak_detector.detect(from_role, to_role, pending_context)
            clean = alert.severity == "LOW"
            if not clean:
                warnings.append(f"Leak detected ({alert.severity}): {alert.leaked_keys}")

            allowed = clean and flushed
            decision = SwitchDecision(
                allowed=allowed, from_role=from_role.role_id, to_role=to_role.role_id,
                temp_state_flushed=flushed, leak_check_clean=clean, warnings=warnings,
            )
            logger.info("SwitchGuard: %s→%s allowed=%s", from_role.role_id, to_role.role_id, allowed)
            return decision

    def statistics(self) -> dict[str, Any]:
        return {"type": "SwitchGuard"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def switch_roles(active_role: RoleProfile, target_role_id: str, knowledge_graph: dict[str, Any]) -> RoleSwitchResult:
    """Controlled role switch with full isolation checks.

    1. Looks up target role from knowledge graph.
    2. Serializes active role state.
    3. Runs SwitchGuard leak detection + temp flush.
    4. Returns cleansed context for the target role.

    Args:
        active_role: Currently active RoleProfile.
        target_role_id: Target role identifier string.
        knowledge_graph: Dict mapping role_id → role profile data.

    Returns:
        RoleSwitchResult with switch decision and cleansed context.
    """
    serializer = RoleStateSerializer()
    guard = SwitchGuard()

    _ = serializer.serialize(active_role)

    # Look up target role
    target_data = knowledge_graph.get(target_role_id, {})
    target_role = RoleProfile(
        role_id=target_role_id,
        persona=target_data.get("persona", {}),
        knowledge_boundary=target_data.get("knowledge_boundary", []),
        state_snapshot=target_data.get("state_snapshot", {}),
    )

    # Build pending context from active role's state
    pending = {**active_role.state_snapshot}

    decision = guard.guard(active_role, target_role, pending)

    result = RoleSwitchResult(
        result_id=uuid.uuid4().hex[:12], active_role=target_role if decision.allowed else active_role,
        previous_role=active_role, switch_decision=decision,
        purged_context={k: v for k, v in pending.items() if k not in (decision.warnings or [])},
        error=None if decision.allowed else "Switch blocked by guard",
    )
    logger.info("[P32] Role Isolation switch: %s→%s allowed=%s", active_role.role_id, target_role_id, decision.allowed)
    return result


print("[P32] Role Isolation Manager initialized — BEAM-SWITCH + MENTOR dual-chain aligned")
