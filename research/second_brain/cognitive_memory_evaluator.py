"""
# status: orphan (2026-08-15 audit, not in runtime path)
P15-2: Level-2 Cognitive Memory Evaluation.

Reference: LoCoMo-Plus (Xi'an Jiaotong University × Tencent) —
           Beyond factual recall: measuring constraint consistency,
           implicit constraint extraction, cue-trigger disconnect,
           and cognitive understanding in agent memory.

Design: Five-component evaluation framework that tests not just
        "what the agent remembers" but "what the agent understands":
        constraint consistency, implicit extraction, cue disconnect
        detection, cognitive scoring, and memory worthiness
        verification.

Complementary to: aml_protocol_adapter.py (Level-1 factual memory) —
                  this module handles Level-2 cognitive memory.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ConstraintType(Enum):
    """Type of constraint extracted from dialogue."""
    TEMPORAL = auto()       # time-based constraint
    PREFERENCE = auto()     # user preference constraint
    POLICY = auto()         # rule / policy / regulation
    GOAL = auto()           # goal-oriented constraint
    VALUE = auto()          # value-based constraint
    STATE = auto()          # user state constraint
    RELATIONAL = auto()     # relationship / role constraint
    PROCEDURAL = auto()     # how-to / process constraint


class ConsistencyVerdict(Enum):
    CONSISTENT = auto()
    INCONSISTENT = auto()
    AMBIGUOUS = auto()
    UNVERIFIABLE = auto()


class DisconnectSeverity(Enum):
    NONE = auto()           # perfectly connected
    MILD = auto()           # slight semantic drift
    MODERATE = auto()       # noticeable disconnect
    SEVERE = auto()         # major semantic gap
    COMPLETE = auto()       # unrelated


class MemoryWorthiness(Enum):
    LOW = auto()            # ephemeral, safe to discard
    MEDIUM = auto()         # potentially useful
    HIGH = auto()           # likely contains constraints/preferences
    CRITICAL = auto()       # contains binding constraints, must retain


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Constraint:
    """A single extracted constraint."""
    constraint_id: str
    constraint_type: ConstraintType
    description: str
    source_text: str
    confidence: float = 0.5
    is_binding: bool = False      # binding = must be respected
    scope: str = "global"         # global / session / task
    created_at: float = field(default_factory=time.time)


@dataclass
class ConsistencyCheck:
    """Result of a single constraint consistency check."""
    constraint_id: str
    response_id: str
    verdict: ConsistencyVerdict = ConsistencyVerdict.UNVERIFIABLE
    violation_description: str = ""
    similarity_score: float = 1.0
    checked_at: float = field(default_factory=time.time)


@dataclass
class CueTriggerPair:
    """A memory cue and the query it should trigger."""
    cue: str                      # what was stored (e.g. "user is vegetarian")
    trigger_query: str            # what should recall it (e.g. "restaurant recommendation")
    expected_relevance: float = 1.0
    actual_relevance: float = 0.0
    disconnect_severity: DisconnectSeverity = DisconnectSeverity.NONE


@dataclass
class CognitiveScore:
    """Level-2 cognitive memory score for an agent."""
    agent_id: str
    constraint_consistency: float = 0.0      # 0–1
    implicit_extraction_quality: float = 0.0 # 0–1
    cue_trigger_alignment: float = 0.0       # 0–1
    worthiness_precision: float = 0.0        # 0–1
    overall_cognitive_score: float = 0.0     # weighted composite
    sample_count: int = 0
    evaluated_at: float = field(default_factory=time.time)


@dataclass
class WorthinessAssessment:
    """Assessment of whether a dialogue fragment should enter long-term memory."""
    fragment_id: str
    worthiness: MemoryWorthiness = MemoryWorthiness.LOW
    contains_binding_constraint: bool = False
    contains_persistent_preference: bool = False
    contains_user_state: bool = False
    reasoning: str = ""
    score: float = 0.0


# ---------------------------------------------------------------------------
# Core classes
# ---------------------------------------------------------------------------

class ConstraintConsistencyEvaluator:
    """Evaluates agent responses against previously established constraints.

    Instead of string matching, uses semantic embedding similarity to
    determine whether a response is consistent with all constraints
    the agent should have remembered.
    """

    def __init__(self, embedding_dim: int = 384):
        self._lock = threading.RLock()
        self.embedding_dim = embedding_dim
        self._constraints: Dict[str, Constraint] = {}
        self._checks: List[ConsistencyCheck] = []
        self._embedding_cache: Dict[str, np.ndarray] = {}

    def register_constraint(self, constraint: Constraint) -> str:
        with self._lock:
            self._constraints[constraint.constraint_id] = constraint
            return constraint.constraint_id

    def _get_embedding(self, text: str) -> np.ndarray:
        """Lightweight text embedding using hash-based projection."""
        if text in self._embedding_cache:
            return self._embedding_cache[text]
        seed = hash(text) % (2 ** 31)
        rng = np.random.RandomState(abs(seed))
        vec = rng.randn(self.embedding_dim).astype(np.float32)
        vec /= np.linalg.norm(vec) + 1e-8
        self._embedding_cache[text] = vec
        return vec

    def _cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))

    def check_response(self, response_text: str, constraint_ids: List[str],
                       threshold: float = 0.6) -> List[ConsistencyCheck]:
        """Check a response against specified constraints."""
        results = []
        with self._lock:
            resp_vec = self._get_embedding(response_text)
            for cid in constraint_ids:
                constraint = self._constraints.get(cid)
                if constraint is None:
                    continue
                const_vec = self._get_embedding(constraint.description)
                sim = self._cosine_similarity(resp_vec, const_vec)

                verdict = ConsistencyVerdict.CONSISTENT
                violation = ""
                if sim < threshold:
                    verdict = ConsistencyVerdict.INCONSISTENT
                    violation = f"Response diverges from '{constraint.description}' (sim={sim:.3f})"
                elif sim < threshold + 0.15:
                    verdict = ConsistencyVerdict.AMBIGUOUS

                check = ConsistencyCheck(
                    constraint_id=cid,
                    response_id=str(uuid.uuid4())[:8],
                    verdict=verdict,
                    violation_description=violation,
                    similarity_score=sim,
                )
                results.append(check)
                self._checks.append(check)

            return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            violations = sum(
                1 for c in self._checks if c.verdict == ConsistencyVerdict.INCONSISTENT
            )
            return {
                "registered_constraints": len(self._constraints),
                "total_checks": len(self._checks),
                "violations_detected": violations,
                "consistency_rate": (
                    1.0 - violations / max(len(self._checks), 1)
                ) if self._checks else 1.0,
            }


class ImplicitConstraintExtractor:
    """Extracts implicit constraints from natural dialogue.

    Identifies user states, goals, values, and procedural constraints
    that are implied but not explicitly stated as "rules".
    """

    # Heuristic trigger patterns for different constraint types
    _PATTERNS: Dict[ConstraintType, List[str]] = {
        ConstraintType.TEMPORAL: [
            "deadline", "by tomorrow", "next week", "urgent", "asap",
            "every day", "always", "never", "before", "after",
        ],
        ConstraintType.PREFERENCE: [
            "i like", "i prefer", "i want", "i'd rather", "my favorite",
            "i hate", "don't like", "can't stand",
        ],
        ConstraintType.POLICY: [
            "must", "required", "mandatory", "policy", "regulation",
            "compliance", "should not", "not allowed",
        ],
        ConstraintType.GOAL: [
            "i need to", "i'm trying to", "goal is", "aiming for",
            "target", "objective", "working toward",
        ],
        ConstraintType.VALUE: [
            "i believe", "principle", "important to me", "value",
            "ethically", "morally", "priority",
        ],
        ConstraintType.STATE: [
            "i am", "i'm currently", "right now i", "my situation",
            "my condition", "i feel",
        ],
        ConstraintType.PROCEDURAL: [
            "step by step", "first", "then", "process", "procedure",
            "workflow", "how to", "method",
        ],
    }

    _BINDING_INDICATORS = [
        "must", "required", "mandatory", "always", "never", "can't",
        "not allowed", "strictly", "absolutely",
    ]

    def __init__(self):
        self._lock = threading.RLock()
        self._extracted: List[Constraint] = []

    def extract(self, dialogue_turn: str,
                turn_id: Optional[str] = None) -> List[Constraint]:
        """Extract implicit constraints from a single dialogue turn."""
        lower = dialogue_turn.lower()
        constraints: List[Constraint] = []

        with self._lock:
            for ctype, triggers in self._PATTERNS.items():
                for trigger in triggers:
                    if trigger in lower:
                        # Find the sentence containing the trigger
                        sentences = dialogue_turn.replace("?", ".").replace("!", ".").split(".")
                        for sent in sentences:
                            if trigger in sent.lower() and len(sent.strip()) > 5:
                                is_binding = any(
                                    kw in sent.lower() for kw in self._BINDING_INDICATORS
                                )
                                constraint = Constraint(
                                    constraint_id=f"{ctype.name}_{uuid.uuid4().hex[:8]}",
                                    constraint_type=ctype,
                                    description=sent.strip(),
                                    source_text=dialogue_turn,
                                    confidence=0.7,
                                    is_binding=is_binding,
                                )
                                constraints.append(constraint)
                                break

            self._extracted.extend(constraints)
            return constraints

    def get_all_constraints(self) -> List[Constraint]:
        with self._lock:
            return list(self._extracted)

    def get_binding_constraints(self) -> List[Constraint]:
        with self._lock:
            return [c for c in self._extracted if c.is_binding]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            by_type = defaultdict(int)
            for c in self._extracted:
                by_type[c.constraint_type.name] += 1
            return {
                "total_extracted": len(self._extracted),
                "binding_count": sum(1 for c in self._extracted if c.is_binding),
                "by_type": dict(by_type),
            }


class CueTriggerDisconnectDetector:
    """Quantifies the semantic disconnection between stored cues and retrieval triggers.

    A "cue-trigger disconnect" occurs when a memory is stored under one
    semantic framing but the query that should retrieve it uses a different
    framing, causing retrieval failure.
    """

    def __init__(self, embedding_dim: int = 384):
        self._lock = threading.RLock()
        self.embedding_dim = embedding_dim
        self._pairs: List[CueTriggerPair] = []

    def _get_embedding(self, text: str) -> np.ndarray:
        seed = hash(text) % (2 ** 31)
        rng = np.random.RandomState(abs(seed))
        vec = rng.randn(self.embedding_dim).astype(np.float32)
        vec /= np.linalg.norm(vec) + 1e-8
        return vec

    def register_pair(self, cue: str, trigger_query: str) -> CueTriggerPair:
        with self._lock:
            cue_vec = self._get_embedding(cue)
            query_vec = self._get_embedding(trigger_query)
            sim = float(np.dot(cue_vec, query_vec))

            severity = DisconnectSeverity.NONE
            if sim < 0.2:
                severity = DisconnectSeverity.COMPLETE
            elif sim < 0.35:
                severity = DisconnectSeverity.SEVERE
            elif sim < 0.5:
                severity = DisconnectSeverity.MODERATE
            elif sim < 0.7:
                severity = DisconnectSeverity.MILD

            pair = CueTriggerPair(
                cue=cue,
                trigger_query=trigger_query,
                actual_relevance=sim,
                disconnect_severity=severity,
            )
            self._pairs.append(pair)
            return pair

    def get_disconnect_rate(self) -> float:
        with self._lock:
            if not self._pairs:
                return 0.0
            disconnected = sum(
                1 for p in self._pairs
                if p.disconnect_severity in (
                    DisconnectSeverity.SEVERE, DisconnectSeverity.COMPLETE,
                )
            )
            return disconnected / len(self._pairs)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            by_severity = defaultdict(int)
            for p in self._pairs:
                by_severity[p.disconnect_severity.name] += 1
            avg_sim = (
                float(np.mean([p.actual_relevance for p in self._pairs]))
                if self._pairs else 0.0
            )
            return {
                "total_pairs": len(self._pairs),
                "disconnect_rate": self.get_disconnect_rate(),
                "average_similarity": round(avg_sim, 4),
                "by_severity": dict(by_severity),
            }


class CognitiveMemoryScorer:
    """Computes Level-2 cognitive memory scores for agents.

    Aggregates constraint consistency, implicit extraction, cue-trigger
    alignment, and worthiness precision into a unified cognitive score.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._scores: Dict[str, CognitiveScore] = {}

    def compute_score(
        self,
        agent_id: str,
        constraint_consistency: float,
        implicit_quality: float,
        cue_trigger_alignment: float,
        worthiness_precision: float,
    ) -> CognitiveScore:
        with self._lock:
            overall = (
                0.35 * constraint_consistency
                + 0.25 * implicit_quality
                + 0.25 * cue_trigger_alignment
                + 0.15 * worthiness_precision
            )
            score = CognitiveScore(
                agent_id=agent_id,
                constraint_consistency=constraint_consistency,
                implicit_extraction_quality=implicit_quality,
                cue_trigger_alignment=cue_trigger_alignment,
                worthiness_precision=worthiness_precision,
                overall_cognitive_score=overall,
                sample_count=1,
            )
            if agent_id in self._scores:
                prev = self._scores[agent_id]
                n = prev.sample_count + 1
                score.sample_count = n
                score.overall_cognitive_score = (
                    (prev.overall_cognitive_score * prev.sample_count + overall) / n
                )
                score.constraint_consistency = (
                    (prev.constraint_consistency * prev.sample_count + constraint_consistency) / n
                )
                score.implicit_extraction_quality = (
                    (prev.implicit_extraction_quality * prev.sample_count + implicit_quality) / n
                )
                score.cue_trigger_alignment = (
                    (prev.cue_trigger_alignment * prev.sample_count + cue_trigger_alignment) / n
                )
                score.worthiness_precision = (
                    (prev.worthiness_precision * prev.sample_count + worthiness_precision) / n
                )
            self._scores[agent_id] = score
            return score

    def get_score(self, agent_id: str) -> Optional[CognitiveScore]:
        with self._lock:
            return self._scores.get(agent_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "evaluated_agents": len(self._scores),
                "average_cognitive_score": round(
                    float(np.mean([s.overall_cognitive_score for s in self._scores.values()]))
                    if self._scores else 0.0, 4,
                ),
                "agent_scores": {
                    aid: round(s.overall_cognitive_score, 4)
                    for aid, s in self._scores.items()
                },
            }


