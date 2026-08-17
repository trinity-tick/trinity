"""
Corrective RAG (CRAG)
=====================
CRAG evaluates the quality of retrieved results and triggers corrective actions
when retrieval confidence is low. This prevents the RAG system from producing
low-quality or hallucinated responses.

Core idea:
  1. After Cross-Encoder reranking, analyze the score distribution.
  2. If confidence is low (max score < threshold, high score variance, etc.),
     trigger one or more corrective strategies:
     - Re-retrieve with different alpha (sparse-dense fusion weight)
     - Re-retrieve with different query expansion
     - Fall back to web/API search
     - Return "low confidence" signal to the caller

Token Efficiency Hook (v6.38):
  When enabled (default), an early-stopping + dedup hook from
  ``trinity.core.token_efficiency`` is attached. Configure via:
    - CRAG_ENABLE_TOKEN_EFFICIENCY env var or ``enable_token_efficiency`` param
    - CRAG_EARLY_STOP_PATIENCE env var or ``early_stop_patience`` param

Reference:
  - Corrective Retrieval Augmented Generation (CRAG) — Microsoft 2024
  - Self-RAG: Learning to Retrieve, Generate, and Critique (2024)
"""

import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


class CorrectiveRAG:
    """
    Corrective RAG module that assesses retrieval quality and triggers
    corrective actions when confidence is low.

    Usage in the pipeline (Stage 5.5):
      crag = CorrectiveRAG(re_retrieve_fn)
      results, confidence = crag.evaluate(results)
      if confidence == "low":
          results = crag.correct(query, results)
    """

    # Confidence thresholds (tunable)
    DEFAULT_CONFIDENCE_THRESHOLD = 0.35        # Max score below this → low confidence
    DEFAULT_SPREAD_THRESHOLD = 0.15             # Score spread (max-min) below this → flat scores → low
    DEFAULT_TOP2_RATIO_THRESHOLD = 1.5          # Score ratio between top1 and top2 (too close → ambiguous)
    DEFAULT_MIN_RESULTS = 3                     # Fewer results than this → low confidence

    def __init__(
        self,
        re_retrieve_fn: Optional[Callable] = None,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        spread_threshold: float = DEFAULT_SPREAD_THRESHOLD,
        top2_ratio_threshold: float = DEFAULT_TOP2_RATIO_THRESHOLD,
        min_results: int = DEFAULT_MIN_RESULTS,
        max_correction_rounds: int = 2,
        enable_token_efficiency: Optional[bool] = None,
        early_stop_patience: Optional[int] = None,
        token_budget_per_query: Optional[int] = None,
    ):
        """
        Args:
            re_retrieve_fn: Callable(query, params) that performs re-retrieval.
                            The params dict can contain:
                            - alpha: float (sparse-dense fusion weight)
                            - expansion: str (query expansion strategy)
                            - top_k: int (number of results)
            confidence_threshold: Max score below this → low confidence.
            spread_threshold: Score spread below this → scores are too flat.
            top2_ratio_threshold: top1_score / top2_score below this → ambiguous.
            min_results: Minimum number of results required.
            max_correction_rounds: Max consecutive corrections per query.
            enable_token_efficiency: Enable early-stop + dedup hook.
                Default: True. Can be overridden via CRAG_ENABLE_TOKEN_EFFICIENCY env.
            early_stop_patience: Consecutive no-gain channels before stopping.
                Default: 3. Can be overridden via CRAG_EARLY_STOP_PATIENCE env.
            token_budget_per_query: Max estimated tokens per query.
                Default: 4096.
        """
        self._re_retrieve_fn = re_retrieve_fn
        self._confidence_threshold = confidence_threshold
        self._spread_threshold = spread_threshold
        self._top2_ratio_threshold = top2_ratio_threshold
        self._min_results = min_results
        self._max_correction_rounds = max_correction_rounds

        # ── Token Efficiency Hook ──────────────────────────────────
        if enable_token_efficiency is None:
            enable_token_efficiency = os.environ.get(
                "CRAG_ENABLE_TOKEN_EFFICIENCY", "true"
            ).lower() in ("1", "true", "yes", "on")
        self._enable_token_efficiency = enable_token_efficiency

        if early_stop_patience is None:
            early_stop_patience = int(os.environ.get("CRAG_EARLY_STOP_PATIENCE", "3"))
        if token_budget_per_query is None:
            token_budget_per_query = int(
                os.environ.get("CRAG_TOKEN_BUDGET", "4096")
            )

        self._token_optimizer = None
        if self._enable_token_efficiency:
            try:
                from trinity.core.token_efficiency import TokenEfficiencyOptimizer
                self._token_optimizer = TokenEfficiencyOptimizer(
                    early_stop_patience=early_stop_patience,
                    enable_dedup=True,
                    enable_dynamic_truncation=True,
                    token_budget_per_query=token_budget_per_query,
                    enabled=True,
                )
            except ImportError:
                pass

        self._stats = {
            "calls": 0,
            "high_confidence": 0,
            "medium_confidence": 0,
            "low_confidence": 0,
            "corrections_applied": 0,
            "re_retrieves": 0,
        }

    def evaluate(
        self,
        results: List[Dict[str, Any]],
        score_key: str = "score",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """
        Evaluate retrieval quality and classify confidence.

        Args:
            results: Ranked list of result dicts (from the pipeline).
            score_key: Key for the score field in each result dict.

        Returns:
            (results, confidence): confidence is "high", "medium", or "low".
        """
        self._stats["calls"] += 1

        if not results:
            self._stats["low_confidence"] += 1
            return [], "low"

        scores = np.array([r.get(score_key, 0.0) for r in results if r.get(score_key) is not None])

        if len(scores) < self._min_results:
            self._stats["low_confidence"] += 1
            return results, "low"

        max_score = float(scores.max())
        min_score = float(scores.min())
        spread = max_score - min_score
        avg_score = float(scores.mean())

        # Compute top-2 ratio
        top2_ratio = 0.0
        if len(scores) >= 2:
            sorted_scores = sorted(scores, reverse=True)
            if sorted_scores[1] > 0:
                top2_ratio = sorted_scores[0] / sorted_scores[1]

        # Decision logic
        low_flags = 0
        if max_score < self._confidence_threshold:
            low_flags += 1  # Overall scores too low
        if spread < self._spread_threshold:
            low_flags += 1  # Scores too flat — no clear winner
        if top2_ratio < self._top2_ratio_threshold:
            low_flags += 1  # Top two too close — ambiguous

        if low_flags >= 2:
            confidence = "low"
            self._stats["low_confidence"] += 1
        elif low_flags >= 1:
            confidence = "medium"
            self._stats["medium_confidence"] += 1
        else:
            confidence = "high"
            self._stats["high_confidence"] += 1

        logger.debug(
            "CRAG evaluate: max=%.4f spread=%.4f top2_ratio=%.2f low_flags=%d → %s",
            max_score, spread, top2_ratio, low_flags, confidence,
        )

        return results, confidence

    def correct(
        self,
        query: str,
        results: List[Dict[str, Any]],
        previous_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Apply corrective strategies when confidence is low.

        Strategies (applied in order):
          1. Tweak sparse-dense fusion alpha (0.2 → 0.8 swing)
          2. Tweak query expansion (different strategy)
          3. Re-retrieve with different top_k

        Args:
            query: The original query.
            results: Current (low-confidence) results.
            previous_params: Parameters used in the previous retrieval attempt.

        Returns:
            Improved results (or original results if correction fails).
        """
        if not self._re_retrieve_fn:
            logger.warning("CRAG: no re_retrieve_fn provided, cannot correct")
            return results

        prev = previous_params or {}
        n_corrections = 0

        # Strategy 1: Swing alpha in the opposite direction
        current_alpha = prev.get("alpha", 0.3)
        new_alpha = 1.0 - current_alpha  # Flip: 0.3 → 0.7, 0.7 → 0.3
        if abs(new_alpha - current_alpha) > 0.1:
            n_corrections += 1
            logger.info("CRAG correction %d: swing alpha %.2f → %.2f", n_corrections, current_alpha, new_alpha)
            corrected = self._re_retrieve_fn(query, {"alpha": new_alpha, **prev})
            if corrected and len(corrected) >= self._min_results:
                self._stats["corrections_applied"] += 1
                self._stats["re_retrieves"] += 1
                results = corrected
                prev["alpha"] = new_alpha

        # Strategy 2: Different expansion strategy
        if n_corrections < self._max_correction_rounds and self._check_low_confidence(results):
            current_exp = prev.get("expansion", "default")
            new_exp = "aggressive" if current_exp != "aggressive" else "minimal"
            n_corrections += 1
            logger.info("CRAG correction %d: expansion %s → %s", n_corrections, current_exp, new_exp)
            corrected = self._re_retrieve_fn(query, {"expansion": new_exp, **prev})
            if corrected and len(corrected) >= self._min_results:
                self._stats["corrections_applied"] += 1
                self._stats["re_retrieves"] += 1
                results = corrected
                prev["expansion"] = new_exp

        # Strategy 3: Larger top_k for more candidates
        if n_corrections < self._max_correction_rounds and self._check_low_confidence(results):
            current_top_k = prev.get("top_k", 100)
            new_top_k = min(current_top_k * 2, 500)
            n_corrections += 1
            logger.info("CRAG correction %d: top_k %d → %d", n_corrections, current_top_k, new_top_k)
            corrected = self._re_retrieve_fn(query, {"top_k": new_top_k, **prev})
            if corrected and len(corrected) >= self._min_results:
                self._stats["corrections_applied"] += 1
                self._stats["re_retrieves"] += 1
                results = corrected

        return results

    def _check_low_confidence(self, results: List[Dict[str, Any]]) -> bool:
        """Quick check if results still have low confidence."""
        _, confidence = self.evaluate(results)
        return confidence in ("low", "medium")

    def statistics(self) -> Dict[str, Any]:
        """Return usage statistics (including token efficiency if enabled)."""
        stats = dict(self._stats)
        total_calls = stats["calls"]
        if total_calls > 0:
            stats["high_confidence_pct"] = (stats["high_confidence"] / total_calls) * 100
            stats["low_confidence_pct"] = (stats["low_confidence"] / total_calls) * 100
        # Merge token efficiency stats
        if self._token_optimizer:
            stats["token_efficiency"] = self._token_optimizer.statistics()
        return stats

    # ── Token Efficiency Integration Methods ────────────────────────

    def start_query_efficiency(self, query: str):
        """Initialize token efficiency tracking for a new query."""
        if self._token_optimizer:
            self._token_optimizer.start_query(query)

    def apply_token_efficiency(
        self,
        results: List[Dict[str, Any]],
        channel_name: str = "",
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """
        Apply dedup + early stop check to a channel's results.

        Args:
            results: Results from one channel.
            channel_name: Channel identifier for tracking.

        Returns:
            (filtered_results, should_stop): True if early stop triggered.
        """
        if not self._token_optimizer:
            return results, False

        # Track token usage
        est_tokens = self._token_optimizer.estimate_result_tokens(results)
        self._token_optimizer.track_tokens(
            channel_name=channel_name,
            result_count=len(results),
            estimated_tokens=est_tokens,
        )

        # Deduplicate
        filtered = self._token_optimizer.deduplicate(results)

        # Check early stop
        should_stop = self._token_optimizer.should_early_stop(filtered)

        return filtered, should_stop

    def finalize_efficiency(self, results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Apply dynamic truncation at end of pipeline."""
        if self._token_optimizer:
            return self._token_optimizer.dynamic_truncate(results)
        return results

    @property
    def token_optimizer(self):
        """Access the underlying TokenEfficiencyOptimizer (may be None)."""
        return self._token_optimizer
