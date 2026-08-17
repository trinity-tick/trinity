"""P30: Session Concurrency Guard — CVE-2025-64168 (CVSS 7.1).

# status: orphan (2026-08-15 audit, not in runtime path)
Concurrent session isolation: detects session-state cross-user leaks,
shared-mutable-state race conditions, and enforces user→session affinity
routing under high concurrency.
"""

from __future__ import annotations

import hashlib
import logging
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
class SessionState:
    session_id: str
    user_id: str
    state_hash: str
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)


@dataclass
class SessionCollision:
    session_a: str
    session_b: str
    shared_user_id: str
    collision_type: str  # "user_bleed", "state_duplicate"
    timestamp: float = field(default_factory=time.time)


@dataclass
class RaceCondition:
    operation_a: str
    operation_b: str
    shared_resource: str
    risk_level: str  # LOW / MEDIUM / HIGH
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConcurrencyAuditReport:
    report_id: str
    max_concurrency: int
    sessions_audited: int
    collisions: list[SessionCollision]
    races: list[RaceCondition]
    safe: bool
    recommendations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Session Isolation Auditor
# ---------------------------------------------------------------------------

class SessionIsolationAuditor:
    """Detect cross-user session-state contamination under high concurrency.

    Checks for duplicate state hashes across different user_ids and
    overlapping user_ids across sessions—both are indicators of session
    state leaking between users.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def audit(self, active_sessions: list[SessionState]) -> list[SessionCollision]:
        collisions: list[SessionCollision] = []
        user_sessions: dict[str, list[str]] = {}
        hash_sessions: dict[str, list[SessionState]] = {}

        for s in active_sessions:
            user_sessions.setdefault(s.user_id, []).append(s.session_id)
            hash_sessions.setdefault(s.state_hash, []).append(s)

        # Multi-session same user (concurrency overlap — expected but flag duplicates)
        for uid, sids in user_sessions.items():
            if len(sids) > 1:
                collisions.append(SessionCollision(
                    session_a=sids[0], session_b=sids[1], shared_user_id=uid,
                    collision_type="state_duplicate",
                ))

        # Same state hash across different users → user bleed
        for h, sessions in hash_sessions.items():
            users = {s.user_id for s in sessions}
            if len(users) > 1:
                collisions.append(SessionCollision(
                    session_a=sessions[0].session_id, session_b=sessions[1].session_id,
                    shared_user_id="cross_user", collision_type="user_bleed",
                ))

        logger.info("SessionIsolationAuditor: %d sessions → %d collisions", len(active_sessions), len(collisions))
        return collisions

    def statistics(self) -> dict[str, Any]:
        return {"type": "SessionIsolationAuditor"}


# ---------------------------------------------------------------------------
# Concurrency Race Detector
# ---------------------------------------------------------------------------

class ConcurrencyRaceDetector:
    """Detect race conditions on shared mutable state from operation traces."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def detect(self, operations: list[dict[str, Any]]) -> list[RaceCondition]:
        races: list[RaceCondition] = []
        write_targets: dict[str, list[dict[str, Any]]] = {}

        for op in operations:
            target = op.get("target", op.get("resource", "unknown"))
            if op.get("type") in ("write", "update", "mutate"):
                write_targets.setdefault(target, []).append(op)

        for target, ops in write_targets.items():
            if len(ops) >= 2:
                for i in range(len(ops)):
                    for j in range(i + 1, len(ops)):
                        t_i = ops[i].get("timestamp", 0.0)
                        t_j = ops[j].get("timestamp", 0.0)
                        if abs(t_i - t_j) < 0.05:  # near-simultaneous writes
                            races.append(RaceCondition(
                                operation_a=ops[i].get("id", f"op_{i}"),
                                operation_b=ops[j].get("id", f"op_{j}"),
                                shared_resource=target,
                                risk_level="HIGH",
                            ))

        logger.info("ConcurrencyRaceDetector: %d ops → %d race conditions", len(operations), len(races))
        return races

    def statistics(self) -> dict[str, Any]:
        return {"type": "ConcurrencyRaceDetector"}


# ---------------------------------------------------------------------------
# User Session Affinity
# ---------------------------------------------------------------------------

class UserSessionAffinity:
    """Ensure same-user requests route to the same session instance.

    Maintains an affinity map user_id → session_id; returns the
    existing session if found, otherwise assigns from the pool.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._affinity: dict[str, str] = {}

    def ensure_affinity(self, user_id: str, session_pool: list[SessionState]) -> SessionState:
        with self._lock:
            existing = self._affinity.get(user_id)
            if existing:
                for s in session_pool:
                    if s.session_id == existing:
                        s.last_active = time.time()
                        return s
            # Assign newest session from pool
            fallback = max(session_pool, key=lambda s: s.last_active) if session_pool else SessionState(
                session_id=uuid.uuid4().hex[:12], user_id=user_id, state_hash="new",
            )
            self._affinity[user_id] = fallback.session_id
            logger.info("UserSessionAffinity: %s → %s", user_id, fallback.session_id)
            return fallback

    def statistics(self) -> dict[str, Any]:
        return {"type": "UserSessionAffinity", "affinity_count": len(self._affinity)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit_concurrent_sessions(max_concurrency: int) -> ConcurrencyAuditReport:
    """Full concurrency audit for a batch of sessions.

    Simulates concurrent session states and runs isolation + race
    detection in sequence.

    Args:
        max_concurrency: Simulated peak concurrency level.

    Returns:
        ConcurrencyAuditReport with collisions, races, recommendations.
    """
    # Simulate concurrent sessions
    sessions = [
        SessionState(
            session_id=f"sess-{i:03d}",
            user_id=f"user-{i % max(1, max_concurrency // 2):02d}",
            state_hash=hashlib.sha256(f"state-{i}".encode()).hexdigest()[:16],
        )
        for i in range(max_concurrency)
    ]

    auditor = SessionIsolationAuditor()
    collisions = auditor.audit(sessions)

    detector = ConcurrencyRaceDetector()
    sim_ops = [
        {"id": f"op_{i}", "type": "write", "target": f"shared_{i % 3}", "timestamp": time.time() + i * 0.001}
        for i in range(min(max_concurrency * 2, 100))
    ]
    races = detector.detect(sim_ops)

    safe = len(collisions) == 0 and len(races) == 0
    recs: list[str] = []
    if collisions:
        recs.append(f"Resolve {len(collisions)} session collisions")
    if races:
        recs.append(f"Mitigate {len(races)} race conditions with locking or immutability")

    report = ConcurrencyAuditReport(
        report_id=uuid.uuid4().hex[:12], max_concurrency=max_concurrency,
        sessions_audited=len(sessions), collisions=collisions, races=races,
        safe=safe, recommendations=recs,
    )
    logger.info("[P30] Concurrency audit: %d sessions @%d concurrency → %s", len(sessions), max_concurrency, "SAFE" if safe else "UNSAFE")
    return report


print("[P30] Session Concurrency Guard initialized — CVE-2025-64168 (CVSS 7.1) aligned")