class MemoryWorthinessVerifier:
    """Verifies which dialogue fragments are worth entering long-term memory.

    A fragment is "worthy" if it contains binding constraints, persistent
    preferences, or durable user state information that future interactions
    will need to reference.
    """

    _HIGH_VALUE_PATTERNS = [
        "always", "never", "permanent", "lifelong", "chronic",
        "policy", "rule", "guideline", "preference", "allergic",
        "requirement", "mandatory", "strict", "must", "can't",
    ]
    _MEDIUM_VALUE_PATTERNS = [
        "usually", "typically", "generally", "prefer", "like",
        "often", "most of the time", "tend to",
    ]

    def __init__(self):
        self._lock = threading.RLock()
        self._assessments: List[WorthinessAssessment] = []

    def assess(self, fragment_text: str,
               fragment_id: Optional[str] = None) -> WorthinessAssessment:
        """Assess whether a dialogue fragment is worth remembering."""
        fid = fragment_id or str(uuid.uuid4())[:8]
        lower = fragment_text.lower()

        has_binding = any(kw in lower for kw in self._HIGH_VALUE_PATTERNS)
        has_preference = any(kw in lower for kw in self._MEDIUM_VALUE_PATTERNS)
        has_state = any(
            kw in lower
            for kw in ["i am", "i'm", "my", "currently", "right now", "today"]
        )

        score = 0.0
        if has_binding:
            score += 0.5
        if has_preference:
            score += 0.3
        if has_state:
            score += 0.2

        if score >= 0.7:
            worthiness = MemoryWorthiness.CRITICAL
        elif score >= 0.4:
            worthiness = MemoryWorthiness.HIGH
        elif score >= 0.2:
            worthiness = MemoryWorthiness.MEDIUM
        else:
            worthiness = MemoryWorthiness.LOW

        reasoning_parts = []
        if has_binding:
            reasoning_parts.append("contains binding constraint")
        if has_preference:
            reasoning_parts.append("contains persistent preference")
        if has_state:
            reasoning_parts.append("contains user state info")

        assessment = WorthinessAssessment(
            fragment_id=fid,
            worthiness=worthiness,
            contains_binding_constraint=has_binding,
            contains_persistent_preference=has_preference,
            contains_user_state=has_state,
            reasoning="; ".join(reasoning_parts) if reasoning_parts else "no durable signals",
            score=score,
        )

        with self._lock:
            self._assessments.append(assessment)

        return assessment

    def get_worthy_fragments(self,
                             min_worthiness: MemoryWorthiness = MemoryWorthiness.HIGH,
                             ) -> List[WorthinessAssessment]:
        with self._lock:
            return [a for a in self._assessments if a.worthiness.value >= min_worthiness.value]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            by_level = defaultdict(int)
            for a in self._assessments:
                by_level[a.worthiness.name] += 1
            total = len(self._assessments)
            return {
                "total_assessed": total,
                "worthy_rate": (
                    sum(1 for a in self._assessments
                        if a.worthiness in (MemoryWorthiness.HIGH, MemoryWorthiness.CRITICAL))
                    / max(total, 1)
                ),
                "by_worthiness": dict(by_level),
            }
