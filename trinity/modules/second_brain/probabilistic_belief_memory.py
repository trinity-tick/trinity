"""
# status: orphan (2026-08-15 audit, not in runtime path)
P25-2: Probabilistic Belief Memory (BeliefMem)
arXiv:2605.05583

Multi-candidate conclusion pool with Noisy-OR probability updates.
Tracks ambiguous information with belief degrees instead of deterministic facts.
LoCoMo/ALFWorld SOTA performance.
"""

import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class BeliefState(Enum):
    HYPOTHESIS = "hypothesis"
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"


class EvidenceSource(Enum):
    DIRECT_OBS = "direct_observation"
    INFERENCE = "inference"
    EXTERNAL_KB = "external_kb"
    USER_INPUT = "user_input"
    COUNTER_EVIDENCE = "counter_evidence"


@dataclass
class EvidenceFragment:
    source: EvidenceSource
    description: str
    reliability: float  # 0-1
    timestamp: float = field(default_factory=time.time)
    decay_factor: float = 0.95
    counter: bool = False

    def effective_weight(self, current_time: float) -> float:
        age = max(0, current_time - self.timestamp)
        return self.reliability * (self.decay_factor ** (age / 3600.0))


@dataclass
class BeliefCandidate:
    candidate_id: str
    proposition: str
    belief_score: float = 0.5
    prior: float = 0.5
    evidence_chain: List[EvidenceFragment] = field(default_factory=list)
    status: BeliefState = BeliefState.HYPOTHESIS
    contradictions: Set[str] = field(default_factory=set)
    supports: Set[str] = field(default_factory=set)
    last_updated: float = field(default_factory=time.time)
    update_count: int = 0

    def noiseless_belief(self) -> float:
        """Noisy-OR aggregation: P = 1 - product(1 - w_i)"""
        now = time.time()
        p = 1.0
        for f in self.evidence_chain:
            w = f.effective_weight(now)
            if f.counter:
                p *= (1 + w)
            else:
                p *= (1 - w)
        return max(0.0, min(1.0, 1.0 - abs(p)))


class NoisyORUpdater:
    """Noisy-OR belief propagation."""
    def __init__(self, base_rate: float = 0.5, noise: float = 0.05):
        self.base_rate = base_rate
        self.noise = noise

    def update(self, candidate: BeliefCandidate, new_evidence: EvidenceFragment) -> float:
        candidate.evidence_chain.append(new_evidence)
        candidate.last_updated = time.time()
        candidate.update_count += 1
        raw = candidate.noiseless_belief()
        noisy = raw + self.noise * (0.5 - raw) * (1 + self.noise)
        candidate.belief_score = max(0.01, min(0.99, noisy + self.base_rate * 0.1))
        return candidate.belief_score

    def batch_update(self, candidates: List[BeliefCandidate],
                     evidence: List[EvidenceFragment]) -> Dict[str, float]:
        results = {}
        for c in candidates:
            for e in evidence:
                self.update(c, e)
            results[c.candidate_id] = c.belief_score
        return results


class BeliefContradictionDetector:
    """Detect contradictory beliefs among candidates."""
    def __init__(self, threshold: float = 0.6):
        self.threshold = threshold
        self.contradiction_pairs: List[Tuple[str, str]] = []

    def detect(self, candidates: List[BeliefCandidate]) -> List[Tuple[str, str]]:
        self.contradiction_pairs.clear()
        for i, ci in enumerate(candidates):
            for j, cj in enumerate(candidates):
                if i >= j: continue
                if self._is_contradictory(ci, cj):
                    ci.contradictions.add(cj.candidate_id)
                    cj.contradictions.add(ci.candidate_id)
                    self.contradiction_pairs.append((ci.candidate_id, cj.candidate_id))
        return self.contradiction_pairs

    def _is_contradictory(self, ci: BeliefCandidate, cj: BeliefCandidate) -> bool:
        if abs(ci.belief_score - cj.belief_score) < self.threshold:
            return False
        negations = ["not ", "no ", "false", "wrong", "incorrect", "don't", "do not"]
        pi, pj = ci.proposition.lower(), cj.proposition.lower()
        for neg in negations:
            if (neg in pi) != (neg in pj):
                clean_i = pi.replace(neg, "")
                clean_j = pj.replace(neg, "")
                if clean_i.strip() == clean_j.strip():
                    return True
        return False


class BeliefConsolidationEngine:
    """Resolve contradictions and consolidate beliefs."""
    def __init__(self, contradiction_detector: BeliefContradictionDetector):
        self.detector = contradiction_detector
        self.resolved_count = 0

    def consolidate(self, candidates: List[BeliefCandidate]) -> List[BeliefCandidate]:
        resolved = []
        skipped = set()
        self.detector.detect(candidates)
        for c in candidates:
            if c.candidate_id in skipped:
                continue
            contradictions = c.contradictions
            if contradictions:
                best = max([c] + [x for x in candidates if x.candidate_id in contradictions],
                           key=lambda x: x.belief_score)
                if best.candidate_id != c.candidate_id:
                    skipped.add(c.candidate_id)
                    continue
                c.status = BeliefState.AMBIGUOUS
            elif c.belief_score > 0.7 and c.update_count >= 3:
                c.status = BeliefState.CONFIRMED
            elif c.belief_score < 0.3 and c.update_count >= 2:
                c.status = BeliefState.REFUTED
            resolved.append(c)
        self.resolved_count += len(skipped)
        return resolved

    def stats(self) -> Dict[str, Any]:
        return {"resolved": self.resolved_count}


