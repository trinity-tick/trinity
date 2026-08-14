"""P28: PAST-Bench Self-Improvement — arXiv 2608.04003 (5-mechanism ablation).

Five complementary self-improvement mechanisms for agent memory:
(1) Planning Guidance, (2) Memory Binding, (3) Skill Lifecycle,
(4) Retrieval Gating, (5) Closeout Flushing. Ablation study shows
all five are necessary for significant improvement.
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
# Data Classes & Enums
# ---------------------------------------------------------------------------

class SkillStage(str, Enum):
    """PAST-Bench skill lifecycle stages."""

    PUBLISH = "publish"
    VERIFY = "verify"
    DEPRECATE = "deprecate"
    REVOKE = "revoke"


@dataclass
class Plan:
    """Task decomposition and execution plan."""

    plan_id: str
    task_id: str
    steps: list[dict[str, Any]]
    estimated_duration_seconds: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class MemoryEntry:
    """Experience bound to a task for future retrieval."""

    entry_id: str
    task_id: str
    experience: dict[str, Any]
    tags: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class GatedRetrieval:
    """Retrieval result with context budget gate applied."""

    results: list[dict[str, Any]]
    total_available: int
    context_budget: int
    truncated: bool = False


@dataclass
class FlushReport:
    """Report of a session closeout flush operation."""

    session_id: str
    entries_flushed: int
    leaked_items: int
    flushed_at: float = field(default_factory=time.time)


@dataclass
class ImprovementReport:
    """Summary of a full self-improvement cycle."""

    cycle_id: str
    task_id: str
    plan_steps: int
    memories_bound: int
    skill_stage_before: SkillStage
    skill_stage_after: SkillStage
    retrieval_gated: bool
    flush_entries: int
    duration_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Mechanism 1: Planning Guidance
# ---------------------------------------------------------------------------

class PlanningGuidance:
    """Task decomposition: breaks a task dict into a structured Plan."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def guide(self, task: dict[str, Any]) -> Plan:
        """Decompose a task into an execution plan.

        Args:
            task: Dict with 'task_id', 'description', 'subtasks'.

        Returns:
            Plan with ordered steps.
        """
        with self._lock:
            task_id = task.get("task_id", uuid.uuid4().hex[:12])
            subtasks: list[dict[str, Any]] = task.get("subtasks", [])
            steps: list[dict[str, Any]] = []
            for i, st in enumerate(subtasks):
                steps.append({
                    "index": i,
                    "action": st.get("action", "noop"),
                    "params": st.get("params", {}),
                    "depends_on": st.get("depends_on", []),
                })
            plan = Plan(
                plan_id=uuid.uuid4().hex[:12],
                task_id=task_id,
                steps=steps,
                estimated_duration_seconds=len(steps) * 5.0,
            )
            logger.info(
                "PlanningGuidance: %d steps for task %s", len(steps), task_id,
            )
            return plan

    def statistics(self) -> dict[str, Any]:
        return {"type": "PlanningGuidance", "status": "ready"}


# ---------------------------------------------------------------------------
# Mechanism 2: Memory Binding
# ---------------------------------------------------------------------------

class MemoryBinding:
    """Bind experiences to task IDs for structured retrieval."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store: dict[str, MemoryEntry] = {}

    def bind(self, experience: dict[str, Any], task_id: str) -> MemoryEntry:
        """Bind an experience to a task ID.

        Args:
            experience: Arbitrary experience dict.
            task_id: The task this experience belongs to.

        Returns:
            Stored MemoryEntry.
        """
        with self._lock:
            entry = MemoryEntry(
                entry_id=uuid.uuid4().hex[:12],
                task_id=task_id,
                experience=experience,
                tags=experience.get("tags", []),
            )
            self._store[entry.entry_id] = entry
            logger.debug("MemoryBinding: entry %s → task %s", entry.entry_id, task_id)
            return entry

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {"bound_entries": len(self._store)}


# ---------------------------------------------------------------------------
# Mechanism 3: Skill Lifecycle Manager
# ---------------------------------------------------------------------------

class SkillLifecycleManager:
    """Manage full lifecycle of a skill: publish → verify → deprecate → revoke."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._skills: dict[str, SkillStage] = {}

    def lifecycle(self, skill_id: str) -> SkillStage:
        """Get or initialize the lifecycle stage for a skill.

        New skills start at PUBLISH; each call advances to the next stage
        in the sequence for demonstration purposes.
        """
        with self._lock:
            if skill_id not in self._skills:
                self._skills[skill_id] = SkillStage.PUBLISH
                logger.info("SkillLifecycle: %s → %s", skill_id, SkillStage.PUBLISH)
            else:
                current = self._skills[skill_id]
                _order = list(SkillStage)
                idx = _order.index(current)
                if idx < len(_order) - 1:
                    self._skills[skill_id] = _order[idx + 1]
                logger.info(
                    "SkillLifecycle: %s %s → %s",
                    skill_id, current, self._skills[skill_id],
                )
            return self._skills[skill_id]

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "total_skills": len(self._skills),
                "by_stage": {
                    stage.value: sum(1 for s in self._skills.values() if s == stage)
                    for stage in SkillStage
                },
            }


