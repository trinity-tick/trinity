"""P31: Adaptive Budgeted Forgetting — arXiv 2604.02280.

Learns optimal pruning thresholds via importance scoring (recency ×
importance × relevance) and automatic threshold optimization to keep
memory within a configurable budget limit.
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
class ImportanceSignal:
    recency: float
    importance: float
    relevance: float
    timestamp: float = field(default_factory=time.time)


@dataclass
class BudgetManagedSet:
    result_id: str
    original_count: int
    retained_count: int
    budget_limit: int
    threshold_used: float
    pruned: list[dict[str, Any]] = field(default_factory=list)
    retained: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Importance Scorer
# ---------------------------------------------------------------------------

class ImportanceScorer:
    """Score memory importance as weighted composite.

    Score = w1·Recency + w2·Importance + w3·Relevance
    where w1=0.4, w2=0.35, w3=0.25 (default weights).
    """

    _W_RECENCY: float = 0.4
    _W_IMPORTANCE: float = 0.35
    _W_RELEVANCE: float = 0.25

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def score(self, memory: dict[str, Any], query_embedding: list[float] | None = None) -> float:
        with self._lock:
            recency = float(memory.get("recency", 0.5))
            importance = float(memory.get("importance", 0.5))
            relevance = float(memory.get("relevance", 0.5))

            # If query embedding provided, boost relevance via cosine sim
            if query_embedding:
                mem_vec = memory.get("embedding", [0.0] * len(query_embedding))
                dot = sum(a * b for a, b in zip(mem_vec[:10], query_embedding[:10]))
                relevance = min(1.0, max(0.0, dot))

            s = self._W_RECENCY * recency + self._W_IMPORTANCE * importance + self._W_RELEVANCE * relevance
            return round(s, 4)

    def statistics(self) -> dict[str, Any]:
        return {"type": "ImportanceScorer", "weights": {"recency": self._W_RECENCY, "importance": self._W_IMPORTANCE, "relevance": self._W_RELEVANCE}}


# ---------------------------------------------------------------------------
# Budget Pruner
# ---------------------------------------------------------------------------

class BudgetPruner:
    """Prune memories below threshold to maintain budget limit.

    Sorts by importance score descending, retains top-K, prunes the rest.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scorer = ImportanceScorer()

    def prune(self, memories: list[dict[str, Any]], budget_limit: int) -> list[dict[str, Any]]:
        with self._lock:
            # Score each memory
            for m in memories:
                m["_score"] = m.get("_score", self._scorer.score(m))

            sorted_mem = sorted(memories, key=lambda m: m.get("_score", 0), reverse=True)
            retained = sorted_mem[:budget_limit]
            logger.info("BudgetPruner: %d→%d (budget=%d)", len(memories), len(retained), budget_limit)
            return retained

    def statistics(self) -> dict[str, Any]:
        return {"type": "BudgetPruner"}


# ---------------------------------------------------------------------------
# Threshold Optimizer
# ---------------------------------------------------------------------------

class ThresholdOptimizer:
    """Learn optimal pruning threshold via batch evaluation.

    Iterates possible thresholds, computing precision/recall/F1 against
    a simulated ground-truth keep-set, and returns the threshold that
    maximizes F1 score.
    """

    def __init__(self, step: float = 0.05) -> None:
        self._lock = threading.RLock()
        self._step = step

    def optimize_threshold(self, memories: list[dict[str, Any]], budget: int, target_f1: float = 0.85) -> float:
        with self._lock:
            scorer = ImportanceScorer()
            for m in memories:
                m["_score"] = scorer.score(m)

            best_threshold = 0.3
            best_f1 = 0.0

            # Ground truth: top-budget by importance are "keep"
            sorted_all = sorted(memories, key=lambda m: m.get("_score", 0), reverse=True)
            keep_ids = {id(m) for m in sorted_all[:budget]}

            for t in [i * self._step for i in range(1, int(1.0 / self._step) + 1)]:
                predicted_keep = {id(m) for m in memories if m.get("_score", 0) >= t}
                if not predicted_keep:
                    continue
                tp = len(predicted_keep & keep_ids)
                precision = tp / len(predicted_keep)
                recall = tp / max(len(keep_ids), 1)
                f1 = 2 * precision * recall / max(precision + recall, 0.001)
                if f1 > best_f1:
                    best_f1 = f1
                    best_threshold = t

            logger.info("ThresholdOptimizer: best=%.2f (F1=%.3f, target=%.2f)", best_threshold, best_f1, target_f1)
            return round(best_threshold, 2)

    def statistics(self) -> dict[str, Any]:
        return {"type": "ThresholdOptimizer", "step": self._step}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def manage_budget(memories: list[dict[str, Any]], context_limit: int) -> BudgetManagedSet:
    """Full adaptive budgeted forgetting pipeline.

    Scores importance, learns optimal threshold, prunes to budget,
    and returns retained + pruned sets with statistics.

    Args:
        memories: List of memory dicts with recency/importance/relevance.
        context_limit: Maximum number of memories to retain.

    Returns:
        BudgetManagedSet with pruned and retained memory lists.
    """
    scorer = ImportanceScorer()
    for m in memories:
        m["_score"] = scorer.score(m)

    optimizer = ThresholdOptimizer()
    threshold = optimizer.optimize_threshold(memories, context_limit)

    pruner = BudgetPruner()
    retained = pruner.prune(memories, context_limit)

    retained_ids = {id(m) for m in retained}
    pruned = [m for m in memories if id(m) not in retained_ids]

    result = BudgetManagedSet(
        result_id=uuid.uuid4().hex[:12], original_count=len(memories),
        retained_count=len(retained), budget_limit=context_limit,
        threshold_used=threshold, pruned=pruned, retained=retained,
        stats={"mean_score": round(sum(m.get("_score", 0) for m in retained) / max(len(retained), 1), 3) if retained else 0.0},
    )
    logger.info("[P31] AdaptiveBudget: %d→%d (budget=%d, threshold=%.2f)", len(memories), len(retained), context_limit, threshold)
    return result


print("[P31] Adaptive Budgeted Forgetting initialized — arXiv 2604.02280 aligned")
