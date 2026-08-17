"""P31: VerMem Unified Memory Operations — arXiv 2608.03137 (2026.08.04).

# status: orphan (2026-08-15 audit, not in runtime path)
Unified 7-operation policy (ADD/REVISE/SOFT_DELETE/RETRIEVE/FILTER/
SUMMARIZE/RESTORE) across 3 memory states with local→global verification.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums & Data
# ---------------------------------------------------------------------------

class MemoryState(str, Enum):
    LTM = "long_term_memory"
    ACTIVE_CONTEXT = "active_context"
    EPISODIC_HISTORY = "episodic_history"


class AtomicOperation(str, Enum):
    ADD = "add"
    REVISE = "revise"
    SOFT_DELETE = "soft_delete"
    RETRIEVE = "retrieve"
    FILTER = "filter"
    SUMMARIZE = "summarize"
    RESTORE = "restore"


@dataclass
class OperationResult:
    op: AtomicOperation
    target: MemoryState
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class VerificationScore:
    legal: bool
    score: float  # 0.0–1.0
    reason: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsistencyReport:
    consistent: bool
    evidence_coherence: float  # 0.0–1.0
    gaps: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class VerifiedOperationLog:
    log_id: str
    operations: list[OperationResult]
    local_scores: list[VerificationScore]
    consistency: ConsistencyReport | None = None
    overall_verdict: str = "PENDING"
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Memory Operation Policy
# ---------------------------------------------------------------------------

class MemoryOperationPolicy:
    """Execute 7 atomic operations with state-aware routing.

    Each (op, MemoryState) pair has a configured handler; unknown pairs
    fall back to a no-op. Thread-safe with per-state locks.
    """

    _VALID_PAIRS: dict[tuple[AtomicOperation, MemoryState], str] = {
        (AtomicOperation.ADD, MemoryState.LTM): "ltm_insert",
        (AtomicOperation.ADD, MemoryState.ACTIVE_CONTEXT): "context_push",
        (AtomicOperation.REVISE, MemoryState.LTM): "ltm_update",
        (AtomicOperation.REVISE, MemoryState.ACTIVE_CONTEXT): "context_update",
        (AtomicOperation.SOFT_DELETE, MemoryState.LTM): "ltm_tombstone",
        (AtomicOperation.SOFT_DELETE, MemoryState.ACTIVE_CONTEXT): "context_expire",
        (AtomicOperation.RETRIEVE, MemoryState.LTM): "ltm_search",
        (AtomicOperation.RETRIEVE, MemoryState.ACTIVE_CONTEXT): "context_peek",
        (AtomicOperation.FILTER, MemoryState.LTM): "ltm_filter",
        (AtomicOperation.FILTER, MemoryState.EPISODIC_HISTORY): "episodic_filter",
        (AtomicOperation.SUMMARIZE, MemoryState.EPISODIC_HISTORY): "episodic_summarize",
        (AtomicOperation.RESTORE, MemoryState.LTM): "ltm_restore",
        (AtomicOperation.RESTORE, MemoryState.EPISODIC_HISTORY): "episodic_restore",
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, dict[str, Any]] = {}

    def execute(self, op: AtomicOperation, target: MemoryState, payload: dict[str, Any]) -> OperationResult:
        t0 = time.perf_counter()
        with self._lock:
            handler = self._VALID_PAIRS.get((op, target))
            if handler is None:
                return OperationResult(op=op, target=target, success=False, error=f"Invalid pair ({op.value},{target.value})")

            key = payload.get("id", payload.get("key", uuid.uuid4().hex[:8]))
            bucket = self._state.setdefault(target.value, {})

            if handler.endswith("insert") or handler.endswith("push"):
                bucket[key] = payload
            elif handler.endswith("update"):
                if key in bucket:
                    bucket[key] = {**bucket[key], **payload}
            elif handler.endswith("tombstone"):
                bucket[key] = {"_tombstone": True, "_data": bucket.pop(key, None)}
            elif handler == "context_expire":
                bucket[key] = {"_expired": time.time()}
            elif handler in ("ltm_search", "context_peek"):
                payload["_found"] = bucket.get(key)
            elif handler.endswith("filter"):
                query = payload.get("query", "")
                payload["_results"] = [v for k, v in bucket.items() if query and query.lower() in str(v).lower()]
            elif handler.endswith("summarize"):
                payload["_summary"] = f"Summarized {len(bucket)} episodic entries"
            elif handler.endswith("restore"):
                tomb = bucket.get(key, {})
                if tomb.get("_tombstone"):
                    bucket[key] = tomb.get("_data", {})
                    payload["_restored"] = True

            elapsed = (time.perf_counter() - t0) * 1000
            logger.info("VerMem %s→%s (%s) ok=%.1fms", op.value, target.value, handler, elapsed)
            return OperationResult(op=op, target=target, success=True, output=payload, latency_ms=elapsed)

    def statistics(self) -> dict[str, Any]:
        return {"type": "MemoryOperationPolicy", "states": {k: len(v) for k, v in self._state.items()}, "valid_pairs": len(self._VALID_PAIRS)}


# ---------------------------------------------------------------------------
# Local Verifier
# ---------------------------------------------------------------------------

class LocalVerifier:
    """Score single-step memory transitions for legality.

    Checks that source → target state transitions follow valid paths
    in the memory state graph.
    """

    _LEGAL_TRANSITIONS: set[tuple[str, str]] = {
        ("ltm", "ltm"), ("context", "context"), ("ltm", "context"), ("context", "ltm"),
        ("episodic", "episodic"), ("episodic", "ltm"),
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def verify(self, transition: dict[str, Any]) -> VerificationScore:
        with self._lock:
            src = transition.get("from", "").lower()[:8]
            dst = transition.get("to", "").lower()[:8]
            if (src, dst) not in self._LEGAL_TRANSITIONS:
                return VerificationScore(legal=False, score=0.0, reason=f"Illegal transition {src}→{dst}")
            # Score based on payload size sanity
            size = len(str(transition.get("payload", "")))
            score = min(1.0, 1000.0 / max(size, 1))
            return VerificationScore(legal=True, score=min(1.0, round(score, 3)), reason="")

    def statistics(self) -> dict[str, Any]:
        return {"type": "LocalVerifier", "legal_transitions": len(self._LEGAL_TRANSITIONS)}


# ---------------------------------------------------------------------------
# Global Verifier
# ---------------------------------------------------------------------------

class GlobalVerifier:
    """Evaluate evidence coherence and terminal state consistency.

    Checks for gaps in the evidence chain and contradictions between
    consecutive verification steps.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def verify_consistency(self, terminal_state: dict[str, Any], evidence_chain: list[dict[str, Any]]) -> ConsistencyReport:
        with self._lock:
            gaps: list[str] = []
            contradictions: list[str] = []
            coherence = 1.0

            for i in range(len(evidence_chain)):
                e = evidence_chain[i]
                if e.get("success") is False:
                    gaps.append(f"Failure at step {i}: {e.get('error', 'unknown')}")
                    coherence -= 0.2
                if i > 0:
                    prev_out = evidence_chain[i - 1].get("output", {})
                    curr_in = e.get("input", {})
                    if prev_out and curr_in and prev_out.get("id") != curr_in.get("id"):
                        contradictions.append(f"ID mismatch step {i-1}→{i}")

            coherence = max(0.0, coherence)
            consistent = len(gaps) == 0 and len(contradictions) == 0
            recs = [f"Address {len(gaps)} gaps, {len(contradictions)} contradictions"] if not consistent else []

            return ConsistencyReport(
                consistent=consistent, evidence_coherence=round(coherence, 2),
                gaps=gaps, contradictions=contradictions, recommendations=recs,
            )

    def statistics(self) -> dict[str, Any]:
        return {"type": "GlobalVerifier"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def operate_and_verify(operations: list[dict[str, Any]]) -> VerifiedOperationLog:
    """Execute a chain of memory operations with local→global verification.

    Args:
        operations: Each dict with 'op', 'target', 'payload'.

    Returns:
        VerifiedOperationLog with per-step results, scores, and consistency.
    """
    policy = MemoryOperationPolicy()
    local = LocalVerifier()
    global_v = GlobalVerifier()

    results: list[OperationResult] = []
    scores: list[VerificationScore] = []
    evidence: list[dict[str, Any]] = []
    terminal_state: dict[str, Any] = {}
    all_ok = True

    for step in operations:
        op = AtomicOperation(step.get("op", "add"))
        target = MemoryState(step.get("target", "active_context"))
        payload = step.get("payload", {})
        result = policy.execute(op, target, payload)
        results.append(result)
        all_ok = all_ok and result.success

        score = local.verify({"from": target.value, "to": target.value, "payload": result.output})
        scores.append(score)

        evidence.append({"success": result.success, "output": result.output, "input": payload, "error": result.error})
        terminal_state = result.output

    consistency = global_v.verify_consistency(terminal_state, evidence)
    verdict = "PASS" if all_ok and consistency.consistent else "FAIL"

    log = VerifiedOperationLog(
        log_id=uuid.uuid4().hex[:12], operations=results, local_scores=scores,
        consistency=consistency, overall_verdict=verdict,
    )
    logger.info("[P31] VerMem operate_and_verify: %d ops → %s", len(operations), verdict)
    return log


print("[P31] VerMem Unified Memory Operations initialized — arXiv 2608.03137 aligned")