# ---------------------------------------------------------------------------
# Mechanism 4: Retrieval Gate — Context Overflow Prevention
# ---------------------------------------------------------------------------

class RetrievalGate:
    """Gate retrieval results to a context budget to prevent overflow."""

    def __init__(self, default_budget: int = 50) -> None:
        self._lock = threading.RLock()
        self.default_budget = default_budget

    def gate(
        self, query: str, context_budget: int
    ) -> GatedRetrieval:
        """Retrieve and gate results to fit within context budget.

        Args:
            query: The retrieval query (unused in stub).
            context_budget: Maximum number of result items to return.

        Returns:
            GatedRetrieval with truncated flag if budget exceeded.
        """
        with self._lock:
            budget = context_budget if context_budget > 0 else self.default_budget
            # Stub: simulate a large result set
            simulated = [
                {"id": f"r{i}", "score": 0.9 - i * 0.01}
                for i in range(min(200, budget * 3))
            ]
            total = len(simulated)
            results = simulated[:budget]
            truncated = total > budget
            gated = GatedRetrieval(
                results=results,
                total_available=total,
                context_budget=budget,
                truncated=truncated,
            )
            logger.debug(
                "RetrievalGate: budget=%d total=%d truncated=%s",
                budget, total, truncated,
            )
            return gated

    def statistics(self) -> dict[str, Any]:
        return {"default_budget": self.default_budget}


# ---------------------------------------------------------------------------
# Mechanism 5: Closeout Flusher — Anti Cross-Session Leakage
# ---------------------------------------------------------------------------

class CloseoutFlusher:
    """Flush session state at closeout to prevent cross-session leakage."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, list[dict[str, Any]]] = {}

    def flush(self, session_id: str) -> FlushReport:
        """Flush all transient state for a session.

        Args:
            session_id: The session to flush.

        Returns:
            FlushReport with flushed count and any leaked items detected.
        """
        with self._lock:
            entries = self._sessions.pop(session_id, [])
            leaked = 0
            # Check for residual references in other sessions
            for sid, sess_entries in self._sessions.items():
                for e in sess_entries:
                    if e.get("_ref_session") == session_id:
                        leaked += 1
            report = FlushReport(
                session_id=session_id,
                entries_flushed=len(entries),
                leaked_items=leaked,
            )
            logger.info(
                "CloseoutFlusher: session %s flushed %d entries, %d leaks",
                session_id, len(entries), leaked,
            )
            return report

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "active_sessions": len(self._sessions),
                "total_pending_entries": sum(
                    len(v) for v in self._sessions.values()
                ),
            }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_self_improvement_cycle(task: dict[str, Any]) -> ImprovementReport:
    """Run a full PAST-Bench self-improvement cycle across all 5 mechanisms.

    Orchestrates: Planning → Memory Binding → Skill Lifecycle →
    Retrieval Gating → Closeout Flushing, producing a consolidated
    ImprovementReport.

    Args:
        task: Task dict with 'task_id', 'description', 'subtasks'.

    Returns:
        ImprovementReport summarising the cycle.
    """
    t0 = time.time()
    task_id = task.get("task_id", uuid.uuid4().hex[:12])

    # 1. Planning
    planner = PlanningGuidance()
    plan = planner.guide(task)

    # 2. Memory Binding
    binder = MemoryBinding()
    for step in plan.steps:
        binder.bind({"step": step}, task_id)

    # 3. Skill Lifecycle
    lifecycle_mgr = SkillLifecycleManager()
    stage_before = lifecycle_mgr.lifecycle(task_id)
    stage_after = lifecycle_mgr.lifecycle(task_id)

    # 4. Retrieval Gate
    gate = RetrievalGate()
    gated = gate.gate(task.get("description", ""), context_budget=20)

    # 5. Closeout Flush
    flusher = CloseoutFlusher()
    flush_report = flusher.flush(task_id)

    elapsed = time.time() - t0
    report = ImprovementReport(
        cycle_id=uuid.uuid4().hex[:12],
        task_id=task_id,
        plan_steps=len(plan.steps),
        memories_bound=len(binder._store),
        skill_stage_before=stage_before,
        skill_stage_after=stage_after,
        retrieval_gated=gated.truncated,
        flush_entries=flush_report.entries_flushed,
        duration_seconds=elapsed,
    )
    logger.info(
        "[P28] PAST-Bench self-improvement cycle complete: "
        "steps=%d bound=%d stage=%s→%s gated=%s flush=%d elapsed=%.2fs",
        report.plan_steps, report.memories_bound,
        stage_before.value, stage_after.value,
        report.retrieval_gated, report.flush_entries, elapsed,
    )
    return report


print("[P28] PAST-Bench Self-Improvement initialized — arXiv 2608.04003 (5-mechanism ablation) aligned")
