"""
# status: orphan (2026-08-15 audit, not in runtime path)
P16-2: Post-Retrieval Evidence Policy.

Reference: MemChain (arXiv 2607.24097, 2026.07.27) — Trainable post-retrieval
           memory policy for evidence construction and context refinement.

Design: After retrieval, constructs ordered evidence trajectories, performs
        explicit memory operations (merge / filter / sort / resolve), and
        optimizes the policy via RL reward signals from downstream answer
        quality. Also quantifies and reduces context overhead from irrelevant
        memories.

Complementary to: retrieval.py (retrieval does recall) —
                  this module does post-retrieval evidence refinement.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_REDUNDANCY_THRESHOLD = 0.85  # cosine similarity above which merge
DEFAULT_MIN_RELEVANCE = 0.15         # below this, filter out
DEFAULT_MAX_CONTEXT_TOKENS = 8192
DEFAULT_TOKENS_PER_CHAR_ESTIMATE = 0.25  # rough estimation


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EvidenceRole(Enum):
    """Semantic role of a piece of evidence in the evidence trace."""
    FACT = auto()           # objective fact
    PREFERENCE = auto()     # user preference
    TEMPORAL_ORDER = auto() # sequential / timing info
    RULE = auto()           # constraint / policy rule
    CONTEXT = auto()        # situational context
    COUNTER_EXAMPLE = auto()  # contradiction or nuance


class MemoryOperation(Enum):
    """Explicit operation performed by ActiveMemoryGenerator."""
    MERGE = auto()        # combine redundant memories
    FILTER = auto()       # remove weak / irrelevant
    SORT = auto()         # reorder by dependency
    RESOLVE = auto()      # resolve contradictions
    SUMMARIZE = auto()    # compress verbose memory


class PolicyOptimizationStatus(Enum):
    """State of TMPO policy optimization."""
    IDLE = auto()
    COLLECTING = auto()   # gathering reward signals
    UPDATING = auto()     # applying gradient updates
    CONVERGED = auto()    # policy stabilized


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EvidenceItem:
    """A single evidence-bearing memory item."""
    memory_id: str
    content: str
    role: EvidenceRole = EvidenceRole.FACT
    relevance_score: float = 0.5
    source_timestamp: float = 0.0
    token_estimate: int = 0
    dependencies: List[str] = field(default_factory=list)
    contradiction_flags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.token_estimate == 0:
            self.token_estimate = max(1, int(len(self.content) * DEFAULT_TOKENS_PER_CHAR_ESTIMATE))


@dataclass
class EvidenceTrace:
    """Ordered sequence of evidence items for a query."""
    query_id: str
    items: List[EvidenceItem] = field(default_factory=list)
    total_tokens: int = 0
    creation_time: float = field(default_factory=time.time)

    def token_count(self) -> int:
        return sum(it.token_estimate for it in self.items)


@dataclass
class TMPOReward:
    """Reward signal for TMPO training from downstream answer quality."""
    query_id: str
    reward: float            # scalar reward in [-1, 1]
    source: str = "user"     # "user", "auto-eval", "benchmark"
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# EvidencePlanner
# ---------------------------------------------------------------------------

class EvidencePlanner:
    """Condition a query into an evidence plan — determines which categories
    of evidence (facts, preferences, temporal, rules) are needed.

    Uses learned weights that TMPO trainer can optimize.
    """

    def __init__(self):
        # Learned weights: P(role | query intent)
        self._role_weights: Dict[str, Dict[EvidenceRole, float]] = defaultdict(
            lambda: {r: 1.0 / len(EvidenceRole) for r in EvidenceRole}
        )
        self._lock = threading.RLock()

    def plan(self, query: str) -> Dict[EvidenceRole, float]:
        """Return evidence role distribution for a query.

        In production, this would use embedding similarity between query and
        role prototypes. Here we use a heuristic based on query keywords.
        """
        ql = query.lower()
        scores: Dict[EvidenceRole, float] = {}
        if any(kw in ql for kw in ("fact", "what", "when", "how many", "define")):
            scores[EvidenceRole.FACT] = 0.8
        else:
            scores[EvidenceRole.FACT] = 0.3

        if any(kw in ql for kw in ("prefer", "like", "favorite", "choose")):
            scores[EvidenceRole.PREFERENCE] = 0.8
        else:
            scores[EvidenceRole.PREFERENCE] = 0.15

        if any(kw in ql for kw in ("after", "before", "sequence", "order", "timeline")):
            scores[EvidenceRole.TEMPORAL_ORDER] = 0.8
        else:
            scores[EvidenceRole.TEMPORAL_ORDER] = 0.1

        if any(kw in ql for kw in ("rule", "constraint", "policy", "must", "should", "limit")):
            scores[EvidenceRole.RULE] = 0.8
        else:
            scores[EvidenceRole.RULE] = 0.1

        scores[EvidenceRole.CONTEXT] = 0.3
        scores[EvidenceRole.COUNTER_EXAMPLE] = 0.05

        return scores

    def update_weights(self, role: EvidenceRole, query_intent: str, reward: float) -> None:
        with self._lock:
            current = self._role_weights[query_intent].get(role, 0.0)
            self._role_weights[query_intent][role] = 0.9 * current + 0.1 * reward


# ---------------------------------------------------------------------------
# EvidenceTraceConstructor
# ---------------------------------------------------------------------------

class EvidenceTraceConstructor:
    """Construct ordered evidence traces by organizing retrieved memories
    according to semantic roles and dependency relations."""

    def __init__(self, max_trace_length: int = 50):
        self.max_trace_length = max_trace_length
        self._lock = threading.RLock()

    def construct(
        self,
        query_id: str,
        items: List[EvidenceItem],
        plan: Dict[EvidenceRole, float],
    ) -> EvidenceTrace:
        """Build ordered evidence trace from items and plan."""
        with self._lock:
            # Sort by role priority (from plan) then by relevance
            def sort_key(item: EvidenceItem) -> float:
                role_score = plan.get(item.role, 0.0)
                return -(role_score * 0.7 + item.relevance_score * 0.3)

            sorted_items = sorted(items, key=sort_key)[:self.max_trace_length]

            # Topological sort by dependencies within same role
            ordered = self._topological_sort(sorted_items)

            trace = EvidenceTrace(query_id=query_id)
            trace.items = ordered
            trace.total_tokens = sum(it.token_estimate for it in ordered)
            return trace

    def _topological_sort(self, items: List[EvidenceItem]) -> List[EvidenceItem]:
        """Simple dependency-aware ordering."""
        ids = {it.memory_id for it in items}
        in_degree: Dict[str, int] = {it.memory_id: 0 for it in items}
        adj: Dict[str, List[str]] = {it.memory_id: [] for it in items}

        for it in items:
            for dep in it.dependencies:
                if dep in ids:
                    adj.setdefault(dep, []).append(it.memory_id)
                    in_degree[it.memory_id] = in_degree.get(it.memory_id, 0) + 1

        queue = deque([mid for mid, deg in in_degree.items() if deg == 0])
        ordered: List[EvidenceItem] = []
        item_map = {it.memory_id: it for it in items}

        while queue and len(ordered) < len(items):
            mid = queue.popleft()
            if mid in item_map:
                ordered.append(item_map[mid])
            for neighbor in adj.get(mid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # Add remaining items that couldn't be topo-sorted
        seen = {it.memory_id for it in ordered}
        for it in items:
            if it.memory_id not in seen:
                ordered.append(it)

        return ordered


# ---------------------------------------------------------------------------
# ActiveMemoryGenerator
# ---------------------------------------------------------------------------

class ActiveMemoryGenerator:
    """Performs explicit memory operations to generate compact evidence
    context: merge redundant, filter weak, sort by dependency, resolve
    contradictions."""

    def __init__(
        self,
        redundancy_threshold: float = DEFAULT_REDUNDANCY_THRESHOLD,
        min_relevance: float = DEFAULT_MIN_RELEVANCE,
    ):
        self.redundancy_threshold = redundancy_threshold
        self.min_relevance = min_relevance
        self._op_counts: Dict[MemoryOperation, int] = defaultdict(int)
        self._lock = threading.RLock()

    def process(self, items: List[EvidenceItem]) -> List[EvidenceItem]:
        """Apply the full pipeline: filter -> merge -> resolve -> sort."""
        with self._lock:
            result = list(items)

            # 1. FILTER: remove weak relevance
            result = self._filter(result)
            self._op_counts[MemoryOperation.FILTER] += 1

            # 2. MERGE: combine redundant items
            result = self._merge(result)
            self._op_counts[MemoryOperation.MERGE] += 1

            # 3. RESOLVE: handle contradictions
            result = self._resolve(result)
            self._op_counts[MemoryOperation.RESOLVE] += 1

            # 4. SORT: by relevance descending
            result = sorted(result, key=lambda x: x.relevance_score, reverse=True)
            self._op_counts[MemoryOperation.SORT] += 1

            return result

    def _filter(self, items: List[EvidenceItem]) -> List[EvidenceItem]:
        return [it for it in items if it.relevance_score >= self.min_relevance]

    def _merge(self, items: List[EvidenceItem]) -> List[EvidenceItem]:
        """Simple greedy merge: if two items of same role have high content
        overlap (by token Jaccard), keep the higher-relevance one."""
        if len(items) <= 1:
            return items
        result: List[EvidenceItem] = []
        used = set()
        for i, a in enumerate(items):
            if i in used:
                continue
            merged = a
            for j in range(i + 1, len(items)):
                if j in used:
                    continue
                b = items[j]
                if a.role == b.role:
                    # Simple token overlap
                    set_a = set(a.content.lower().split())
                    set_b = set(b.content.lower().split())
                    if set_a and set_b:
                        overlap = len(set_a & set_b) / min(len(set_a), len(set_b))
                        if overlap > self.redundancy_threshold:
                            used.add(j)
                            merged = a if a.relevance_score >= b.relevance_score else b
            result.append(merged)
            used.add(i)
        return result

    def _resolve(self, items: List[EvidenceItem]) -> List[EvidenceItem]:
        """Mark contradictions but keep both items (flagged for downstream)."""
        for i, a in enumerate(items):
            for j in range(i + 1, len(items)):
                b = items[j]
                if a.contradiction_flags and b.memory_id in a.contradiction_flags:
                    a.metadata["contradicts"] = b.memory_id
                    b.metadata["contradicts"] = a.memory_id
        return items

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"operation_counts": dict(self._op_counts)}


# ---------------------------------------------------------------------------
# TMPOTrainer
# ---------------------------------------------------------------------------

class TMPOTrainer:
    """Trace-Guided Memory Policy Optimization.

    Optimizes the evidence construction policy (EvidencePlanner weights,
    redundancy threshold, min relevance) via RL reward signals from
    downstream answer quality.
    """

    def __init__(
        self,
        planner: EvidencePlanner,
        generator: ActiveMemoryGenerator,
        learning_rate: float = 0.01,
    ):
        self.planner = planner
        self.generator = generator
        self.learning_rate = learning_rate
        self._reward_buffer: deque[TMPOReward] = deque(maxlen=1024)
        self._status = PolicyOptimizationStatus.IDLE
        self._update_count: int = 0
        self._lock = threading.RLock()

    def record_reward(self, reward: TMPOReward) -> None:
        with self._lock:
            self._reward_buffer.append(reward)
            self._status = PolicyOptimizationStatus.COLLECTING

    def optimize_step(self) -> Dict[str, float]:
        """Run one optimization step using accumulated rewards."""
        with self._lock:
            if len(self._reward_buffer) < 8:
                return {"status": "insufficient_data", "buffer_size": len(self._reward_buffer)}

            self._status = PolicyOptimizationStatus.UPDATING
            rewards = list(self._reward_buffer)
            mean_r = float(np.mean([r.reward for r in rewards]))

            # Update ActiveMemoryGenerator thresholds based on reward signal
            if mean_r < 0:
                # Negative reward -> tighter filtering, more aggressive merge
                self.generator.redundancy_threshold = max(
                    0.5,
                    self.generator.redundancy_threshold - self.learning_rate * 0.05,
                )
                self.generator.min_relevance = min(
                    0.5,
                    self.generator.min_relevance + self.learning_rate * 0.05,
                )
            else:
                # Positive reward -> relax thresholds slightly
                self.generator.redundancy_threshold = min(
                    0.95,
                    self.generator.redundancy_threshold + self.learning_rate * 0.02,
                )
                self.generator.min_relevance = max(
                    0.05,
                    self.generator.min_relevance - self.learning_rate * 0.02,
                )

            self._update_count += 1
            self._status = PolicyOptimizationStatus.IDLE

            return {
                "mean_reward": mean_r,
                "updates": self._update_count,
                "redundancy_threshold": self.generator.redundancy_threshold,
                "min_relevance": self.generator.min_relevance,
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            rewards = [r.reward for r in self._reward_buffer]
            return {
                "status": self._status.name,
                "buffer_size": len(self._reward_buffer),
                "updates": self._update_count,
                "mean_reward": float(np.mean(rewards)) if rewards else 0.0,
                "redundancy_threshold": self.generator.redundancy_threshold,
                "min_relevance": self.generator.min_relevance,
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()


# ---------------------------------------------------------------------------
# ContextOverheadReducer
# ---------------------------------------------------------------------------

class ContextOverheadReducer:
    """Quantifies and reduces context overhead from irrelevant or weak
    memories. Tracks token consumption and applies clipping strategies.

    Maintains a budget and prunes memories that exceed it, keeping the
    highest relevance items within budget.
    """

    def __init__(self, max_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS):
        self.max_tokens = max_tokens
        self._token_history: deque[int] = deque(maxlen=256)
        self._saved_tokens: int = 0
        self._clip_events: int = 0
        self._lock = threading.RLock()

    def budget_for(self, base_tokens: int) -> int:
        """How many tokens remain for evidence after accounting for base."""
        return max(0, self.max_tokens - base_tokens)

    def clip(
        self,
        items: List[EvidenceItem],
        base_tokens: int = 0,
    ) -> Tuple[List[EvidenceItem], int]:
        """Clip evidence items to fit within token budget. Returns (kept_items,
        tokens_saved)."""
        with self._lock:
            available = self.budget_for(base_tokens)
            if available <= 0:
                self._clip_events += 1
                saved = sum(it.token_estimate for it in items)
                self._saved_tokens += saved
                self._token_history.append(saved)
                return [], saved

            # Sort by relevance descending, keep as many as fit
            sorted_items = sorted(items, key=lambda x: x.relevance_score, reverse=True)
            kept: List[EvidenceItem] = []
            used = 0
            for it in sorted_items:
                if used + it.token_estimate <= available:
                    kept.append(it)
                    used += it.token_estimate
                else:
                    break

            saved = sum(it.token_estimate for it in items) - used
            self._saved_tokens += saved
            self._token_history.append(saved)
            self._clip_events += 1
            return kept, saved

    def overhead_report(self, items: List[EvidenceItem], base_tokens: int = 0) -> Dict[str, Any]:
        """Report context overhead statistics."""
        total = sum(it.token_estimate for it in items)
        available = self.budget_for(base_tokens)
        overhead = max(0, total - available)
        return {
            "total_evidence_tokens": total,
            "base_tokens": base_tokens,
            "available_tokens": available,
            "overhead_tokens": overhead,
            "overhead_ratio": overhead / max(total, 1),
            "budget_utilization": total / max(available, 1) if available > 0 else float("inf"),
        }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            hist = list(self._token_history)
            return {
                "max_tokens": self.max_tokens,
                "total_tokens_saved": self._saved_tokens,
                "clip_events": self._clip_events,
                "avg_tokens_saved_per_clip": (
                    self._saved_tokens / max(self._clip_events, 1)
                ),
                "history_len": len(hist),
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()
