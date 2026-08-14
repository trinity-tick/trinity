"""
P16-3: Serendipity Retrieval Engine.

Reference: Retrieval Paradox — more precise retrieval reduces serendipitous
           discovery. Counterbalances precision with exploration.

Design: Three retrieval modes — Query (high precision), Wander (high recall /
        random sampling), Surface (background spontaneous recall). Noise
        budget allocator enforces 80/20 explore-exploit. Associative bridging
        traverses weak connections (co-occurrence / temporal proximity /
        emotional resonance) for unexpected discovery.

Complementary to: graph_router.py (router does efficient precise routing) —
                  this module does serendipitous / exploratory retrieval.
"""

from __future__ import annotations

import logging
import math
import random
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

DEFAULT_NOISE_BUDGET = 0.20          # 20% noise budget
DEFAULT_WANDER_TEMPERATURE = 1.2     # temperature for wander sampling
DEFAULT_SURFACE_INTERVAL_SEC = 300.0  # surface mode cycle interval
DEFAULT_BRIDGE_HOP_LIMIT = 3


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RetrievalMode(Enum):
    """Operational mode of serendipity retrieval."""
    QUERY = auto()     # high precision, user-directed
    WANDER = auto()    # high recall, random exploration
    SURFACE = auto()   # background spontaneous recall


class BridgeType(Enum):
    """Type of associative bridge between memories."""
    CO_OCCURRENCE = auto()       # appeared in same interaction
    TEMPORAL_PROXIMITY = auto()  # close in time
    EMOTIONAL_RESONANCE = auto() # similar VAD emotional signature
    TOPICAL_ADJACENCY = auto()   # semantically adjacent topics
    RANDOM_JUMP = auto()         # stochastic connection


class NoiseAllocation(Enum):
    """How noise budget is spent."""
    WANDER_SAMPLE = auto()
    TANGENTIAL_QUERY = auto()
    RANDOM_WALK = auto()
    ASSOCIATIVE_JUMP = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RetrievalHit:
    """A single retrieval result with serendipity metadata."""
    memory_id: str
    content: str
    relevance: float
    mode: RetrievalMode = RetrievalMode.QUERY
    bridge_path: Optional[List[str]] = None  # IDs traversed to reach this
    bridge_type: Optional[BridgeType] = None
    serendipity_score: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class BridgeEdge:
    """Weak associative edge used for serendipity jumps."""
    source_id: str
    target_id: str
    bridge_type: BridgeType
    strength: float = 0.1  # deliberately weak
    discovery_count: int = 0


# ---------------------------------------------------------------------------
# QueryRetriever
# ---------------------------------------------------------------------------

class QueryRetriever:
    """High-precision mode: when the user explicitly knows what they want.
    Returns top-K highest relevance hits. Standard exploit mode."""

    def __init__(self, default_top_k: int = 10):
        self.default_top_k = default_top_k
        self._query_count: int = 0
        self._lock = threading.RLock()

    def retrieve(
        self,
        query: str,
        candidates: List[RetrievalHit],
        top_k: Optional[int] = None,
    ) -> List[RetrievalHit]:
        """Return top-K most relevant hits."""
        k = top_k or self.default_top_k
        with self._lock:
            self._query_count += 1
            sorted_hits = sorted(candidates, key=lambda h: h.relevance, reverse=True)
            for h in sorted_hits[:k]:
                h.mode = RetrievalMode.QUERY
            return sorted_hits[:k]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"query_count": self._query_count}


# ---------------------------------------------------------------------------
# WanderRetriever
# ---------------------------------------------------------------------------

