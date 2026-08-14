"""
P15-1: Theory of Mind User Modeling.

Reference: Readable Minds (arXiv 2604.04157) — Persistent memory catalyzes
           emergent Theory of Mind behaviors in LLM agents.

Design: Builds multi-dimensional mental models of users and interacting
        agents — goals, values, preferences, knowledge state, emotional
        state, and intent trajectories. Tracks knowledge asymmetry between
        system and user via Bayesian belief updates after each interaction.

Complementary to: episodic_foresight.py (future prediction) —
                  this module handles current mental-state modeling.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class KnowledgeDomain(Enum):
    """Domain of knowledge tracked in belief state."""
    FACTUAL = auto()       # objective facts
    PROCEDURAL = auto()    # how-to knowledge
    PERSONAL = auto()      # user-specific information
    TASK_RELEVANT = auto() # knowledge about current task
    COMMON_SENSE = auto()  # shared cultural / commonsense knowledge


class EmotionalValence(Enum):
    """Categorical emotional valence."""
    POSITIVE = auto()
    NEGATIVE = auto()
    NEUTRAL = auto()
    AMBIVALENT = auto()


class IntentCertainty(Enum):
    """Confidence level of inferred intent."""
    SPECULATIVE = auto()   # low-confidence hypothesis
    PLAUSIBLE = auto()     # medium confidence
    LIKELY = auto()        # high confidence
    CONFIRMED = auto()     # user explicitly confirmed


class InteractionRole(Enum):
    """Role of the counterparty in a multi-agent interaction."""
    COLLABORATOR = auto()
    COMPETITOR = auto()
    OBSERVER = auto()
    SUPERVISOR = auto()
    UNKNOWN = auto()


class KnowledgeState(Enum):
    """Binary knowledge state for a domain/node."""
    KNOWN = auto()
    UNKNOWN = auto()
    UNCERTAIN = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Goal:
    """User goal tracked in the mental model."""
    goal_id: str
    description: str
    priority: float = 0.5           # 0.0–1.0
    active: bool = True
    parent_goal_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


@dataclass
class Value:
    """A tracked user value or principle."""
    value_id: str
    label: str
    strength: float = 0.5           # 0.0–1.0, inferred commitment level
    source_evidence: List[str] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)


@dataclass
class Preference:
    """User preference entry."""
    preference_id: str
    domain: str
    key: str
    value: Any
    confidence: float = 0.5
    source: str = "inferred"
    timestamp: float = field(default_factory=time.time)


@dataclass
class BeliefNode:
    """A single belief about user's knowledge state."""
    domain: KnowledgeDomain
    topic: str
    state: KnowledgeState = KnowledgeState.UNCERTAIN
    probability: float = 0.5        # Bayesian belief P(user_knows | evidence)
    prior: float = 0.5
    evidence_count: int = 0
    last_evidence_time: float = field(default_factory=time.time)


@dataclass
class EmotionalState:
    """Snapshot of the user's emotional state."""
    valence: EmotionalValence = EmotionalValence.NEUTRAL
    intensity: float = 0.0          # 0.0–1.0
    primary_emotion: str = ""
    confidence: float = 0.0
    cue_tokens: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class IntentTrajectory:
    """Sequence of inferred intents over time."""
    trajectory_id: str
    intent_descriptions: List[str] = field(default_factory=list)
    certainties: List[IntentCertainty] = field(default_factory=list)
    timestamps: List[float] = field(default_factory=list)
    drift_score: float = 0.0        # intent shift magnitude


@dataclass
class OpponentModel:
    """Model of another agent's hidden state."""
    agent_id: str
    role: InteractionRole = InteractionRole.UNKNOWN
    inferred_goals: List[Goal] = field(default_factory=list)
    inferred_capabilities: Dict[str, float] = field(default_factory=dict)
    cooperation_tendency: float = 0.5
    deception_probability: float = 0.0
    interaction_history_length: int = 0
    last_interaction: float = 0.0


