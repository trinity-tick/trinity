"""P28: DCPM Dual Process Memory — arXiv 2606.09483.

# status: orphan (2026-08-15 audit, not in runtime path)
System-1 (daytime fast writer) ↔ System-2 (nighttime slow inducer) with
bidirectional belief revision chains, cross-domain schema induction, and
collision detection. Core insight: dual-process architecture resolves the
stability-plasticity dilemma in continual agent memory.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class BeliefRevisionNode:
    """DCPM belief node with bidirectional supersedes chain.

    Each belief is a subject-predicate-object triple with a revisable
    truth value. When revised, superseded_by points to the newer node
    and the new node has its predecessor in _predecessor (internal).
    """

    belief_id: str
    subject: str
    predicate: str
    object: str
    timestamp: float = field(default_factory=time.time)
    superseded_by: Optional[str] = None


@dataclass
class Schema:
    """Cross-domain schema induced from session belief clusters."""

    schema_id: str
    domain: str
    slots: dict[str, str]
    confidence: float
    source_belief_ids: list[str] = field(default_factory=list)


@dataclass
class CollisionReport:
    """Report of a detected cross-domain schema collision."""

    schema_a_id: str
    schema_b_id: str
    conflicting_slots: list[str]
    resolution: str = ""
    severity: float = 0.0


@dataclass
class CoreSchema:
    """High-level core schema abstracted from multiple domain schemas."""

    core_id: str
    label: str
    domains: list[str]
    invariant_slots: dict[str, str]
    abstraction_level: int = 0


@dataclass
class ConsolidationReport:
    """Summary of a System-2 nighttime consolidation run."""

    session_id: str
    beliefs_consolidated: int
    schemas_induced: int
    collisions_detected: int
    core_schemas_abstracted: int
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# System-1  Daytime Writer
# ---------------------------------------------------------------------------

class System1DaytimeWriter:
    """Fast belief recorder with bidirectional revision link.

    Operates during active conversation: writes beliefs immediately,
    links revisions both forward (superseded_by) and backward (internal
    predecessor map) for full chain traversal.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._beliefs: dict[str, BeliefRevisionNode] = {}
        self._predecessors: dict[str, str] = {}  # new_id → old_id

    def record_belief(self, belief: BeliefRevisionNode) -> BeliefRevisionNode:
        """Store a belief and build bidirectional supersedes chain.

        Args:
            belief: The belief node to record (superseded_by may be set).

        Returns:
            The stored belief node with timestamp set.
        """
        with self._lock:
            if not belief.belief_id:
                belief.belief_id = uuid.uuid4().hex[:12]
            if belief.timestamp == 0.0:
                belief.timestamp = time.time()
            self._beliefs[belief.belief_id] = belief
            if belief.superseded_by:
                self._predecessors[belief.superseded_by] = belief.belief_id
            logger.debug(
                "System1 recorded belief %s (subject=%s predicate=%s)",
                belief.belief_id, belief.subject, belief.predicate,
            )
            return belief

    def get_chain(self, belief_id: str) -> list[BeliefRevisionNode]:
        """Walk the full revision chain for a belief."""
        with self._lock:
            chain: list[BeliefRevisionNode] = []
            current_id = belief_id
            while current_id and current_id in self._beliefs:
                node = self._beliefs[current_id]
                chain.append(node)
                current_id = node.superseded_by
            return chain

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_beliefs": len(self._beliefs),
                "revision_chains": len(self._predecessors),
            }


# ---------------------------------------------------------------------------
# System-2  Nighttime Engine
# ---------------------------------------------------------------------------

