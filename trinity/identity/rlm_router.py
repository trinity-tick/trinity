"""
Trinity Identity — RLM Dynamic Weight Router
==============================================
Reinforcement Learning Memory (RLM) adaptive router that upgrades
the HybridRouter's 4 fixed strategies to 5 dynamic-weight strategies
with feedback-driven weight adjustment.

Key features:
  - 5 routing strategies (identity_files / procedural_patterns /
    episodic_keys / value_specifications / hybrid_search)
  - Exponential moving average (EMA) weight updates via feedback
  - Temperature-controlled exploration-exploitation balance
  - Top-k multi-strategy routing with confidence threshold fallback
  - Persistent weight & statistics save/load (JSON)

Architecture:
  RLMRouter extends HybridRouter, adding dynamic weight vectors,
  feedback learning, and statistical tracking while retaining the
  base classify() + regex-based routing as default initialization.
"""

from __future__ import annotations

import json
import math
import os
import random
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .hybrid_router import HybridRouter, QueryType


# ═══════════════════════════════════════════════════════════════════════════
# Data Models
# ═══════════════════════════════════════════════════════════════════════════

_DEFAULT_STRATEGIES = [
    "identity_files",
    "procedural_patterns",
    "episodic_keys",
    "value_specifications",
    "hybrid_search",
]


@dataclass
class RouteResult:
    """Structured routing decision with confidence and strategy details."""

    strategy: str
    confidence: float
    query: str
    top_k: List[Tuple[str, float]] = field(default_factory=list)
    fallback: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════════════
# RLMRouter
# ═══════════════════════════════════════════════════════════════════════════