@dataclass
class MentalModelSnapshot:
    """Immutable snapshot of the full mental model at a point in time."""
    user_id: str
    goals: List[Goal]
    values: List[Value]
    preferences: List[Preference]
    emotional_state: EmotionalState
    belief_nodes: Dict[str, BeliefNode]
    intent_trajectory: IntentTrajectory
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Core classes
# ---------------------------------------------------------------------------

class UserMentalModel:
    """Multi-dimensional mental model of a single user.

    Tracks goals, values, preferences, emotional state, knowledge
    asymmetries, and intent trajectories through Bayesian updates.
    """

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._lock = threading.RLock()

        # Storage
        self.goals: Dict[str, Goal] = {}
        self.values: Dict[str, Value] = {}
        self.preferences: Dict[str, Preference] = {}
        self.belief_nodes: Dict[str, BeliefNode] = {}
        self.emotional_states: deque = deque(maxlen=100)
        self.intent_trajectories: List[IntentTrajectory] = []

        # Current state
        self._current_emotional_state = EmotionalState()
        self._active_intent = IntentTrajectory(trajectory_id="default")

    # ---------- Goal management ----------

    def add_goal(self, goal: Goal) -> None:
        with self._lock:
            self.goals[goal.goal_id] = goal
            logger.debug(f"[UserMentalModel] Added goal '{goal.goal_id}' for {self.user_id}")

    def update_goal_priority(self, goal_id: str, new_priority: float) -> bool:
        with self._lock:
            g = self.goals.get(goal_id)
            if g is None:
                return False
            g.priority = max(0.0, min(1.0, new_priority))
            g.last_updated = time.time()
            return True

    def deactivate_goal(self, goal_id: str) -> bool:
        with self._lock:
            g = self.goals.get(goal_id)
            if g is None:
                return False
            g.active = False
            g.last_updated = time.time()
            return True

    def get_active_goals(self) -> List[Goal]:
        with self._lock:
            return sorted(
                [g for g in self.goals.values() if g.active],
                key=lambda x: x.priority, reverse=True,
            )

    # ---------- Value management ----------

    def add_or_update_value(self, value: Value) -> None:
        with self._lock:
            existing = self.values.get(value.value_id)
            if existing:
                existing.strength = 0.7 * existing.strength + 0.3 * value.strength
                existing.source_evidence.extend(value.source_evidence)
                existing.last_updated = time.time()
            else:
                self.values[value.value_id] = value

    def get_top_values(self, n: int = 5) -> List[Value]:
        with self._lock:
            return sorted(self.values.values(), key=lambda v: v.strength, reverse=True)[:n]

    # ---------- Preference management ----------

    def set_preference(self, pref: Preference) -> None:
        with self._lock:
            key = f"{pref.domain}.{pref.key}"
            existing = self.preferences.get(key)
            if existing:
                existing.value = pref.value
                existing.confidence = max(existing.confidence, pref.confidence)
                existing.timestamp = time.time()
            else:
                self.preferences[key] = pref

    def get_preference(self, domain: str, key: str) -> Optional[Preference]:
        with self._lock:
            return self.preferences.get(f"{domain}.{key}")

    # ---------- Belief state / knowledge asymmetry ----------

    def get_or_create_belief(self, topic: str, domain: KnowledgeDomain) -> BeliefNode:
        with self._lock:
            node_key = f"{domain.name}:{topic}"
            if node_key not in self.belief_nodes:
                self.belief_nodes[node_key] = BeliefNode(
                    domain=domain, topic=topic,
                )
            return self.belief_nodes[node_key]

    def update_belief(self, topic: str, domain: KnowledgeDomain,
                      evidence_positive: bool) -> BeliefNode:
        """Bayesian belief update based on new evidence.

        evidence_positive=True  → user demonstrated knowledge
        evidence_positive=False → user demonstrated lack of knowledge
        """
        with self._lock:
            node = self.get_or_create_belief(topic, domain)
            likelihood_ratio = 5.0 if evidence_positive else 0.2

            # Bayes: P(K|E) = P(E|K)*P(K) / [P(E|K)*P(K) + P(E|~K)*(1-P(K))]
            prior = node.probability
            numerator = likelihood_ratio * prior
            denominator = numerator + (1.0 - prior)
            posterior = numerator / max(denominator, 1e-9)

            node.prior = prior
            node.probability = posterior
            node.evidence_count += 1
            node.last_evidence_time = time.time()

            if posterior > 0.8:
                node.state = KnowledgeState.KNOWN
            elif posterior < 0.2:
                node.state = KnowledgeState.UNKNOWN
            else:
                node.state = KnowledgeState.UNCERTAIN

            return node

    # ---------- Emotional state ----------

    def set_emotional_state(self, state: EmotionalState) -> None:
        with self._lock:
            self._current_emotional_state = state
            self.emotional_states.append(state)

    def get_emotional_state(self) -> EmotionalState:
        with self._lock:
            return self._current_emotional_state

    # ---------- Intent trajectory ----------

    def append_intent(self, description: str, certainty: IntentCertainty) -> None:
        with self._lock:
            self._active_intent.intent_descriptions.append(description)
            self._active_intent.certainties.append(certainty)
            self._active_intent.timestamps.append(time.time())

    def finalize_intent_trajectory(self) -> IntentTrajectory:
        with self._lock:
            traj = self._active_intent
            if len(traj.certainties) >= 2:
                shifts = [
                    abs(hash(traj.intent_descriptions[i]) - hash(traj.intent_descriptions[i - 1]))
                    for i in range(1, len(traj.intent_descriptions))
                ]
                traj.drift_score = float(np.mean(shifts) / max(1, max(shifts))) if shifts else 0.0
            self.intent_trajectories.append(traj)
            self._active_intent = IntentTrajectory(trajectory_id=f"traj_{len(self.intent_trajectories)}")
            return traj

    # ---------- Snapshot & statistics ----------

    def snapshot(self) -> MentalModelSnapshot:
        with self._lock:
            return MentalModelSnapshot(
                user_id=self.user_id,
                goals=list(self.goals.values()),
                values=list(self.values.values()),
                preferences=list(self.preferences.values()),
                emotional_state=self._current_emotional_state,
                belief_nodes=dict(self.belief_nodes),
                intent_trajectory=self._active_intent,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            known_count = sum(1 for n in self.belief_nodes.values() if n.state == KnowledgeState.KNOWN)
            return {
                "user_id": self.user_id,
                "goal_count": len(self.goals),
                "active_goal_count": sum(1 for g in self.goals.values() if g.active),
                "value_count": len(self.values),
                "preference_count": len(self.preferences),
                "belief_node_count": len(self.belief_nodes),
                "known_facts": known_count,
                "unknown_facts": sum(1 for n in self.belief_nodes.values() if n.state == KnowledgeState.UNKNOWN),
                "uncertain_facts": sum(1 for n in self.belief_nodes.values() if n.state == KnowledgeState.UNCERTAIN),
                "intent_trajectory_count": len(self.intent_trajectories),
                "emotional_state_count": len(self.emotional_states),
                "current_valence": self._current_emotional_state.valence.name,
            }


class OpponentIntentInference:
    """Infers hidden intents of other agents from interaction patterns.

    Works in multi-agent scenarios: analyzes turn-taking, response
    patterns, cooperative/competitive signals to infer goals and
    deception probability.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.opponent_models: Dict[str, OpponentModel] = {}

    def get_or_create_model(self, agent_id: str) -> OpponentModel:
        with self._lock:
            if agent_id not in self.opponent_models:
                self.opponent_models[agent_id] = OpponentModel(agent_id=agent_id)
            return self.opponent_models[agent_id]

    def observe_interaction(self, agent_id: str, action: str,
                            response_delay: float = 0.0,
                            cooperative_signal: Optional[bool] = None,
                            ) -> OpponentModel:
        """Update opponent model based on an observed interaction."""
        with self._lock:
            model = self.get_or_create_model(agent_id)
            model.interaction_history_length += 1
            model.last_interaction = time.time()

            if cooperative_signal is not None:
                alpha = 0.15
                model.cooperation_tendency = (
                    (1 - alpha) * model.cooperation_tendency
                    + alpha * (1.0 if cooperative_signal else 0.0)
                )

            # Infer deception: suspicious if cooperative but slow to respond
            if cooperative_signal and response_delay > 5.0 and model.cooperation_tendency < 0.4:
                model.deception_probability = min(
                    1.0, model.deception_probability + 0.05,
                )
            else:
                model.deception_probability = max(
                    0.0, model.deception_probability - 0.01,
                )

            return model

    def infer_goals(self, agent_id: str, action_sequence: List[str]) -> List[Goal]:
        """Heuristic goal inference from action sequences."""
        with self._lock:
            model = self.get_or_create_model(agent_id)
            inferred: List[Goal] = []

            action_text = " ".join(action_sequence).lower()
            if any(kw in action_text for kw in ["maximize", "optimize", "win"]):
                inferred.append(Goal(
                    goal_id=f"{agent_id}_competitive",
                    description="Maximize own outcome",
                    priority=0.8,
                ))
            if any(kw in action_text for kw in ["share", "help", "collaborate"]):
                inferred.append(Goal(
                    goal_id=f"{agent_id}_cooperative",
                    description="Achieve mutual benefit",
                    priority=0.7,
                ))
            if any(kw in action_text for kw in ["hide", "deceive", "bluff"]):
                inferred.append(Goal(
                    goal_id=f"{agent_id}_deceptive",
                    description="Conceal true intent",
                    priority=0.9,
                ))

            model.inferred_goals.extend(inferred)
            return inferred

    def get_model(self, agent_id: str) -> Optional[OpponentModel]:
        with self._lock:
            return self.opponent_models.get(agent_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tracked_agents": len(self.opponent_models),
                "agents": {
                    aid: {
                        "interactions": m.interaction_history_length,
                        "cooperation_tendency": round(m.cooperation_tendency, 3),
                        "deception_probability": round(m.deception_probability, 3),
                    }
                    for aid, m in self.opponent_models.items()
                },
            }


class BeliefStateTracker:
    """Tracks knowledge asymmetry between user state and system state.

    Maintains a bipartite belief graph: what the user knows vs what
    the system knows, detecting gaps that need clarification or
    teaching opportunities.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._user_knowledge: Dict[str, BeliefNode] = {}
        self._system_knowledge: Dict[str, bool] = {}
        self._update_log: List[Tuple[str, KnowledgeState, float]] = []

    def register_system_knowledge(self, topic: str, known: bool = True) -> None:
        with self._lock:
            self._system_knowledge[topic] = known

    def register_user_knowledge(self, topic: str, domain: KnowledgeDomain,
                                 initial_state: KnowledgeState = KnowledgeState.UNCERTAIN,
                                 prior: float = 0.5) -> BeliefNode:
        with self._lock:
            node = BeliefNode(domain=domain, topic=topic, state=initial_state, probability=prior)
            self._user_knowledge[topic] = node
            return node

    def bayesian_update(self, topic: str, evidence_positive: bool) -> Optional[BeliefNode]:
        """Update user belief via Bayesian update."""
        with self._lock:
            node = self._user_knowledge.get(topic)
            if node is None:
                return None
            likelihood = 5.0 if evidence_positive else 0.2
            prior = node.probability
            posterior = (likelihood * prior) / (likelihood * prior + (1.0 - prior) + 1e-9)
            node.prior = prior
            node.probability = posterior
            node.evidence_count += 1
            node.last_evidence_time = time.time()

            if posterior > 0.8:
                node.state = KnowledgeState.KNOWN
            elif posterior < 0.2:
                node.state = KnowledgeState.UNKNOWN

            self._update_log.append((topic, node.state, posterior))
            if len(self._update_log) > 1000:
                self._update_log = self._update_log[-500:]
            return node

    def detect_asymmetries(self) -> List[Dict[str, Any]]:
        """Find topics where system knows but user likely doesn't."""
        with self._lock:
            gaps = []
            for topic, sys_known in self._system_knowledge.items():
                user_node = self._user_knowledge.get(topic)
                if sys_known and (user_node is None or user_node.probability < 0.3):
                    gaps.append({
                        "topic": topic,
                        "system_knows": True,
                        "user_probability": user_node.probability if user_node else 0.0,
                        "severity": "critical" if user_node is None else "moderate",
                    })
            return sorted(gaps, key=lambda g: g["user_probability"])

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tracked_topics": len(self._user_knowledge),
                "system_known_topics": sum(1 for v in self._system_knowledge.values() if v),
                "user_confirmed_known": sum(
                    1 for n in self._user_knowledge.values() if n.state == KnowledgeState.KNOWN
                ),
                "asymmetry_gaps": len(self.detect_asymmetries()),
                "update_count": len(self._update_log),
            }