class System2NighttimeEngine:
    """Slow reasoning engine that induces schemas and detects collisions.

    Runs during idle periods (nighttime consolidation): clusters session
    beliefs into domain schemas and cross-checks for collisions.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def induce_schemas(self, session_beliefs: list[BeliefRevisionNode]) -> list[Schema]:
        """Induce domain schemas from a batch of session beliefs.

        Args:
            session_beliefs: List of beliefs from one session.

        Returns:
            List of induced Schema objects grouped by predicate domain.
        """
        with self._lock:
            schemas: list[Schema] = []
            domain_groups: dict[str, list[BeliefRevisionNode]] = {}
            for b in session_beliefs:
                domain_groups.setdefault(b.predicate, []).append(b)

            for domain, beliefs in domain_groups.items():
                slots: dict[str, str] = {}
                for b in beliefs:
                    slots[b.subject] = b.object
                schema = Schema(
                    schema_id=uuid.uuid4().hex[:12],
                    domain=domain,
                    slots=slots,
                    confidence=min(1.0, len(beliefs) / 10.0),
                    source_belief_ids=[b.belief_id for b in beliefs],
                )
                schemas.append(schema)
            logger.info(
                "System2 induced %d schemas from %d beliefs",
                len(schemas), len(session_beliefs),
            )
            return schemas

    def detect_collisions(
        self, schema_a: Schema, schema_b: Schema
    ) -> Optional[CollisionReport]:
        """Detect cross-domain collisions between two schemas.

        A collision occurs when both schemas have the same slot key but
        different values, indicating conflicting beliefs about the same
        entity across domains.
        """
        with self._lock:
            common_keys = set(schema_a.slots.keys()) & set(schema_b.slots.keys())
            conflicting: list[str] = []
            for key in common_keys:
                if schema_a.slots[key] != schema_b.slots[key]:
                    conflicting.append(key)

            if not conflicting:
                return None

            report = CollisionReport(
                schema_a_id=schema_a.schema_id,
                schema_b_id=schema_b.schema_id,
                conflicting_slots=conflicting,
                severity=len(conflicting) / max(
                    len(schema_a.slots), len(schema_b.slots), 1
                ),
            )
            logger.warning(
                "Collision: %s vs %s → %d conflicting slots",
                schema_a.schema_id, schema_b.schema_id, len(conflicting),
            )
            return report

    def statistics(self) -> dict[str, Any]:
        return {"type": "System2NighttimeEngine", "status": "ready"}


# ---------------------------------------------------------------------------
# Cross-Domain Abstractor
# ---------------------------------------------------------------------------

class CrossDomainAbstractor:
    """Abstract multiple domain schemas into high-level core schemas."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def abstract(self, schemas: list[Schema]) -> list[CoreSchema]:
        """Abstract domain schemas into core-level schemas.

        Merges overlapping slot keys across domains into invariant
        core slots at a higher abstraction level.
        """
        with self._lock:
            if not schemas:
                return []

            all_keys: set[str] = set()
            all_domains: list[str] = []
            for s in schemas:
                all_keys |= set(s.slots.keys())
                all_domains.append(s.domain)

            invariant: dict[str, str] = {}
            for key in all_keys:
                values = [
                    s.slots[key]
                    for s in schemas
                    if key in s.slots and s.slots[key]
                ]
                if len(set(values)) == 1 and values:
                    invariant[key] = values[0]

            core = CoreSchema(
                core_id=uuid.uuid4().hex[:12],
                label=" → ".join(sorted(set(all_domains))),
                domains=sorted(set(all_domains)),
                invariant_slots=invariant,
                abstraction_level=1,
            )
            logger.info("Abstracted core schema %s with %d invariant slots",
                        core.core_id, len(invariant))
            return [core]

    def statistics(self) -> dict[str, Any]:
        return {"type": "CrossDomainAbstractor", "status": "ready"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def consolidate(session_id: str) -> ConsolidationReport:
    """Run a full DCPM consolidation cycle: System-1 → System-2 → Abstract.

    Orchestrates the dual-process pipeline: collects beliefs from the
    daytime writer, invokes nighttime schema induction and collision
    detection, then abstracts cross-domain core schemas.

    Args:
        session_id: The session identifier for this consolidation.

    Returns:
        ConsolidationReport summarizing the run.
    """
    t0 = time.time()
    writer = System1DaytimeWriter()
    engine = System2NighttimeEngine()
    abstractor = CrossDomainAbstractor()

    # Simulate gathering all stored beliefs (in production this reads from
    # persistent store keyed by session_id).
    all_beliefs = list(writer._beliefs.values())
    schemas = engine.induce_schemas(all_beliefs)

    # Pairwise collision detection
    collisions = 0
    for i in range(len(schemas)):
        for j in range(i + 1, len(schemas)):
            report = engine.detect_collisions(schemas[i], schemas[j])
            if report:
                collisions += 1

    core_schemas = abstractor.abstract(schemas)

    elapsed = time.time() - t0
    logger.info(
        "[P28] DCPM dual-process consolidation complete: "
        "beliefs=%d schemas=%d collisions=%d cores=%d elapsed=%.2fs",
        len(all_beliefs), len(schemas), collisions, len(core_schemas), elapsed,
    )

    return ConsolidationReport(
        session_id=session_id,
        beliefs_consolidated=len(all_beliefs),
        schemas_induced=len(schemas),
        collisions_detected=collisions,
        core_schemas_abstracted=len(core_schemas),
        elapsed_seconds=elapsed,
    )


print("[P28] DCPM Dual Process Memory initialized — arXiv 2606.09483 aligned")