class WanderRetriever:
    """Low precision / high recall / random sampling mode.

    When the user doesn't know exactly what to look for, wander mode
    applies temperature-scaled softmax sampling over all candidates
    to trigger unexpected associations.
    """

    def __init__(
        self,
        temperature: float = DEFAULT_WANDER_TEMPERATURE,
        sample_count: int = 5,
    ):
        self.temperature = temperature
        self.sample_count = sample_count
        self._wander_count: int = 0
        self._lock = threading.RLock()

    def wander(self, candidates: List[RetrievalHit]) -> List[RetrievalHit]:
        """Temperature-scaled random sampling from candidates."""
        with self._lock:
            self._wander_count += 1
            if not candidates:
                return []

            n = min(self.sample_count, len(candidates))
            rels = np.array([h.relevance for h in candidates], dtype=np.float64)
            # Apply temperature scaling
            if self.temperature > 0:
                probs = np.exp(rels / self.temperature)
                probs = probs / probs.sum()
            else:
                probs = np.ones(len(candidates)) / len(candidates)

            chosen_indices = np.random.choice(
                len(candidates), size=n, replace=False, p=probs,
            )
            result = []
            for idx in chosen_indices:
                hit = candidates[idx]
                hit.mode = RetrievalMode.WANDER
                hit.serendipity_score = 1.0 - hit.relevance  # low relevance = high serendipity
                result.append(hit)
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"wander_count": self._wander_count, "temperature": self.temperature}


# ---------------------------------------------------------------------------
# SurfaceRetriever
# ---------------------------------------------------------------------------

class SurfaceRetriever:
    """Non-volitional / background recall mode.

    Periodically surfaces memories that are potentially relevant but haven't
    been explicitly queried — "memories that find you." Uses a background
    cycle that checks for dormant high-significance memories.
    """

    def __init__(self, cycle_interval: float = DEFAULT_SURFACE_INTERVAL_SEC):
        self.cycle_interval = cycle_interval
        self._last_cycle: float = 0.0
        self._surface_count: int = 0
        self._surfaced_history: deque[str] = deque(maxlen=128)
        self._lock = threading.RLock()

    def should_cycle(self) -> bool:
        with self._lock:
            return (time.monotonic() - self._last_cycle) >= self.cycle_interval

    def surface(
        self,
        candidates: List[RetrievalHit],
        top_k: int = 3,
    ) -> List[RetrievalHit]:
        """Surface dormant but significant memories.

        Selection is biased toward memories that:
        - Have not been surfaced recently
        - Have moderate relevance (not too high = already known, not too low = noise)
        - Carry high emotional significance markers in metadata
        """
        with self._lock:
            self._surface_count += 1
            self._last_cycle = time.monotonic()

            if not candidates:
                return []

            # Prefer candidates not recently surfaced
            fresh = [
                h for h in candidates
                if h.memory_id not in self._surfaced_history
            ]
            if not fresh:
                fresh = list(candidates)

            # Bias toward moderate relevance + emotional significance
            def surface_score(h: RetrievalHit) -> float:
                mid_relevance = 1.0 - abs(h.relevance - 0.4)  # peak at 0.4
                emotional = h.metadata.get("emotional_significance", 0.5)
                return 0.6 * mid_relevance + 0.4 * emotional

            scored = sorted(fresh, key=surface_score, reverse=True)
            result = scored[:min(top_k, len(scored))]
            for h in result:
                h.mode = RetrievalMode.SURFACE
                h.serendipity_score = surface_score(h)
                self._surfaced_history.append(h.memory_id)
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "surface_count": self._surface_count,
                "cycle_interval": self.cycle_interval,
                "surfaced_history_len": len(self._surfaced_history),
            }


# ---------------------------------------------------------------------------
# NoiseBudgetAllocator
# ---------------------------------------------------------------------------

