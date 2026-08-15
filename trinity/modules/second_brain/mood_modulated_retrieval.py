"""
# status: orphan (2026-08-15 audit, not in runtime path)
P16-4: Mood-Modulated Retrieval.

Reference: REMT MoodIndex × Retrieval Paradox — retrieval mode switching
           modulated by system emotional state.

Design: Estimates current mood state from recent interaction sequence
        (valence/arousal dual-axis). Biases retrieval radius based on mood
        (positive = exploration, negative = safe/focused). Emotional
        salience decay differentiates high-affect events (slow decay) from
        mundane ones (fast decay). Contextualizes results with emotional
        formation metadata. Ranks retrieval to prefer mood-congruent results.

Complementary to: affective_memory_topology.py (topology handles storage-end
                  emotional structure) — this module handles retrieval-end
                  mood modulation.
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

DEFAULT_RECENT_WINDOW = 20           # recent interactions for mood estimation
DEFAULT_HIGH_AFFECT_THRESHOLD = 0.7  # |valence| above this = high affect
DEFAULT_HIGH_AFFECT_HALF_LIFE = 86400.0 * 7   # 7 days
DEFAULT_LOW_AFFECT_HALF_LIFE = 86400.0 * 0.5  # 12 hours
DEFAULT_MOOD_RADIUS_ALPHA_BASE = 1.0
DEFAULT_MOOD_EXPANSION = 0.5         # extra search radius per mood unit
DEFAULT_MOOD_CONTRACTION = 0.3       # radius shrink per negative mood unit


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MoodZone(Enum):
    """Quadrant in valence-arousal space."""
    HIGH_POSITIVE = auto()   # excited, joyful
    LOW_POSITIVE = auto()    # calm, content
    HIGH_NEGATIVE = auto()   # anxious, angry
    LOW_NEGATIVE = auto()    # sad, withdrawn
    NEUTRAL = auto()


class RetrievalBias(Enum):
    """Retrieval behavior bias based on mood."""
    EXPLORATORY = auto()   # broad search, novel connections
    FOCUSED = auto()       # narrow search, safe results
    BALANCED = auto()      # standard retrieval
    NOSTALGIC = auto()     # biased toward past positive memories


class SalienceTier(Enum):
    """Emotional salience tier for decay curve."""
    HIGH_AFFECT = auto()    # |valence| > 0.7, slow decay
    MODERATE = auto()       # 0.3 < |valence| <= 0.7
    MUNDANE = auto()        # |valence| <= 0.3, fast decay


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InteractionSnapshot:
    """Snapshot of a single interaction for mood estimation."""
    timestamp: float
    valence: float         # inferred valence from interaction
    arousal: float         # inferred arousal from interaction
    text_fragment: str = ""
    weight: float = 1.0


@dataclass
class MoodState:
    """Estimated mood state in valence-arousal space."""
    valence: float = 0.0      # [-1, 1]
    arousal: float = 0.0      # [-1, 1]
    confidence: float = 0.5   # estimation confidence
    zone: MoodZone = MoodZone.NEUTRAL
    bias: RetrievalBias = RetrievalBias.BALANCED
    timestamp: float = field(default_factory=time.time)

    def to_vector(self) -> np.ndarray:
        return np.array([self.valence, self.arousal], dtype=np.float64)

    def magnitude(self) -> float:
        return float(np.sqrt(self.valence ** 2 + self.arousal ** 2))


@dataclass
class MoodAwareHit:
    """Retrieval hit with mood-related metadata."""
    memory_id: str
    content: str
    relevance: float
    formation_valence: float = 0.0  # valence at memory formation time
    formation_arousal: float = 0.0  # arousal at memory formation time
    mood_congruence: float = 0.0    # how well this matches current mood
    salience_tier: SalienceTier = SalienceTier.MUNDANE
    decayed_weight: float = 1.0     # after emotional salience decay
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# MoodStateEstimator
# ---------------------------------------------------------------------------

class MoodStateEstimator:
    """Estimates current mood state from the most recent N interaction
    snapshots. Uses exponential moving average over valence and arousal
    with recency weighting."""

    def __init__(self, window_size: int = DEFAULT_RECENT_WINDOW):
        self.window_size = window_size
        self._interactions: deque[InteractionSnapshot] = deque(maxlen=window_size)
        self._current: MoodState = MoodState()
        self._lock = threading.RLock()

    def record(self, snapshot: InteractionSnapshot) -> None:
        """Add an interaction snapshot and update mood estimate."""
        with self._lock:
            self._interactions.append(snapshot)
            self._update()

    def _update(self) -> None:
        if not self._interactions:
            self._current = MoodState()
            return

        now = time.time()
        total_weight = 0.0
        weighted_valence = 0.0
        weighted_arousal = 0.0

        for snap in self._interactions:
            age = now - snap.timestamp
            recency = math.exp(-age / 3600.0)  # 1-hour half-life for recency
            w = snap.weight * recency
            weighted_valence += snap.valence * w
            weighted_arousal += snap.arousal * w
            total_weight += w

        if total_weight > 0:
            valence = weighted_valence / total_weight
            arousal = weighted_arousal / total_weight
        else:
            valence, arousal = 0.0, 0.0

        valence = max(-1.0, min(1.0, valence))
        arousal = max(-1.0, min(1.0, arousal))

        zone = self._classify_zone(valence, arousal)
        bias = self._classify_bias(valence, arousal)

        self._current = MoodState(
            valence=valence,
            arousal=arousal,
            confidence=min(1.0, len(self._interactions) / self.window_size),
            zone=zone,
            bias=bias,
        )

    def _classify_zone(self, valence: float, arousal: float) -> MoodZone:
        if abs(valence) < 0.2 and abs(arousal) < 0.2:
            return MoodZone.NEUTRAL
        if valence > 0.2:
            return MoodZone.HIGH_POSITIVE if arousal > 0.3 else MoodZone.LOW_POSITIVE
        if valence < -0.2:
            return MoodZone.HIGH_NEGATIVE if arousal > 0.3 else MoodZone.LOW_NEGATIVE
        return MoodZone.NEUTRAL

    def _classify_bias(self, valence: float, arousal: float) -> RetrievalBias:
        if valence > 0.3:
            return RetrievalBias.EXPLORATORY
        elif valence < -0.3:
            return RetrievalBias.FOCUSED
        elif valence < 0 and arousal < -0.2:
            return RetrievalBias.NOSTALGIC
        return RetrievalBias.BALANCED

    @property
    def current(self) -> MoodState:
        with self._lock:
            return self._current

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "valence": self._current.valence,
                "arousal": self._current.arousal,
                "zone": self._current.zone.name,
                "bias": self._current.bias.name,
                "confidence": self._current.confidence,
                "interaction_count": len(self._interactions),
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()


# ---------------------------------------------------------------------------
# MoodBiasedRetriever
# ---------------------------------------------------------------------------

class MoodBiasedRetriever:
    """Retrieval biased by mood state.

    Positive mood (valence > 0): expand search radius (alpha > 1), more exploration.
    Negative mood (valence < 0): shrink search radius (alpha < 1), safety-first.
    High arousal: increase sampling temperature for diversity.
    Low arousal: conservative retrieval.
    """

    def __init__(
        self,
        base_radius: float = DEFAULT_MOOD_RADIUS_ALPHA_BASE,
        expansion: float = DEFAULT_MOOD_EXPANSION,
        contraction: float = DEFAULT_MOOD_CONTRACTION,
    ):
        self.base_radius = base_radius
        self.expansion = expansion
        self.contraction = contraction
        self._lock = threading.RLock()

    def compute_radius(self, mood: MoodState) -> float:
        """Compute search radius multiplier based on current mood.

        alpha > 1: broader search (positive/exploratory mood)
        alpha < 1: narrower search (negative/focused mood)
        """
        with self._lock:
            if mood.valence > 0:
                alpha = self.base_radius + self.expansion * mood.valence
            else:
                alpha = self.base_radius - self.contraction * abs(mood.valence)
            return max(0.3, min(2.0, alpha))

    def compute_temperature(self, mood: MoodState) -> float:
        """Compute sampling temperature from arousal level."""
        return 0.5 + mood.arousal * 1.0

    def bias_scores(
        self,
        hits: List[MoodAwareHit],
        mood: MoodState,
    ) -> List[MoodAwareHit]:
        """Adjust hit scores based on mood congruence."""
        with self._lock:
            radius = self.compute_radius(mood)
            for hit in hits:
                # Mood-congruent bonus: higher score if formation mood matches current
                hit.mood_congruence = 1.0 - abs(hit.formation_valence - mood.valence) / 2.0
                # Adjust relevance with mood radius and congruence
                adjusted = hit.relevance * radius * (0.7 + 0.3 * hit.mood_congruence)
                hit.relevance = min(1.0, adjusted)
            return hits

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "base_radius": self.base_radius,
                "expansion": self.expansion,
                "contraction": self.contraction,
            }

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()


# ---------------------------------------------------------------------------
# EmotionalSalienceDecay
# ---------------------------------------------------------------------------

class EmotionalSalienceDecay:
    """Emotional salience decay curve.

    High-affect events (|valence| > 0.7) have long half-lives (slow decay),
    ensuring emotionally significant memories persist longer.
    Mundane events decay rapidly.

    The decay function: weight(t) = exp(-t * ln(2) / half_life)
    """

    def __init__(
        self,
        high_affect_half_life: float = DEFAULT_HIGH_AFFECT_HALF_LIFE,
        low_affect_half_life: float = DEFAULT_LOW_AFFECT_HALF_LIFE,
        threshold: float = DEFAULT_HIGH_AFFECT_THRESHOLD,
    ):
        self.high_affect_half_life = high_affect_half_life
        self.low_affect_half_life = low_affect_half_life
        self.threshold = threshold
        self._lock = threading.RLock()

    def classify(self, valence: float) -> SalienceTier:
        av = abs(valence)
        if av > self.threshold:
            return SalienceTier.HIGH_AFFECT
        elif av > 0.3:
            return SalienceTier.MODERATE
        return SalienceTier.MUNDANE

    def half_life_for(self, tier: SalienceTier) -> float:
        if tier == SalienceTier.HIGH_AFFECT:
            return self.high_affect_half_life
        elif tier == SalienceTier.MODERATE:
            return (self.high_affect_half_life + self.low_affect_half_life) / 2.0
        return self.low_affect_half_life

    def compute_weight(
        self,
        valence: float,
        age_seconds: float,
    ) -> Tuple[float, SalienceTier]:
        """Compute decayed weight based on formation valence and age.

        Returns (weight, salience_tier).
        """
        with self._lock:
            tier = self.classify(valence)
            half_life = self.half_life_for(tier)
            if half_life <= 0:
                return 1.0, tier
            weight = math.exp(-age_seconds * math.log(2) / half_life)
            return weight, tier

    def decay_hits(self, hits: List[MoodAwareHit], now: float) -> List[MoodAwareHit]:
        """Apply decay to a list of hits based on their formation time."""
        for hit in hits:
            # formation timestamp not directly in MoodAwareHit; use metadata
            age = hit.metadata.get("age_seconds", 0.0)
            weight, tier = self.compute_weight(hit.formation_valence, age)
            hit.decayed_weight = weight
            hit.salience_tier = tier
            hit.relevance *= weight
        return hits

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "high_affect_half_life_hours": self.high_affect_half_life / 3600.0,
                "low_affect_half_life_hours": self.low_affect_half_life / 3600.0,
                "threshold": self.threshold,
            }


# ---------------------------------------------------------------------------
# AffectiveContextualizer
# ---------------------------------------------------------------------------

class AffectiveContextualizer:
    """Attaches emotional formation metadata to each retrieval result.

    For each retrieved memory, annotates "under what emotional context
    this memory was formed" — enabling downstream systems to interpret
    results through an emotional lens.
    """

    def __init__(self):
        self._context_cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def register_context(
        self,
        memory_id: str,
        formation_valence: float,
        formation_arousal: float,
        mood_zone: str = "NEUTRAL",
        tags: Optional[List[str]] = None,
    ) -> None:
        with self._lock:
            self._context_cache[memory_id] = {
                "formation_valence": formation_valence,
                "formation_arousal": formation_arousal,
                "mood_zone": mood_zone,
                "tags": tags or [],
                "registered_at": time.time(),
            }

    def contextualize(self, hits: List[MoodAwareHit]) -> List[MoodAwareHit]:
        """Annotate hits with formation emotional context."""
        with self._lock:
            for hit in hits:
                ctx = self._context_cache.get(hit.memory_id, {})
                hit.formation_valence = ctx.get("formation_valence", 0.0)
                hit.formation_arousal = ctx.get("formation_arousal", 0.0)
                hit.metadata["mood_zone_at_formation"] = ctx.get("mood_zone", "UNKNOWN")
                hit.metadata["formation_tags"] = ctx.get("tags", [])
            return hits

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"cached_contexts": len(self._context_cache)}

    def snapshot(self) -> Dict[str, Any]:
        return self.statistics()


# ---------------------------------------------------------------------------
# MoodAwareRanker
# ---------------------------------------------------------------------------

class MoodAwareRanker:
    """Ranks retrieval results with mood awareness.

    When the system is in a particular mood state, this ranker boosts
    results whose formation mood is emotionally congruent with the
    current state — mood-congruent memory bias.
    """

    def __init__(self, congruence_weight: float = 0.3):
        self.congruence_weight = congruence_weight
        self._lock = threading.RLock()

    def rank(
        self,
        hits: List[MoodAwareHit],
        mood: MoodState,
        top_k: Optional[int] = None,
    ) -> List[MoodAwareHit]:
        """Rank hits, boosting mood-congruent results.

        Final score = (1 - w) * relevance + w * congruence
        where congruence = 1 - |formation_valence - current_valence| / 2
        """
        with self._lock:
            for hit in hits:
                hit.mood_congruence = 1.0 - abs(
                    hit.formation_valence - mood.valence
                ) / 2.0

            def final_score(h: MoodAwareHit) -> float:
                return (
                    (1.0 - self.congruence_weight) * h.relevance
                    + self.congruence_weight * h.mood_congruence
                )

            ranked = sorted(hits, key=final_score, reverse=True)
            if top_k:
                ranked = ranked[:top_k]
            return ranked

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"congruence_weight": self.congruence_weight}
