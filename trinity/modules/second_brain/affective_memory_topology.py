"""
P16-1: Affective Memory Topology.

Reference: REMT — Realtime Editable Memory Topology (Frontiers in AI 2026.03).

Design: A self-referential autobiographical memory graph where every memory
        node carries VAD (Valence/Arousal/Dominance) emotional labels.
        Tracks system-level mood index, synthetic neuroplasticity (edge
        strengthening / decay / pruning), and dynamically aggregates
        identity state from the emotional topology.

Complementary to: theory_of_mind_user_model.py (ToM models the user) —
                  this module models the system's own autobiographical
                  memory and emotional topology.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_MOOD_DECAY_RATE = 0.02       # per-step decay toward neutral
DEFAULT_EDGE_STRENGTHEN_RATE = 0.15  # per-activation boost
DEFAULT_EDGE_DECAY_RATE = 0.008      # per-step decay
DEFAULT_PRUNE_THRESHOLD = 0.05       # remove edges below this weight
DEFAULT_HALF_LIFE_SECONDS = 3600.0   # 1 hour half-life for emotional salience


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EdgeEvent(Enum):
    """Category of event that triggers edge plasticity."""
    EXPLICIT_QUERY = auto()
    IMPLICIT_ASSOCIATION = auto()
    EMOTIONAL_ECHO = auto()
    TEMPORAL_PROXIMITY = auto()
    TOPICAL_SIMILARITY = auto()
    SYSTEM_REFLECTION = auto()


class PrunePolicy(Enum):
    """Policy for neuroplasticity pruning."""
    THRESHOLD = auto()        # prune below fixed weight threshold
    PERCENTILE = auto()       # prune bottom N percentile
    RANK_BASED = auto()       # keep top-K edges per node
    ADAPTIVE = auto()         # dynamic threshold based on graph density


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class VADLabel:
    """Valence-Arousal-Dominance emotion label for a memory node.

    All values in [-1.0, 1.0].
    """
    valence: float = 0.0     # positive (-1) to positive (+1)
    arousal: float = 0.0     # calm (-1) to excited (+1)
    dominance: float = 0.0   # submissive (-1) to dominant (+1)

    def magnitude(self) -> float:
        return float(np.sqrt(self.valence ** 2 + self.arousal ** 2 + self.dominance ** 2))

    def is_high_affect(self, threshold: float = 0.7) -> bool:
        return abs(self.valence) > threshold

    def to_vector(self) -> np.ndarray:
        return np.array([self.valence, self.arousal, self.dominance], dtype=np.float64)


@dataclass
class EmotionalEdge:
    """Weighted edge between two emotionally valenced memory nodes."""
    source_id: str
    target_id: str
    weight: float = 0.5
    activation_count: int = 0
    last_activated: float = field(default_factory=time.monotonic)
    edge_type: EdgeEvent = EdgeEvent.IMPLICIT_ASSOCIATION
    emotional_congruence: float = 0.0  # cosine similarity of VAD vectors


@dataclass
class EmotionallyValencedNode:
    """Memory node with VAD emotional label.

    Each node represents a discrete autobiographical memory event
    with associated timestamp, emotional signature, and content summary.
    """
    node_id: str
    timestamp: float       # Unix epoch seconds
    event_summary: str
    vad_label: VADLabel = field(default_factory=VADLabel)
    tags: List[str] = field(default_factory=list)
    intensity: float = 0.5  # overall emotional intensity
    replay_count: int = 0
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# MoodIndex
# ---------------------------------------------------------------------------

class MoodIndex:
    """Bounded emotional index [-1.0, 1.0] that accumulates affective
    experience across interactions and modulates retrieval bias.

    Implements momentum-based tracking with decay toward neutral baseline.
    Supports per-step and per-event updates with configurable decay rate.
    """

    def __init__(
        self,
        initial: float = 0.0,
        decay_rate: float = DEFAULT_MOOD_DECAY_RATE,
        momentum: float = 0.3,
    ):
        self._value: float = max(-1.0, min(1.0, initial))
        self.decay_rate = decay_rate
        self.momentum = momentum
        self._history: deque[Tuple[float, float]] = deque(maxlen=256)
        self._lock = threading.RLock()

    @property
    def value(self) -> float:
        with self._lock:
            return self._value

    def step(self, delta: float = 0.0) -> float:
        """Apply one timestep: decay toward neutral, then apply delta."""
        with self._lock:
            self._value = self._value * (1.0 - self.decay_rate)
            self._value = self._value + delta * self.momentum
            self._value = max(-1.0, min(1.0, self._value))
            self._history.append((time.time(), self._value))
            return self._value

    def update_from_event(self, vad: VADLabel, weight: float = 1.0) -> float:
        """Update mood from a VAD-labeled memory event."""
        delta = vad.valence * weight
        return self.step(delta)

    def reset(self) -> None:
        with self._lock:
            self._value = 0.0

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            history = list(self._history)
            vals = [h[1] for h in history] if history else [0.0]
            return {
                "current": self._value,
                "mean": float(np.mean(vals)),
                "std": float(np.std(vals)),
                "min": float(np.min(vals)),
                "max": float(np.max(vals)),
                "sample_count": len(vals),
            }

    def snapshot(self) -> Dict[str, Any]:
        return {"mood_index": self._value, **self.statistics()}


# ---------------------------------------------------------------------------
# SyntheticNeuroplasticity
# ---------------------------------------------------------------------------

class SyntheticNeuroplasticity:
    """Synthetic neuroplasticity engine — edge strengthening, decay,
    and pruning for the emotional memory graph.

    - Strengthening: edges are reinforced on repeated co-activation.
    - Decay: all edges decay exponentially toward zero each step.
    - Pruning: edges below threshold are removed to maintain sparsity.

    Uses Hebbian-inspired "fire together, wire together" with emotional
    congruence bonuses.
    """

    def __init__(
        self,
        strengthen_rate: float = DEFAULT_EDGE_STRENGTHEN_RATE,
        decay_rate: float = DEFAULT_EDGE_DECAY_RATE,
        prune_threshold: float = DEFAULT_PRUNE_THRESHOLD,
        prune_policy: PrunePolicy = PrunePolicy.THRESHOLD,
        half_life: float = DEFAULT_HALF_LIFE_SECONDS,
    ):
        self.strengthen_rate = strengthen_rate
        self.decay_rate = decay_rate
        self.prune_threshold = prune_threshold
        self.prune_policy = prune_policy
        self.half_life = half_life
        self._edges: Dict[str, EmotionalEdge] = {}
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"strengthened": 0, "decayed": 0, "pruned": 0}

    def edge_key(self, src: str, tgt: str) -> str:
        return f"{src}||{tgt}"

    def get_edge(self, src: str, tgt: str) -> Optional[EmotionalEdge]:
        with self._lock:
            return self._edges.get(self.edge_key(src, tgt))

    def strengthen(
        self,
        src: str,
        tgt: str,
        event: EdgeEvent = EdgeEvent.IMPLICIT_ASSOCIATION,
        emotional_congruence: float = 0.0,
    ) -> EmotionalEdge:
        """Strengthen (or create) an edge between two nodes."""
        key = self.edge_key(src, tgt)
        with self._lock:
            if key in self._edges:
                edge = self._edges[key]
                edge.activation_count += 1
                edge.last_activated = time.monotonic()
                edge.weight = min(1.0, edge.weight + self.strengthen_rate * (1.0 - edge.weight))
                edge.edge_type = event
                edge.emotional_congruence = max(edge.emotional_congruence, emotional_congruence)
            else:
                edge = EmotionalEdge(
                    source_id=src,
                    target_id=tgt,
                    weight=self.strengthen_rate,
                    activation_count=1,
                    edge_type=event,
                    emotional_congruence=emotional_congruence,
                )
                self._edges[key] = edge
            self._stats["strengthened"] += 1
            return edge

    def decay_all(self) -> int:
        """Apply exponential decay to all edges. Returns count of decayed edges."""
        with self._lock:
            for edge in self._edges.values():
                edge.weight *= (1.0 - self.decay_rate)
            self._stats["decayed"] += len(self._edges)
            return len(self._edges)

    def prune(self) -> List[EmotionalEdge]:
        """Remove edges below prune threshold. Returns list of pruned edges."""
        removed: List[EmotionalEdge] = []
        with self._lock:
            if self.prune_policy == PrunePolicy.THRESHOLD:
                to_remove = [
                    k for k, e in self._edges.items()
                    if e.weight < self.prune_threshold
                ]
            elif self.prune_policy == PrunePolicy.RANK_BASED:
                # Keep top edges per node; prune weak ones
                node_edges: Dict[str, List[Tuple[str, EmotionalEdge]]] = defaultdict(list)
                for k, e in self._edges.items():
                    node_edges[e.source_id].append((k, e))
                to_remove = []
                for _node, edges in node_edges.items():
                    if len(edges) > 20:
                        edges.sort(key=lambda x: x[1].weight)
                        to_remove.extend(k for k, _e in edges[: len(edges) - 20])
            else:
                to_remove = [
                    k for k, e in self._edges.items()
                    if e.weight < self.prune_threshold
                ]

            for k in to_remove:
                removed.append(self._edges.pop(k))
            self._stats["pruned"] += len(removed)
        logger.debug("Neuroplasticity pruned %d edges", len(removed))
        return removed

    def edge_count(self) -> int:
        with self._lock:
            return len(self._edges)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            weights = [e.weight for e in self._edges.values()]
            return {
                "total_edges": len(self._edges),
                "mean_weight": float(np.mean(weights)) if weights else 0.0,
                "max_weight": float(np.max(weights)) if weights else 0.0,
                "min_weight": float(np.min(weights)) if weights else 0.0,
                **self._stats,
            }


# ---------------------------------------------------------------------------
# AutobiographicalMemoryGraph
# ---------------------------------------------------------------------------

class AutobiographicalMemoryGraph:
    """Self-referential autobiographical memory graph.

    Organizes personal experience nodes on a timeline with emotional labels.
    Each node contains timestamp, VAD emotional tags, and an event summary.
    Supports temporal queries, emotional filtering, and timeline traversal.
    """

    def __init__(self, max_nodes: int = 10_000):
        self.max_nodes = max_nodes
        self._nodes: Dict[str, EmotionallyValencedNode] = {}
        self._timeline: List[str] = []       # node IDs in chronological order
        self.plasticity = SyntheticNeuroplasticity()
        self._lock = threading.RLock()

    def add_node(self, node: EmotionallyValencedNode) -> EmotionallyValencedNode:
        """Insert a new autobiographical memory node."""
        with self._lock:
            if len(self._nodes) >= self.max_nodes:
                oldest = self._timeline.pop(0)
                self._remove_node_edges(oldest)
                self._nodes.pop(oldest, None)
            self._nodes[node.node_id] = node
            # Insert in chronological order
            idx = 0
            for i, nid in enumerate(self._timeline):
                if self._nodes[nid].timestamp > node.timestamp:
                    break
                idx = i + 1
            self._timeline.insert(idx, node.node_id)
            # Auto-link to temporally adjacent nodes
            if idx > 0:
                prev_id = self._timeline[idx - 1]
                prev_vad = self._nodes[prev_id].vad_label
                congruence = 1.0 - np.linalg.norm(
                    node.vad_label.to_vector() - prev_vad.to_vector()
                ) / (2.0 * np.sqrt(3))
                self.plasticity.strengthen(
                    prev_id, node.node_id,
                    EdgeEvent.TEMPORAL_PROXIMITY,
                    emotional_congruence=float(congruence),
                )
            return node

    def get_node(self, node_id: str) -> Optional[EmotionallyValencedNode]:
        with self._lock:
            return self._nodes.get(node_id)

    def query_timeline(
        self,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        limit: int = 100,
    ) -> List[EmotionallyValencedNode]:
        """Retrieve nodes within a time window."""
        with self._lock:
            result = []
            for nid in self._timeline:
                node = self._nodes[nid]
                if start_time is not None and node.timestamp < start_time:
                    continue
                if end_time is not None and node.timestamp > end_time:
                    break
                result.append(node)
                if len(result) >= limit:
                    break
            return result

    def filter_by_emotion(
        self,
        min_valence: Optional[float] = None,
        max_valence: Optional[float] = None,
        min_arousal: Optional[float] = None,
        limit: int = 100,
    ) -> List[EmotionallyValencedNode]:
        """Filter nodes by VAD emotional ranges."""
        with self._lock:
            result = []
            for nid in self._timeline:
                node = self._nodes[nid]
                v = node.vad_label
                if min_valence is not None and v.valence < min_valence:
                    continue
                if max_valence is not None and v.valence > max_valence:
                    continue
                if min_arousal is not None and v.arousal < min_arousal:
                    continue
                result.append(node)
                if len(result) >= limit:
                    break
            return result

    def step_plasticity(self) -> None:
        """Run one cycle of neuroplasticity (decay + prune)."""
        self.plasticity.decay_all()
        self.plasticity.prune()

    def _remove_node_edges(self, node_id: str) -> None:
        keys = list(self.plasticity._edges.keys())
        for k in keys:
            if k.startswith(f"{node_id}||") or k.endswith(f"||{node_id}"):
                self.plasticity._edges.pop(k, None)

    def node_count(self) -> int:
        with self._lock:
            return len(self._nodes)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self._nodes),
                "timeline_span_days": (
                    (self._nodes[self._timeline[-1]].timestamp -
                     self._nodes[self._timeline[0]].timestamp) / 86400.0
                ) if len(self._timeline) >= 2 else 0.0,
                "plasticity": self.plasticity.statistics(),
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()


# ---------------------------------------------------------------------------
# DynamicIdentityState
# ---------------------------------------------------------------------------

class DynamicIdentityState:
    """Dynamic identity state aggregated from the autobiographical memory
    graph. Evolves with emotional topology — synthesizes a coherent
    self-representation from past experiences weighted by recency and
    emotional salience.

    Produces: a vector identity profile, trait summaries, and temporal
    self-continuity estimates.
    """

    def __init__(self, graph: AutobiographicalMemoryGraph, dim: int = 128):
        self.graph = graph
        self.dim = dim
        self._identity_vector: np.ndarray = np.zeros(dim, dtype=np.float64)
        self._trait_scores: Dict[str, float] = {}
        self._last_update: float = 0.0
        self._lock = threading.RLock()
        # Default trait dimensions
        self.trait_names: List[str] = [
            "openness", "conscientiousness", "extraversion",
            "agreeableness", "neuroticism", "curiosity",
            "resilience", "adaptability", "reflectiveness",
        ]

    def recompute(self) -> np.ndarray:
        """Recompute identity state from the full memory graph.

        Weights recent and high-affect nodes more heavily.
        """
        with self._lock:
            now = time.time()
            nodes = list(self.graph._nodes.values())
            if not nodes:
                self._identity_vector = np.zeros(self.dim)
                return self._identity_vector

            total_weight = 0.0
            vector = np.zeros(self.dim)

            for node in nodes:
                age = now - node.timestamp
                recency = math.exp(-age / (7.0 * 86400.0))  # 7-day half-life
                affect = node.vad_label.magnitude()
                weight = 0.5 * recency + 0.5 * affect
                seed = hash(node.node_id) % (2 ** 31)
                rng = np.random.default_rng(seed)
                node_vec = rng.normal(0, 0.1, self.dim)
                vector += weight * node_vec
                total_weight += weight

            if total_weight > 0:
                self._identity_vector = vector / total_weight
            else:
                self._identity_vector = np.zeros(self.dim)

            self._last_update = now
            self._update_traits()
            return self._identity_vector

    def _update_traits(self) -> None:
        """Infer trait scores from aggregated emotional signature of all
        nodes in the graph."""
        nodes = list(self.graph._nodes.values())
        if not nodes:
            return
        avg_valence = float(np.mean([n.vad_label.valence for n in nodes]))
        avg_arousal = float(np.mean([n.vad_label.arousal for n in nodes]))
        avg_dominance = float(np.mean([n.vad_label.dominance for n in nodes]))

        self._trait_scores = {
            "openness": min(1.0, max(0.0, 0.5 + avg_arousal * 0.3)),
            "conscientiousness": min(1.0, max(0.0, 0.5 + avg_dominance * 0.3)),
            "extraversion": min(1.0, max(0.0, 0.5 + avg_valence * 0.3)),
            "agreeableness": min(1.0, max(0.0, 0.5 + avg_valence * 0.4 - avg_dominance * 0.2)),
            "neuroticism": min(1.0, max(0.0, 0.5 - avg_valence * 0.3 + avg_arousal * 0.2)),
            "curiosity": min(1.0, max(0.0, 0.5 + avg_arousal * 0.4)),
            "resilience": min(1.0, max(0.0, 0.5 + avg_dominance * 0.3 + avg_valence * 0.2)),
            "adaptability": min(1.0, max(0.0, 0.5 + avg_arousal * 0.2 - abs(avg_valence) * 0.3)),
            "reflectiveness": min(1.0, max(0.0, 0.5 + avg_arousal * 0.15 + avg_dominance * 0.15)),
        }

    @property
    def identity_vector(self) -> np.ndarray:
        with self._lock:
            return self._identity_vector.copy()

    @property
    def trait_scores(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._trait_scores)

    def cosine_similarity(self, past_vector: np.ndarray) -> float:
        """Measure self-continuity between current and past identity."""
        with self._lock:
            norm = np.linalg.norm(self._identity_vector) * np.linalg.norm(past_vector)
            if norm == 0:
                return 0.0
            return float(np.dot(self._identity_vector, past_vector) / norm)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "trait_scores": dict(self._trait_scores),
                "identity_norm": float(np.linalg.norm(self._identity_vector)),
                "last_update": self._last_update,
                "graph_node_count": self.graph.node_count(),
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()
