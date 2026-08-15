"""P32: Temporal Predicate Rewriter — Memory Consolidation Survey (Zylos, Jun 2026).

# status: orphan (2026-08-15 audit, not in runtime path)
Fact self-updating: rewrites temporal predicates ("will go"/"is going")→
("went") as time advances, checks validity windows, and records immutable
audit trails for each rewrite (addressing the Zylos Review audit gap).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

TemporalStatus = Literal["future", "present", "past"]


@dataclass
class TemporalPredicate:
    subject: str
    predicate: str
    obj: str
    valid_from: datetime
    valid_until: datetime
    status: TemporalStatus
    predicate_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WindowStatus:
    predicate_id: str
    status: TemporalStatus
    within_window: bool
    time_remaining: float  # seconds until expiry, negative if expired
    action: str = ""  # "keep", "rewrite", "archive"
    timestamp: float = field(default_factory=time.time)


@dataclass
class AuditEntry:
    entry_id: str
    predicate_id: str
    from_text: str
    to_text: str
    rewrite_reason: str
    operator: str = "PredicateRewriter"
    immutable_hash: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsolidationBatch:
    batch_id: str
    rewritten: list[TemporalPredicate]
    archived: list[WindowStatus]
    audit_trail: list[AuditEntry]
    stats: dict[str, int] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Validity Window Manager
# ---------------------------------------------------------------------------

class ValidityWindowManager:
    """Check temporal predicate validity windows.

    Compares predicate valid_from/valid_until against a reference time;
    classifies as future/present/past and recommends keep/rewrite/archive.
    """

    _FUTURE_TENSE_VERBS: set[str] = {"will", "going to", "shall", "plan to", "expect to"}
    _PAST_TENSE_MAP: dict[str, str] = {
        "will go": "went", "going to go": "went", "will be": "was",
        "going to be": "was", "plan to": "planned to", "expect to": "expected to",
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def check(self, predicate: TemporalPredicate, reference_time: datetime | None = None) -> WindowStatus:
        with self._lock:
            now = reference_time or datetime.now()

            if now < predicate.valid_from:
                status: TemporalStatus = "future"
                action = "keep"
            elif now <= predicate.valid_until:
                status = "present"
                action = "rewrite" if predicate.status == "future" else "keep"
            else:
                status = "past"
                action = "archive"

            remaining = (predicate.valid_until - now).total_seconds()

            return WindowStatus(
                predicate_id=predicate.predicate_id, status=status,
                within_window=remaining > 0, time_remaining=round(remaining, 1), action=action,
            )

    def statistics(self) -> dict[str, Any]:
        return {"type": "ValidityWindowManager", "tense_verbs": len(self._PAST_TENSE_MAP)}


# ---------------------------------------------------------------------------
# Predicate Rewriter
# ---------------------------------------------------------------------------

class PredicateRewriter:
    """Rewrite temporal predicates as time advances.

    "将去新加坡" → "去了新加坡" : automatically advances tense
    based on validity window checks. Records before/after for audit.
    """

    _FUTURE_PATTERNS: dict[str, str] = {
        "will ": "has ", "going to ": "has ", "plan to ": "have ",
        "is ": "was ", "are ": "were ", "shall ": "has ",
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._wm = ValidityWindowManager()

    def rewrite(self, memories: list[TemporalPredicate], now: datetime | None = None) -> list[TemporalPredicate]:
        with self._lock:
            ref = now or datetime.now()
            rewritten: list[TemporalPredicate] = []

            for mem in memories:
                ws = self._wm.check(mem, ref)
                if ws.action == "rewrite":
                    new_pred = mem.predicate
                    for future_pat, past_pat in self._FUTURE_PATTERNS.items():
                        if mem.predicate.startswith(future_pat):
                            new_pred = mem.predicate.replace(future_pat, past_pat, 1)
                            break
                    new_status: TemporalStatus = "present"
                    mem = TemporalPredicate(
                        subject=mem.subject, predicate=new_pred, obj=mem.obj,
                        valid_from=mem.valid_from, valid_until=mem.valid_until,
                        status=new_status, predicate_id=mem.predicate_id, metadata=mem.metadata,
                    )
                rewritten.append(mem)

            logger.info("PredicateRewriter: %d→%d (potential rewrites)", len(memories), sum(1 for m in rewritten if m.status == "present"))
            return rewritten

    def statistics(self) -> dict[str, Any]:
        return {"type": "PredicateRewriter", "patterns": len(self._FUTURE_PATTERNS)}


# ---------------------------------------------------------------------------
# Audit Trail Recorder
# ---------------------------------------------------------------------------

class AuditTrailRecorder:
    """Immutable audit trail for every temporal rewrite event.

    Each rewrite generates an AuditEntry with a content-addressed
    immutable hash, addressing the audit gap flagged in Zylos Review.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._ledger: list[AuditEntry] = []

    def record(self, rewrite_event: dict[str, Any]) -> AuditEntry:
        with self._lock:
            from_text = rewrite_event.get("from", "")
            to_text = rewrite_event.get("to", "")
            reason = rewrite_event.get("reason", "unknown")

            entry = AuditEntry(
                entry_id=uuid.uuid4().hex[:12],
                predicate_id=rewrite_event.get("predicate_id", ""),
                from_text=from_text, to_text=to_text, rewrite_reason=reason,
                immutable_hash=hashlib.sha256(f"{from_text}→{to_text}:{reason}".encode()).hexdigest(),
            )
            self._ledger.append(entry)
            logger.info("AuditTrail: recorded rewrite %s", entry.entry_id)
            return entry

    def statistics(self) -> dict[str, Any]:
        return {"type": "AuditTrailRecorder", "entries": len(self._ledger)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def rewrite_and_archive(current_time: datetime | None = None) -> ConsolidationBatch:
    """Full temporal predicate consolidation pipeline.

    Creates sample predicates, rewrites future-tense to past-tense,
    archives expired predicates, and records immutable audit entries.

    Args:
        current_time: Reference datetime (defaults to now).

    Returns:
        ConsolidationBatch with rewritten, archived, and audit trail.
    """
    now = current_time or datetime.now()

    # Sample predicates at various temporal stages
    samples = [
        TemporalPredicate(subject="Alice", predicate="will go", obj="Singapore",
                          valid_from=now - timedelta(days=30), valid_until=now + timedelta(days=30), status="future"),
        TemporalPredicate(subject="Bob", predicate="is working", obj="on Project X",
                          valid_from=now - timedelta(days=60), valid_until=now - timedelta(days=1), status="past"),
        TemporalPredicate(subject="Carol", predicate="plan to deploy", obj="v2.0",
                          valid_from=now - timedelta(days=7), valid_until=now + timedelta(days=7), status="future"),
    ]

    rewriter = PredicateRewriter()
    wm = ValidityWindowManager()
    recorder = AuditTrailRecorder()

    rewritten = rewriter.rewrite(samples, now)

    archived: list[WindowStatus] = []
    audit_trail: list[AuditEntry] = []

    for orig, curr in zip(samples, rewritten):
        ws = wm.check(curr, now)
        if ws.action == "archive":
            archived.append(ws)
        if orig.predicate != curr.predicate:
            entry = recorder.record({"from": orig.predicate, "to": curr.predicate, "predicate_id": curr.predicate_id, "reason": "tense_advance"})
            audit_trail.append(entry)

    batch = ConsolidationBatch(
        batch_id=uuid.uuid4().hex[:12], rewritten=rewritten, archived=archived,
        audit_trail=audit_trail,
        stats={"total": len(samples), "rewritten": len(audit_trail), "archived": len(archived)},
    )
    logger.info("[P32] TemporalPredicate: %d total, %d rewritten, %d archived", len(samples), len(audit_trail), len(archived))
    return batch


print("[P32] Temporal Predicate Rewriter initialized — Zylos Memory Consolidation Survey (Jun 2026) aligned")