class BeliefContextWindow:
    """Sliding window of belief context for inference."""
    def __init__(self, window_size: int = 50):
        self.window_size = window_size
        self.window: List[BeliefCandidate] = []

    def push(self, candidate: BeliefCandidate):
        self.window.append(candidate)
        while len(self.window) > self.window_size:
            self.window.pop(0)

    def get_active_context(self) -> List[BeliefCandidate]:
        return [c for c in self.window if c.status in (
            BeliefState.CONFIRMED, BeliefState.HYPOTHESIS, BeliefState.AMBIGUOUS)]

    def query_belief(self, proposition: str) -> Optional[float]:
        for c in reversed(self.window):
            if c.proposition.lower() == proposition.lower():
                return c.belief_score
        return None


class UncertaintyQuantifier:
    """Quantify uncertainty across all active beliefs."""
    def __init__(self):
        self.entropy_history: List[float] = []

    def compute_entropy(self, candidates: List[BeliefCandidate]) -> float:
        if not candidates:
            return 0.0
        total = 0.0
        for c in candidates:
            p = max(0.001, min(0.999, c.belief_score))
            total += -p * math.log2(p) - (1-p) * math.log2(1-p)
        entropy = total / len(candidates)
        self.entropy_history.append(entropy)
        return entropy

    def is_stable(self, threshold: float = 0.1, lookback: int = 5) -> bool:
        if len(self.entropy_history) < lookback:
            return False
        recent = self.entropy_history[-lookback:]
        return max(recent) - min(recent) < threshold


class BelievabilityScorer:
    """Score overall system believability."""
    def __init__(self):
        self.scores: List[float] = []

    def score(self, candidates: List[BeliefCandidate]) -> float:
        if not candidates:
            return 0.5
        total = sum(c.belief_score for c in candidates)
        avg = total / len(candidates)
        self.scores.append(avg)
        return avg


class HypotheticalReasoner:
    """Reason over hypothetical belief states for planning."""
    def __init__(self, query_fn):
        self.query_fn = query_fn
        self.counterfactuals: Dict[str, float] = {}

    def evaluate(self, propositions: List[str],
                 assumed_beliefs: Dict[str, float]) -> Dict[str, float]:
        results = {}
        for p in propositions:
            base = self.query_fn(p) or 0.5
            adjusted = sum(assumed_beliefs.get(p, base) for _ in [None]) / max(1, 1)
            results[p] = adjusted
        self.counterfactuals.update(results)
        return results


class ProbabilisticBeliefMemory:
    """Main orchestrator for probabilistic belief memory system."""
    def __init__(self):
        self.candidates: Dict[str, BeliefCandidate] = {}
        self.updater = NoisyORUpdater()
        self.detector = BeliefContradictionDetector()
        self.consolidator = BeliefConsolidationEngine(self.detector)
        self.context_window = BeliefContextWindow()
        self.uncertainty = UncertaintyQuantifier()
        self.scorer = BelievabilityScorer()

    def assert_belief(self, proposition: str, evidence: List[Dict[str, Any]],
                      prior: float = 0.5) -> BeliefCandidate:
        cid = f"bc_{hash(proposition) % 1000000:06d}"
        if cid in self.candidates:
            c = self.candidates[cid]
        else:
            c = BeliefCandidate(candidate_id=cid, proposition=proposition, prior=prior)
        fragments = [EvidenceFragment(
            source=EvidenceSource[e.get("source", "DIRECT_OBS")],
            description=e.get("description", ""),
            reliability=e.get("reliability", 0.5),
            counter=e.get("counter", False),
        ) for e in evidence]
        self.updater.batch_update([c], fragments)
        self.candidates[cid] = c
        self.context_window.push(c)
        return c

    def query(self, proposition: str) -> Optional[float]:
        return self.context_window.query_belief(proposition)

    def consolidate(self) -> List[BeliefCandidate]:
        active = self.context_window.get_active_context()
        return self.consolidator.consolidate(active)

    def get_state_summary(self) -> Dict[str, Any]:
        active = self.context_window.get_active_context()
        return {
            "total_candidates": len(self.candidates),
            "active": len(active),
            "confirmed": sum(1 for c in active if c.status == BeliefState.CONFIRMED),
            "ambiguous": sum(1 for c in active if c.status == BeliefState.AMBIGUOUS),
            "entropy": self.uncertainty.compute_entropy(active),
            "believability": self.scorer.score(active),
            "stable": self.uncertainty.is_stable(),
        }

    def hypothesize(self, assumptions: Dict[str, float],
                    queries: List[str]) -> Dict[str, float]:
        reasoner = HypotheticalReasoner(self.query)
        return reasoner.evaluate(queries, assumptions)

    def reset_stale(self, max_age: float = 86400):
        """Mark beliefs older than max_age (s) as stale."""
        now = time.time()
        for c in self.candidates.values():
            if now - c.last_updated > max_age:
                c.status = BeliefState.STALE
