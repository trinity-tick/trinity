"""P33: MemCollab Contrastive Distillation — arXiv 2603.23234.

Cross-model memory collaboration: contrasts reasoning trajectories from
different model-based agents, distills task-invariant constraints via KL
divergence minimization, and gates retrieval by task category.
"""

from __future__ import annotations

import logging
import math
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
class ReasoningTrajectory:
    trajectory_id: str
    model_name: str
    task_category: str
    steps: list[dict[str, Any]]
    outcome: str  # "success" / "failure"
    token_distribution: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskInvariantConstraint:
    constraint_id: str
    task_category: str
    invariant_rule: str
    abstract_constraint: dict[str, Any]
    source_models: list[str]
    confidence: float  # 0.0–1.0
    distillation_loss: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetrievalGateResult:
    matched_constraints: list[TaskInvariantConstraint]
    rejected_count: int
    gate_reason: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Cross-Model Constraint Distiller
# ---------------------------------------------------------------------------

class CrossModelConstraintDistiller:
    """Distill task-invariant constraints via contrastive trajectory analysis.

    For each task category, pairs trajectories from different models,
    computes KL divergence between their token distributions, and extracts
    abstract constraints that survive across model boundaries (i.e., low
    cross-model KL → high invariance).
    """

    _KL_THRESHOLD: float = 0.35  # below this = invariant

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._constraints: dict[str, list[TaskInvariantConstraint]] = {}

    def distill(self, trajectories: list[ReasoningTrajectory]) -> list[TaskInvariantConstraint]:
        with self._lock:
            by_task: dict[str, list[ReasoningTrajectory]] = {}
            for t in trajectories:
                by_task.setdefault(t.task_category, []).append(t)

            results: list[TaskInvariantConstraint] = []

            for task_cat, trajs in by_task.items():
                models_present = list({t.model_name for t in trajs})
                if len(models_present) < 2:
                    continue

                # Pairwise cross-model KL
                for i in range(len(trajs)):
                    for j in range(i + 1, len(trajs)):
                        a, b = trajs[i], trajs[j]
                        if a.model_name == b.model_name:
                            continue

                        kl = self._kl_divergence(a.token_distribution, b.token_distribution)
                        if kl < self._KL_THRESHOLD:
                            # Steps that appear in both trajectories are invariants
                            a_steps = {s.get("action", ""): s for s in a.steps}
                            b_steps = {s.get("action", ""): s for s in b.steps}
                            common = set(a_steps) & set(b_steps)
                            for act in common:
                                constraint = TaskInvariantConstraint(
                                    constraint_id=uuid.uuid4().hex[:12],
                                    task_category=task_cat,
                                    invariant_rule=f"Do {act} as: {a_steps[act].get('description', act)}",
                                    abstract_constraint={"action": act, "precondition": a_steps[act].get("pre", {}), "postcondition": a_steps[act].get("post", {})},
                                    source_models=[a.model_name, b.model_name],
                                    confidence=round(1.0 - kl, 3),
                                    distillation_loss=round(kl, 4),
                                )
                                results.append(constraint)

            self._constraints["global"] = results
            logger.info("MemCollab Distiller: %d trajectories → %d invariants across %d categories", len(trajectories), len(results), len(by_task))
            return results

    @staticmethod
    def _kl_divergence(p: dict[str, float], q: dict[str, float]) -> float:
        kl = 0.0
        all_keys = set(p) | set(q)
        for k in all_keys:
            pk = p.get(k, 1e-9)
            qk = q.get(k, 1e-9)
            kl += pk * math.log(pk / qk) if pk > 0 else 0.0
        return kl

    def statistics(self) -> dict[str, Any]:
        return {"type": "CrossModelConstraintDistiller", "constraints": len(self._constraints.get("global", [])), "kl_threshold": self._KL_THRESHOLD}


# ---------------------------------------------------------------------------
# Task-Aware Retrieval Gate
# ---------------------------------------------------------------------------

class TaskAwareRetrievalGate:
    """Filter constraints by task category at inference time.

    Ensures only relevant invariants are retrieved, preventing interference
    from constraints belonging to unrelated task types.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._task_index: dict[str, list[TaskInvariantConstraint]] = {}

    def index(self, constraints: list[TaskInvariantConstraint]) -> None:
        with self._lock:
            for c in constraints:
                self._task_index.setdefault(c.task_category, []).append(c)

    def filter(self, task_category: str, constraints: list[TaskInvariantConstraint] | None = None) -> RetrievalGateResult:
        with self._lock:
            pool = constraints if constraints else self._task_index.get(task_category, [])
            if constraints and task_category:
                pool = [c for c in pool if c.task_category == task_category]

            matched = sorted(pool, key=lambda c: c.confidence, reverse=True)
            rejected = len(pool) - len(matched)
            reason = f"Category='{task_category}' matched {len(matched)} constraints" if matched else f"No constraints for category '{task_category}'"

            logger.info("TaskAwareRetrievalGate: %s → %d matched, %d rejected", task_category, len(matched), rejected)
            return RetrievalGateResult(matched_constraints=matched, rejected_count=rejected, gate_reason=reason)

    def statistics(self) -> dict[str, Any]:
        return {"type": "TaskAwareRetrievalGate", "indexed_categories": len(self._task_index)}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def contrastive_distill(trajectories: list[ReasoningTrajectory]) -> list[TaskInvariantConstraint]:
    """Full MemCollab pipeline: distill + index + gate.

    1. Distill cross-model invariants via contrastive KL.
    2. Index constraints by task category.
    3. Return all invariants (caller may gate by task at inference).

    Args:
        trajectories: List of ReasoningTrajectory from heterogeneous models.

    Returns:
        List of TaskInvariantConstraint distilled across model boundaries.
    """
    distiller = CrossModelConstraintDistiller()
    gate = TaskAwareRetrievalGate()

    constraints = distiller.distill(trajectories)
    gate.index(constraints)

    logger.info("[P33] MemCollab contrastive_distill: %d invariants from %d trajectories", len(constraints), len(trajectories))
    return constraints


print("[P33] MemCollab Contrastive Distillation initialized — arXiv 2603.23234 aligned")