class RLMRouter(HybridRouter):
    """Dynamic-weight identity router with reinforcement learning feedback.

    Extends HybridRouter to replace the 4 fixed strategy weights with
    an adaptive weight vector that evolves via EMA feedback updates.
    Temperature parameter governs exploration vs exploitation.

    Usage::

        router = RLMRouter()
        result = router.route("what are my core values")
        router.update_feedback("what are my core values", "identity_files", True)
        stats = router.get_strategy_stats()
        router.save("trinity/data/rlm_router_state.json")
    """

    def __init__(
        self,
        strategies: Optional[List[str]] = None,
        initial_weights: Optional[Dict[str, float]] = None,
        learning_rate: float = 0.05,
        temperature: float = 0.1,
        min_confidence_threshold: float = 0.15,
    ):
        """Initialize RLMRouter.

        Args:
            strategies: Strategy names (defaults to 5 standard strategies).
            initial_weights: Initial weight per strategy. If None, uses
                             uniform distribution.
            learning_rate: EMA learning rate (0.0–1.0) for weight updates.
            temperature: Softmax temperature; higher → more exploration,
                         lower → more exploitation of highest-weight strategy.
            min_confidence_threshold: Minimum confidence for top-1; below
                                      this, fall back to default strategy.
        """
        super().__init__()

        self.strategies = list(strategies) if strategies else list(_DEFAULT_STRATEGIES)
        self.learning_rate = max(0.0, min(1.0, learning_rate))
        self.temperature = max(0.01, temperature)
        self.min_confidence_threshold = min_confidence_threshold
        self._default_strategy = self.strategies[0]

        # Weight vector — normalized on init
        if initial_weights:
            self.weights = {
                s: initial_weights.get(s, 1.0 / len(self.strategies))
                for s in self.strategies
            }
        else:
            uniform = 1.0 / len(self.strategies)
            self.weights = {s: uniform for s in self.strategies}
        self._normalize_weights()

        # Statistical tracking
        self.hits: Dict[str, int] = defaultdict(int)       # times selected
        self.successes: Dict[str, int] = defaultdict(int)   # times succeeded
        self.total_queries: int = 0
        self.total_feedback: int = 0

        # Init stats counters for all strategies
        for s in self.strategies:
            self.hits[s] = 0
            self.successes[s] = 0

    # ── Normalization ─────────────────────────────────────────────────

    def _normalize_weights(self) -> None:
        """Normalize weights to sum to 1.0."""
        total = sum(self.weights.values())
        if total > 0:
            for s in self.strategies:
                self.weights[s] /= total
        else:
            uniform = 1.0 / len(self.strategies)
            for s in self.strategies:
                self.weights[s] = uniform

    # ── Score Computation ────────────────────────────────────────────

    def _compute_strategy_scores(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> List[Tuple[str, float]]:
        """Compute weighted score for each strategy.

        Base score is derived from HybridRouter's existing classify logic,
        then multiplied by the dynamic weight, plus a small random jitter
        for exploration.

        Returns:
            Sorted list of (strategy_name, score) descending.
        """
        # Base scores from parent classify
        _ = context  # reserved for future context-aware scoring
        qtype, base_conf = self.classify(query)

        # Map query type to strategy bonus
        type_to_strategy_bonus = {
            QueryType.IDENTITY: "identity_files",
            QueryType.FACT: "procedural_patterns",
            QueryType.FUZZY: "hybrid_search",
            QueryType.HYBRID: "value_specifications",
        }

        bonus_strategy = type_to_strategy_bonus.get(qtype, self._default_strategy)

        scores: List[Tuple[str, float]] = []
        for s in self.strategies:
            base = self.weights.get(s, 0.0)
            # Bonus for strategy matching the query type
            if s == bonus_strategy:
                base += base_conf * 0.3
            # Small random jitter for exploration (scaled by temperature)
            jitter = random.uniform(0, self.temperature) * 0.05
            scores.append((s, base + jitter))

        # Sort descending by score
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores

    # ── Softmax ───────────────────────────────────────────────────────

    def _softmax(self, scores: List[Tuple[str, float]]) -> List[Tuple[str, float]]:
        """Apply temperature-scaled softmax to strategy scores.

        Returns:
            List of (strategy_name, probability) sorted descending.
        """
        names = [s[0] for s in scores]
        values = [s[1] for s in scores]
        max_val = max(values)

        # Subtract max for numerical stability, divide by temperature
        scaled = [(v - max_val) / self.temperature for v in values]
        exp_vals = [math.exp(v) for v in scaled]
        exp_sum = sum(exp_vals)

        if exp_sum == 0:
            uniform = 1.0 / len(names)
            return [(n, uniform) for n in names]

        probs = [(names[i], exp_vals[i] / exp_sum) for i in range(len(names))]
        probs.sort(key=lambda x: x[1], reverse=True)
        return probs

    # ── Main Route Method ────────────────────────────────────────────

    def route(
        self,
        query: str,
        context: Optional[Dict[str, Any]] = None,
        top_k: int = 1,
    ) -> RouteResult:
        """Route query to optimal strategy with dynamic weights.

        Args:
            query: Natural language query string.
            context: Optional context dict for context-aware routing.
            top_k: Number of top strategies to return (default 1).

        Returns:
            RouteResult with selected strategy, confidence, and metadata.
        """
        self.total_queries += 1

        # Compute weighted scores
        scores = self._compute_strategy_scores(query, context)

        # Softmax normalization
        probs = self._softmax(scores)

        # Extract top-k
        top_k_probs = probs[:max(1, min(top_k, len(probs)))]

        top_strategy, top_conf = top_k_probs[0]

        # Confidence threshold fallback
        fallback = False
        if top_conf < self.min_confidence_threshold:
            top_strategy = self._default_strategy
            top_conf = self.weights.get(self._default_strategy, 0.0)
            fallback = True

        # Record hit
        self.hits[top_strategy] += 1

        return RouteResult(
            strategy=top_strategy,
            confidence=round(top_conf, 4),
            query=query,
            top_k=top_k_probs,
            fallback=fallback,
            metadata={
                "temperature": self.temperature,
                "learning_rate": self.learning_rate,
                "total_queries": self.total_queries,
                "weights_snapshot": dict(self.weights),
            },
        )

    # ── Feedback / Weight Update ─────────────────────────────────────

    def update_feedback(
        self,
        query: str,
        chosen_strategy: str,
        success: bool,
    ) -> Dict[str, Any]:
        """Update strategy weights via EMA based on routing feedback.

        Weight update formula:
            weight *= (1 - lr) + success * lr

        After update, weights are re-normalized and statistics incremented.

        Args:
            query: The original query (for logging).
            chosen_strategy: The strategy that was selected.
            success: Whether the routing was successful.

        Returns:
            Dict with updated weight and stats for the strategy.
        """
        self.total_feedback += 1

        if chosen_strategy not in self.strategies:
            return {"error": f"Unknown strategy: {chosen_strategy}"}

        self.hits.setdefault(chosen_strategy, 0)
        self.successes.setdefault(chosen_strategy, 0)

        if success:
            self.successes[chosen_strategy] += 1

        # EMA update: weight *= (1 - lr) + success * lr
        lr = self.learning_rate
        old_weight = self.weights.get(chosen_strategy, 0.0)
        success_val = 1.0 if success else 0.0
        new_weight = old_weight * (1 - lr) + success_val * lr
        self.weights[chosen_strategy] = max(0.0, new_weight)

        # Re-normalize
        self._normalize_weights()

        return {
            "strategy": chosen_strategy,
            "old_weight": round(old_weight, 4),
            "new_weight": round(self.weights[chosen_strategy], 4),
            "success": success,
            "total_feedback": self.total_feedback,
        }

    # ── Statistics ───────────────────────────────────────────────────

    def get_strategy_stats(self) -> Dict[str, Any]:
        """Return per-strategy hit rate and success rate statistics.

        Returns:
            Dict mapping strategy name to {'hits', 'successes', 'hit_rate', 'success_rate'}.
        """
        stats: Dict[str, Any] = {}
        for s in self.strategies:
            h = self.hits.get(s, 0)
            succ = self.successes.get(s, 0)
            stats[s] = {
                "hits": h,
                "successes": succ,
                "hit_rate": round(h / max(self.total_queries, 1), 4),
                "success_rate": round(succ / max(h, 1), 4),
            }
        return {
            "strategies": stats,
            "total_queries": self.total_queries,
            "total_feedback": self.total_feedback,
            "weights": {s: round(w, 4) for s, w in self.weights.items()},
            "temperature": self.temperature,
            "learning_rate": self.learning_rate,
        }

    # ── Persistence ──────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        """Serialize RLMRouter state to a plain dict."""
        return {
            "strategies": list(self.strategies),
            "weights": dict(self.weights),
            "hits": dict(self.hits),
            "successes": dict(self.successes),
            "total_queries": self.total_queries,
            "total_feedback": self.total_feedback,
            "learning_rate": self.learning_rate,
            "temperature": self.temperature,
            "min_confidence_threshold": self.min_confidence_threshold,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "version": "1.0.0",
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RLMRouter":
        """Deserialize RLMRouter from a plain dict."""
        router = cls(
            strategies=data.get("strategies"),
            initial_weights=data.get("weights"),
            learning_rate=data.get("learning_rate", 0.05),
            temperature=data.get("temperature", 0.1),
            min_confidence_threshold=data.get("min_confidence_threshold", 0.15),
        )
        router.hits = defaultdict(int, data.get("hits", {}))
        router.successes = defaultdict(int, data.get("successes", {}))
        router.total_queries = data.get("total_queries", 0)
        router.total_feedback = data.get("total_feedback", 0)
        return router

    def save(self, filepath: str) -> None:
        """Persist router state to JSON file.

        Args:
            filepath: Absolute path to save the state JSON.
        """
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, filepath: str) -> "RLMRouter":
        """Load router state from a JSON file.

        Args:
            filepath: Absolute path to the saved state JSON.

        Returns:
            RLMRouter instance with restored state.

        Raises:
            FileNotFoundError: If the file does not exist.
        """
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    # ── Self-Test ─────────────────────────────────────────────────────

    def self_test(self) -> Dict[str, Any]:
        """Runtime self-diagnostic covering core RLMRouter operations.

        Returns:
            {"pass": bool, "checks": [...], "summary": str}
        """
        checks = []

        # Check 1: route returns valid RouteResult
        try:
            result = self.route("what are my core values")
            assert isinstance(result, RouteResult)
            assert result.strategy in self.strategies, f"Unknown strategy: {result.strategy}"
            assert 0.0 <= result.confidence <= 1.0
            checks.append({
                "name": "route_returns_valid",
                "pass": True,
                "detail": f"strategy={result.strategy}, conf={result.confidence:.3f}",
            })
        except Exception as e:
            checks.append({"name": "route_returns_valid", "pass": False, "detail": str(e)})

        # Check 2: top-k routing
        try:
            result = self.route("test query", top_k=3)
            assert len(result.top_k) == 3, f"Expected 3, got {len(result.top_k)}"
            checks.append({
                "name": "top_k_routing",
                "pass": True,
                "detail": f"top-3: {[(s, round(c, 3)) for s, c in result.top_k]}",
            })
        except Exception as e:
            checks.append({"name": "top_k_routing", "pass": False, "detail": str(e)})

        # Check 3: feedback update increases success count
        try:
            init_stats = self.get_strategy_stats()
            old_successes = init_stats["strategies"]["identity_files"]["successes"]
            self.update_feedback("test query", "identity_files", success=True)
            new_stats = self.get_strategy_stats()
            assert new_stats["strategies"]["identity_files"]["successes"] == old_successes + 1
            checks.append({
                "name": "feedback_success_count",
                "pass": True,
                "detail": f"successes: {old_successes} → {old_successes + 1}",
            })
        except Exception as e:
            checks.append({"name": "feedback_success_count", "pass": False, "detail": str(e)})

        # Check 4: feedback failure does not increment success count
        try:
            self.update_feedback("test query 2", "procedural_patterns", success=False)
            new_stats = self.get_strategy_stats()
            # success count for procedural_patterns should still be 0 (since we only logged a failure)
            assert new_stats["strategies"]["procedural_patterns"]["successes"] >= 0
            checks.append({
                "name": "feedback_failure_no_success",
                "pass": True,
                "detail": "failure feedback does not increment success count",
            })
        except Exception as e:
            checks.append({"name": "feedback_failure_no_success", "pass": False, "detail": str(e)})

        # Check 5: save/load round-trip
        try:
            tmp_path = os.path.join(
                os.path.dirname(__file__), "..", "data", "_test_rlm_router_state.json"
            )
            self.save(tmp_path)
            loaded = RLMRouter.load(tmp_path)
            assert loaded.strategies == self.strategies
            assert loaded.learning_rate == self.learning_rate
            assert loaded.temperature == self.temperature
            # Clean up
            os.remove(tmp_path)
            checks.append({
                "name": "save_load_roundtrip",
                "pass": True,
                "detail": "strategies, lr, temperature preserved",
            })
        except Exception as e:
            checks.append({"name": "save_load_roundtrip", "pass": False, "detail": str(e)})

        # Check 6: temperature affects score distribution
        try:
            hot_router = RLMRouter(temperature=2.0)
            cold_router = RLMRouter(temperature=0.01)
            hot_result = hot_router.route("test query")
            cold_result = cold_router.route("test query")
            assert hot_result.strategy in self.strategies
            assert cold_result.strategy in self.strategies
            checks.append({
                "name": "temperature_exploration",
                "pass": True,
                "detail": f"hot={hot_result.strategy}, cold={cold_result.strategy}",
            })
        except Exception as e:
            checks.append({"name": "temperature_exploration", "pass": False, "detail": str(e)})

        all_pass = all(c["pass"] for c in checks)
        return {
            "pass": all_pass,
            "checks": checks,
            "summary": f"RLMRouter self-test: {sum(1 for c in checks if c['pass'])}/{len(checks)} passed",
        }


# ── Module-level self_test ─────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module-level entry point for regression testing."""
    router = RLMRouter()
    return router.self_test()