class NoiseBudgetAllocator:
    """80/20 noise budget — 80% focused retrieval, 20% random/tangential
    sampling. Simulates explore-exploit tradeoff.

    Maintains a running budget and allocates noise tokens across
    wander samples, tangential queries, random walks, and associative jumps.
    """

    def __init__(self, noise_ratio: float = DEFAULT_NOISE_BUDGET):
        self.noise_ratio = noise_ratio
        self._budget_remaining: float = noise_ratio
        self._allocation_history: deque[Dict[str, Any]] = deque(maxlen=128)
        self._lock = threading.RLock()

    def allocate(
        self,
        total_candidates: int,
    ) -> Dict[NoiseAllocation, int]:
        """Decide how many noise samples to draw across allocation types."""
        with self._lock:
            budget_slots = max(1, int(total_candidates * self.noise_ratio))
            self._budget_remaining = self.noise_ratio

            allocation = {
                NoiseAllocation.WANDER_SAMPLE: max(1, budget_slots // 2),
                NoiseAllocation.ASSOCIATIVE_JUMP: max(1, budget_slots // 4),
                NoiseAllocation.RANDOM_WALK: max(0, budget_slots // 8),
                NoiseAllocation.TANGENTIAL_QUERY: max(0, budget_slots // 8),
            }
            self._allocation_history.append({
                "total_candidates": total_candidates,
                "budget_slots": budget_slots,
                "allocation": {k.name: v for k, v in allocation.items()},
                "time": time.time(),
            })
            return allocation

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"noise_ratio": self.noise_ratio, "history_len": len(self._allocation_history)}

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()


# ---------------------------------------------------------------------------
# AssociativeBridging
# ---------------------------------------------------------------------------

class AssociativeBridging:
    """Associative bridging — traverses weak connections (co-occurrence,
    temporal proximity, emotional resonance) to jump from a current query
    to unexpectedly related memories.

    Builds and maintains a sparse graph of weak bridge edges.
    """

    def __init__(self, max_hops: int = DEFAULT_BRIDGE_HOP_LIMIT):
        self.max_hops = max_hops
        self._edges: Dict[str, Dict[str, BridgeEdge]] = defaultdict(dict)
        self._bridge_count: int = 0
        self._lock = threading.RLock()

    def add_bridge(
        self,
        src: str,
        tgt: str,
        bridge_type: BridgeType,
        strength: float = 0.1,
    ) -> BridgeEdge:
        """Add or strengthen a weak associative bridge."""
        key = f"{src}||{tgt}"
        with self._lock:
            if tgt in self._edges.get(src, {}):
                edge = self._edges[src][tgt]
                edge.discovery_count += 1
                edge.strength = min(0.5, edge.strength + strength * 0.1)
                return edge
            edge = BridgeEdge(
                source_id=src,
                target_id=tgt,
                bridge_type=bridge_type,
                strength=strength,
                discovery_count=1,
            )
            self._edges[src][tgt] = edge
            # Symmetric for undirected traversal
            self._edges[tgt][src] = BridgeEdge(
                source_id=tgt,
                target_id=src,
                bridge_type=bridge_type,
                strength=strength,
                discovery_count=1,
            )
            return edge

    def traverse(
        self,
        start_id: str,
        node_map: Dict[str, RetrievalHit],
        max_results: int = 5,
    ) -> List[RetrievalHit]:
        """Multi-hop traversal from start_id following bridge edges.
        Uses BFS with cumulative discovery score that decays with hops.
        """
        with self._lock:
            self._bridge_count += 1
            discovered: Dict[str, float] = {}  # node_id -> cumulative score
            visited = {start_id}
            frontier = deque([(start_id, 0, 1.0)])  # (node_id, hops, score)

            while frontier:
                current, hops, score = frontier.popleft()
                if hops >= self.max_hops:
                    continue
                for neighbor_id, edge in self._edges.get(current, {}).items():
                    if neighbor_id in visited:
                        continue
                    visited.add(neighbor_id)
                    new_score = score * edge.strength * (0.7 ** hops)
                    if neighbor_id in node_map:
                        discovered[neighbor_id] = discovered.get(neighbor_id, 0) + new_score
                    frontier.append((neighbor_id, hops + 1, new_score))

            # Return top discovered nodes as RetrievalHits
            result = []
            for nid, score in sorted(discovered.items(), key=lambda x: -x[1]):
                if nid in node_map:
                    hit = node_map[nid]
                    hit.bridge_path = list(visited)
                    hit.bridge_type = self._edges.get(start_id, {}).get(nid, BridgeEdge(
                        "", "", BridgeType.RANDOM_JUMP,
                    )).bridge_type
                    hit.serendipity_score = min(1.0, score)
                    result.append(hit)
                if len(result) >= max_results:
                    break
            return result

    def edge_count(self) -> int:
        with self._lock:
            return sum(len(targets) for targets in self._edges.values()) // 2

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "bridge_count": self._bridge_count,
                "edge_count": self.edge_count(),
                "max_hops": self.max_hops,
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()