class MentalStateUpdate:
    """Orchestrates post-interaction Bayesian belief updates.

    After each user interaction, this class:
    1. Extracts evidence about user knowledge from the interaction
    2. Updates the BeliefStateTracker
    3. Updates the UserMentalModel goals/values/preferences
    4. Produces an update delta for audit
    """

    def __init__(self, mental_model: UserMentalModel,
                 belief_tracker: BeliefStateTracker):
        self.mental_model = mental_model
        self.belief_tracker = belief_tracker
        self._lock = threading.RLock()
        self.update_count: int = 0

    def process_interaction(self, interaction_text: str,
                            demonstrated_knowledge: Optional[List[str]] = None,
                            demonstrated_ignorance: Optional[List[str]] = None,
                            emotional_cues: Optional[List[str]] = None,
                            new_intent: Optional[str] = None,
                            ) -> Dict[str, Any]:
        """Process one user interaction and update all mental models."""
        with self._lock:
            delta: Dict[str, Any] = {
                "belief_updates": [],
                "emotional_change": False,
                "intent_change": False,
            }

            # 1. Update belief state
            if demonstrated_knowledge:
                for topic in demonstrated_knowledge:
                    node = self.belief_tracker.bayesian_update(topic, evidence_positive=True)
                    if node:
                        delta["belief_updates"].append({
                            "topic": topic,
                            "new_state": node.state.name,
                            "probability": round(node.probability, 3),
                        })
            if demonstrated_ignorance:
                for topic in demonstrated_ignorance:
                    node = self.belief_tracker.bayesian_update(topic, evidence_positive=False)
                    if node:
                        delta["belief_updates"].append({
                            "topic": topic,
                            "new_state": node.state.name,
                            "probability": round(node.probability, 3),
                        })

            # 2. Update emotional state if cues present
            if emotional_cues:
                valence = EmotionalValence.NEUTRAL
                if any(w in " ".join(emotional_cues).lower() for w in ["happy", "great", "excited", "pleased"]):
                    valence = EmotionalValence.POSITIVE
                elif any(w in " ".join(emotional_cues).lower() for w in ["angry", "frustrated", "sad", "upset"]):
                    valence = EmotionalValence.NEGATIVE
                state = EmotionalState(
                    valence=valence,
                    intensity=0.6,
                    primary_emotion=emotional_cues[0] if emotional_cues else "",
                    confidence=0.7,
                    cue_tokens=emotional_cues,
                )
                self.mental_model.set_emotional_state(state)
                delta["emotional_change"] = True

            # 3. Append intent
            if new_intent:
                self.mental_model.append_intent(new_intent, IntentCertainty.PLAUSIBLE)
                delta["intent_change"] = True

            self.update_count += 1
            return delta

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "update_count": self.update_count,
                "mental_model_stats": self.mental_model.statistics(),
                "belief_tracker_stats": self.belief_tracker.statistics(),
            }
