"""
# status: orphan (2026-08-15 audit, not in runtime path)
P12-2: Episodic Foresight & Mental Simulation.

Reference: Constructive Episodic Simulation Hypothesis (Schacter & Addis)
           + Scene Construction Theory (Maguire / Hassabis).

Design: Decomposes past episodic memories into recombinable elements
        (who / where / action / when / outcome), recombines them to
        generate future scenario variants, runs a mental sandbox
        simulation on each, and constructs spatially coherent scenes
        using hippocampus-like binding mechanisms.

Interface-compatible with: proactive_anticipator.py, episodic_rl.py
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ElementRole(Enum):
    AGENT = auto()         # who — the subject
    LOCATION = auto()      # where
    ACTION = auto()        # what happened
    TIME = auto()          # when
    OUTCOME = auto()       # result / consequence
    OBJECT = auto()        # object involved
    EMOTION = auto()       # emotional tag


class ScenarioRisk(Enum):
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()


class SimulationStatus(Enum):
    PENDING = auto()
    RUNNING = auto()
    COMPLETED = auto()
    DIVERGED = auto()      # scenario diverges too far from baseline


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class MemoryElement:
    """A single decomposable element from an episodic memory."""
    element_id: str
    role: ElementRole
    value: str                 # normalized label (e.g. "meeting_room_3", "presentation")
    embedding: np.ndarray = field(default_factory=lambda: np.zeros(64))
    confidence: float = 1.0    # extraction confidence
    source_episode_id: str = ""


@dataclass
class EpisodicMemory:
    """A single episodic memory trace."""
    episode_id: str
    timestamp: float
    elements: List[MemoryElement] = field(default_factory=list)
    summary: str = ""
    emotional_valence: float = 0.0  # -1.0 to 1.0


@dataclass
class ScenarioVariant:
    """A generated future scenario from recombined elements."""
    variant_id: str
    elements: List[MemoryElement] = field(default_factory=list)
    plausibility: float = 1.0
    risk: ScenarioRisk = ScenarioRisk.LOW
    parent_episodes: List[str] = field(default_factory=list)


@dataclass
class SimulationOutcome:
    """Result of running a mental simulation on a scenario."""
    scenario_id: str
    status: SimulationStatus
    expected_reward: float
    confidence: float
    trajectory: List[Dict[str, Any]] = field(default_factory=list)
    risk_score: float = 0.0
    notes: str = ""


@dataclass
class Scene:
    """A spatially coherent mental scene constructed from elements."""
    scene_id: str
    spatial_layout: Dict[str, np.ndarray]  # element_id -> 3D position
    binding_strength: float                # hippocampal binding quality
    elements: List[MemoryElement] = field(default_factory=list)
    narrative: str = ""


# ---------------------------------------------------------------------------
# EpisodicDecomposer
# ---------------------------------------------------------------------------

class EpisodicDecomposer:
    """
    Decomposes episodic memories into recombinable atomic elements:
    who (agent), where (location), what (action), when (time),
    outcome (result), objects, and emotional tags.
    """

    ROLE_KEYWORDS: Dict[ElementRole, List[str]] = {
        ElementRole.AGENT: ["agent", "user", "bot", "speaker", "actor", "participant", "I", "we"],
        ElementRole.LOCATION: ["room", "office", "desk", "kitchen", "home", "building", "at"],
        ElementRole.ACTION: ["click", "type", "run", "open", "close", "say", "write", "read", "send"],
        ElementRole.TIME: ["morning", "afternoon", "evening", "today", "yesterday", "monday", "202"],
        ElementRole.OUTCOME: ["result", "outcome", "success", "failure", "error", "completed"],
        ElementRole.OBJECT: ["file", "document", "window", "email", "message", "photo"],
        ElementRole.EMOTION: ["happy", "frustrated", "confused", "satisfied", "urgent", "calm"],
    }

    def __init__(self, embedding_dim: int = 64) -> None:
        self.embedding_dim = embedding_dim
        self._rng = np.random.default_rng(42)
        self._lock = threading.RLock()
        self._decompose_count: int = 0

    def decompose(self, memory: EpisodicMemory) -> List[MemoryElement]:
        """
        Decompose a single episodic memory into its constituent elements.
        Uses keyword matching + embedding-based semantic extraction.
        """
        with self._lock:
            elements: List[MemoryElement] = []
            text_lower = memory.summary.lower()

            for role, keywords in self.ROLE_KEYWORDS.items():
                matched = [kw for kw in keywords if kw in text_lower]
                if matched:
                    elem = MemoryElement(
                        element_id=f"{memory.episode_id}_{role.name}",
                        role=role,
                        value=matched[0],
                        embedding=self._rng.uniform(-0.1, 0.1, self.embedding_dim).astype(np.float64),
                        confidence=0.85,
                        source_episode_id=memory.episode_id,
                    )
                    elements.append(elem)

            # Also add a generic action element if none matched
            if not any(e.role == ElementRole.ACTION for e in elements):
                elements.append(MemoryElement(
                    element_id=f"{memory.episode_id}_ACTION",
                    role=ElementRole.ACTION,
                    value="interaction",
                    embedding=self._rng.uniform(-0.1, 0.1, self.embedding_dim).astype(np.float64),
                    confidence=0.5,
                    source_episode_id=memory.episode_id,
                ))

            # Add an emotional tag
            elements.append(MemoryElement(
                element_id=f"{memory.episode_id}_EMOTION",
                role=ElementRole.EMOTION,
                value="positive" if memory.emotional_valence > 0 else "negative",
                embedding=self._rng.uniform(-0.1, 0.1, self.embedding_dim).astype(np.float64),
                confidence=0.7,
                source_episode_id=memory.episode_id,
            ))

            self._decompose_count += 1
            return elements

    def decompose_batch(self, memories: List[EpisodicMemory]) -> List[List[MemoryElement]]:
        """Decompose a batch of episodic memories."""
        return [self.decompose(m) for m in memories]

    def statistics(self) -> Dict[str, Any]:
        return {"decompose_count": self._decompose_count, "embedding_dim": self.embedding_dim}


# ---------------------------------------------------------------------------
# ScenarioRecombinator
# ---------------------------------------------------------------------------

class ScenarioRecombinator:
    """
    Recombines memory elements from multiple past episodes to generate
    plausible future scenario variants.
    """

    def __init__(self, max_scenarios: int = 10, temperature: float = 0.7) -> None:
        self.max_scenarios = max_scenarios
        self.temperature = temperature
        self._rng = np.random.default_rng(123)
        self._lock = threading.RLock()
        self._generated_count: int = 0

    def recombine(
        self,
        episode_elements: List[List[MemoryElement]],
        constraints: Optional[List[str]] = None,
    ) -> List[ScenarioVariant]:
        """
        Generate scenario variants by recombining elements across episodes.
        Each scenario must include at least one of each role.
        """
        with self._lock:
            # Build role -> element pool
            pool: Dict[ElementRole, List[MemoryElement]] = {}
            for elems in episode_elements:
                for elem in elems:
                    pool.setdefault(elem.role, []).append(elem)

            variants: List[ScenarioVariant] = []
            for v_idx in range(self.max_scenarios):
                selected: List[MemoryElement] = []
                for role in ElementRole:
                    if role in pool and pool[role]:
                        # weighted random selection with temperature
                        candidates = pool[role]
                        weights = np.array([e.confidence for e in candidates])
                        weights = np.exp(weights / self.temperature)
                        weights /= weights.sum()
                        pick = candidates[self._rng.choice(len(candidates), p=weights)]
                        selected.append(pick)

                # plausibility = average confidence of selected elements
                plausibility = float(np.mean([e.confidence for e in selected]))

                # risk assessment
                risk = ScenarioRisk.LOW
                if plausibility < 0.6:
                    risk = ScenarioRisk.HIGH
                elif plausibility < 0.8:
                    risk = ScenarioRisk.MEDIUM

                # collect source episodes
                parent_ids = list({e.source_episode_id for e in selected if e.source_episode_id})

                variant = ScenarioVariant(
                    variant_id=f"scenario_{self._generated_count + v_idx:04d}",
                    elements=selected,
                    plausibility=plausibility,
                    risk=risk,
                    parent_episodes=parent_ids,
                )
                variants.append(variant)

            self._generated_count += len(variants)
            return variants

    def statistics(self) -> Dict[str, Any]:
        return {"scenarios_generated": self._generated_count, "max_per_batch": self.max_scenarios}


# ---------------------------------------------------------------------------
# MentalSimulator
# ---------------------------------------------------------------------------

class MentalSimulator:
    """
    Runs a "mental sandbox" on generated scenarios to evaluate
    expected outcomes via lightweight forward simulation.
    """

    def __init__(
        self,
        max_steps: int = 20,
        risk_threshold: float = 0.7,
        reward_fn: Optional[Callable[[List[MemoryElement], int], float]] = None,
    ) -> None:
        self.max_steps = max_steps
        self.risk_threshold = risk_threshold
        self.reward_fn = reward_fn or self._default_reward
        self._rng = np.random.default_rng(456)
        self._lock = threading.RLock()
        self._simulation_count: int = 0

    @staticmethod
    def _default_reward(elements: List[MemoryElement], step: int) -> float:
        # default: higher confidence elements yield higher reward, decay over steps
        base = float(np.mean([e.confidence for e in elements])) if elements else 0.5
        return base * (0.95 ** step)

    def simulate(self, scenario: ScenarioVariant) -> SimulationOutcome:
        """Run mental simulation on a single scenario variant."""
        with self._lock:
            trajectory: List[Dict[str, Any]] = []
            total_reward = 0.0

            for step in range(self.max_steps):
                r = self.reward_fn(scenario.elements, step)
                trajectory.append({"step": step, "reward": r, "state": "nominal"})
                total_reward += r

                # risk-based early termination
                if scenario.risk == ScenarioRisk.CRITICAL:
                    self._simulation_count += 1
                    return SimulationOutcome(
                        scenario_id=scenario.variant_id,
                        status=SimulationStatus.DIVERGED,
                        expected_reward=total_reward,
                        confidence=0.3,
                        trajectory=trajectory,
                        risk_score=1.0,
                        notes="Terminated early: critical risk scenario.",
                    )

            # confidence decays with risk
            confidence_map = {
                ScenarioRisk.LOW: 0.9,
                ScenarioRisk.MEDIUM: 0.7,
                ScenarioRisk.HIGH: 0.5,
                ScenarioRisk.CRITICAL: 0.3,
            }
            confidence = confidence_map.get(scenario.risk, 0.5) * scenario.plausibility

            risk_score = {
                ScenarioRisk.LOW: 0.1,
                ScenarioRisk.MEDIUM: 0.4,
                ScenarioRisk.HIGH: 0.7,
                ScenarioRisk.CRITICAL: 0.95,
            }.get(scenario.risk, 0.5)

            self._simulation_count += 1
            return SimulationOutcome(
                scenario_id=scenario.variant_id,
                status=SimulationStatus.COMPLETED,
                expected_reward=total_reward,
                confidence=confidence,
                trajectory=trajectory,
                risk_score=risk_score,
            )

    def simulate_batch(self, scenarios: List[ScenarioVariant]) -> List[SimulationOutcome]:
        return [self.simulate(s) for s in scenarios]

    def statistics(self) -> Dict[str, Any]:
        return {"simulations_run": self._simulation_count, "max_steps": self.max_steps}


# ---------------------------------------------------------------------------
# SceneConstructor
# ---------------------------------------------------------------------------

class SceneConstructor:
    """
    Constructs spatially coherent mental scenes via hippocampus-like
    binding of elements into a 3D layout. Supports narrative generation
    for downstream episodic foresight.
    """

    def __init__(self, spatial_dim: int = 3) -> None:
        self.spatial_dim = spatial_dim
        self._rng = np.random.default_rng(789)
        self._lock = threading.RLock()
        self._scenes_constructed: int = 0
        self._bindings: Dict[str, float] = {}

    def construct(self, scenario: ScenarioVariant) -> Scene:
        """
        Bind scenario elements into a coherent spatial layout.
        Elements with complementary roles are placed closer together.
        """
        with self._lock:
            layout: Dict[str, np.ndarray] = {}
            elements = scenario.elements
            n = len(elements)

            # Generate positions with role-based attraction
            for i, elem in enumerate(elements):
                pos = np.zeros(self.spatial_dim, dtype=np.float64)
                if i > 0:
                    # place near previous element, offset by role compatibility
                    prev = layout[elements[i - 1].element_id]
                    offset = self._rng.uniform(-0.5, 0.5, self.spatial_dim)
                    pos = prev + offset
                else:
                    pos = self._rng.uniform(-1, 1, self.spatial_dim)
                layout[elem.element_id] = pos

            # compute binding strength = inverse of average pairwise distance
            positions = list(layout.values())
            if len(positions) > 1:
                dists = []
                for i in range(len(positions)):
                    for j in range(i + 1, len(positions)):
                        dists.append(float(np.linalg.norm(positions[i] - positions[j])))
                avg_dist = np.mean(dists)
                binding_strength = 1.0 / (1.0 + avg_dist)
            else:
                binding_strength = 1.0

            scene_id = f"scene_{self._scenes_constructed:04d}"
            self._bindings[scene_id] = binding_strength

            # Generate simple narrative
            roles_text = ", ".join(f"{e.role.name}={e.value}" for e in elements)
            narrative = f"Scene {scene_id}: {roles_text} | binding={binding_strength:.3f}"

            self._scenes_constructed += 1
            return Scene(
                scene_id=scene_id,
                spatial_layout=layout,
                binding_strength=binding_strength,
                elements=elements,
                narrative=narrative,
            )

    def construct_batch(self, scenarios: List[ScenarioVariant]) -> List[Scene]:
        return [self.construct(s) for s in scenarios]

    def statistics(self) -> Dict[str, Any]:
        return {
            "scenes_constructed": self._scenes_constructed,
            "mean_binding_strength": float(np.mean(list(self._bindings.values())))
            if self._bindings else 0.0,
        }
