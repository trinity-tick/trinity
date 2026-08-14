"""
Token Efficiency Optimizer for Trinity Retrieval Pipeline
=========================================================

Provides four optimization strategies to reduce token consumption
during the 47-channel cascade retrieval process:

1. **Early Stopping**: Terminates retrieval when N consecutive channels
   produce no relevance gain, avoiding wasteful channel processing.

2. **Channel Deduplication**: Cross-channel result deduplication using
   content fingerprinting to prevent redundant content from consuming tokens.

3. **Dynamic Truncation**: Automatically adjusts result count based on
   query complexity (simple → top-3, medium → top-5, complex → top-10).

4. **Token Budget Tracker**: Records estimated token consumption per
   channel and per query, with configurable budget limits.

Integration:
    from trinity.core.token_efficiency import TokenEfficiencyOptimizer

    optimizer = TokenEfficiencyOptimizer(
        early_stop_patience=3,      # stop after 3 channels with no gain
        enable_dedup=True,          # cross-channel dedup
        enable_dynamic_truncation=True,
        token_budget_per_query=4096,
    )

    # Hook into retrieval loop:
    for channel_name, results in channel_generator:
        optimizer.track_tokens(channel_name, len(results), estimated_tokens)
        results = optimizer.deduplicate(results)
        if optimizer.should_early_stop(results):
            break
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Query Complexity Patterns ─────────────────────────────────────────

_SIMPLE_PATTERNS = re.compile(
    r"^(what|who|when|where|which|how many|how much|"
    r"define|definition of|meaning of|what is|what are|"
    r"list|show me|find|get|fetch)\s",
    re.IGNORECASE,
)

_COMPLEX_PATTERNS = re.compile(
    r"(compare|contrast|analyze|explain why|how does|how do|"
    r"relationship between|difference between|similarities|"
    r"evaluate|assess|summarize|elaborate|in detail|"
    r"pros and cons|advantages|disadvantages)",
    re.IGNORECASE,
)

# Token estimation: ~1.3 tokens per English word, ~2.5 per Chinese char
_TOKENS_PER_WORD_EN = 1.3
_TOKENS_PER_CHAR_ZH = 2.5
_OVERHEAD_TOKENS_PER_RESULT = 20  # metadata overhead per result dict


@dataclass
class ChannelRecord:
    """Per-channel tracking record."""
    channel_name: str
    result_count: int
    estimated_tokens: int
    unique_results_added: int
    relevance_gain: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TokenBudget:
    """Token budget tracking."""
    limit: int
    used: int = 0
    channel_records: List[ChannelRecord] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def usage_pct(self) -> float:
        return (self.used / self.limit * 100) if self.limit > 0 else 0.0


class TokenEfficiencyOptimizer:
    """
    Token efficiency optimizer for Trinity's multi-channel retrieval.

    Configurable via constructor parameters; all features can be
    toggled on/off independently.

    Usage::

        optimizer = TokenEfficiencyOptimizer(
            early_stop_patience=3,
            enable_dedup=True,
            enable_dynamic_truncation=True,
            token_budget_per_query=4096,
        )

        budget = optimizer.start_query(query)

        for channel_name, results in channel_pipeline:
            estimated = optimizer.estimate_tokens(results)
            budget = optimizer.track_tokens(channel_name, len(results), estimated)

            if optimizer.enable_dedup:
                results = optimizer.deduplicate(results)

            if budget.remaining <= 0:
                logger.warning("Token budget exhausted after %s", channel_name)
                break

            if optimizer.should_early_stop(results):
                logger.info("Early stop triggered at %s", channel_name)
                break

        if optimizer.enable_dynamic_truncation:
            results = optimizer.dynamic_truncate(results)

        stats = optimizer.statistics()
    """

    def __init__(
        self,
        early_stop_patience: int = 3,
        early_stop_min_gain: float = 0.05,
        enable_dedup: bool = True,
        dedup_similarity_threshold: float = 0.85,
        enable_dynamic_truncation: bool = True,
        simple_query_top_k: int = 3,
        medium_query_top_k: int = 5,
        complex_query_top_k: int = 10,
        token_budget_per_query: int = 4096,
        enabled: bool = True,
    ):
        """
        Args:
            early_stop_patience: Number of consecutive channels with no
                relevance gain before early stopping.
            early_stop_min_gain: Minimum score increase to count as "gain."
            enable_dedup: Enable cross-channel content deduplication.
            dedup_similarity_threshold: Jaccard / fingerprint similarity
                threshold above which results are considered duplicates.
            enable_dynamic_truncation: Auto-adjust top-k by query complexity.
            simple_query_top_k: Top-k for simple queries.
            medium_query_top_k: Top-k for medium queries.
            complex_query_top_k: Top-k for complex queries.
            token_budget_per_query: Max estimated tokens per query.
            enabled: Master switch; when False, all methods are no-ops.
        """
        self.enabled = enabled

        # Early stopping
        self.early_stop_patience = early_stop_patience
        self.early_stop_min_gain = early_stop_min_gain
        self._consecutive_no_gain = 0
        self._best_score = 0.0

        # Deduplication
        self.enable_dedup = enable_dedup
        self.dedup_similarity_threshold = dedup_similarity_threshold
        self._seen_fingerprints: OrderedDict = OrderedDict()
        self._dedup_count = 0

        # Dynamic truncation
        self.enable_dynamic_truncation = enable_dynamic_truncation
        self.simple_query_top_k = simple_query_top_k
        self.medium_query_top_k = medium_query_top_k
        self.complex_query_top_k = complex_query_top_k

        # Token budget
        self.token_budget_per_query = token_budget_per_query
        self._budget: Optional[TokenBudget] = None
        self._current_query: Optional[str] = None

        # Stats
        self._stats: Dict[str, Any] = {
            "total_queries": 0,
            "total_channels_processed": 0,
            "total_channels_skipped": 0,
            "total_results_before": 0,
            "total_results_after": 0,
            "total_tokens_saved": 0,
            "total_dedup_removed": 0,
            "early_stops_triggered": 0,
            "budget_exhaustions": 0,
        }

    # ═══════════════════════════════════════════════════════════════════
    #  Query Complexity Classification
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def classify_complexity(query: str) -> str:
        """
        Classify query complexity: 'simple', 'medium', or 'complex'.

        Heuristics:
        - Word count, presence of comparison/analysis keywords,
          question depth indicators.
        """
        if not query or not isinstance(query, str):
            return "medium"

        query_clean = query.strip()
        word_count = len(query_clean.split())

        # Check complex patterns
        if _COMPLEX_PATTERNS.search(query_clean):
            return "complex"

        # Long queries are complex
        if word_count > 15:
            return "complex"

        # Multi-sentence or multi-clause
        if any(sep in query_clean for sep in ("?", ";", "\n")):
            clauses = [c.strip() for c in re.split(r"[?;\n]+", query_clean) if c.strip()]
            if len(clauses) >= 2:
                return "complex"

        # Medium: moderate length or has some structure
        if word_count > 8:
            return "medium"
        if any(kw in query_clean.lower() for kw in (
            "describe", "explain", "how", "why", "tell me about",
        )):
            return "medium"

        # Simple: short, direct questions
        if word_count <= 8 or _SIMPLE_PATTERNS.search(query_clean):
            return "simple"

        return "medium"

    @staticmethod
    def _count_chinese_chars(text: str) -> int:
        """Count Chinese characters in text."""
        return sum(1 for c in text if '\u4e00' <= c <= '\u9fff')

    def estimate_tokens(self, text: str) -> int:
        """
        Estimate token count for a text string.
        Uses ~1.3 tokens/word for English, ~2.5 for Chinese characters.
        """
        if not text:
            return 0
        en_words = len(text.split())
        zh_chars = self._count_chinese_chars(text)
        # Deduplicate: some words may be counted in both
        en_only = max(0, en_words - zh_chars)
        return int(en_only * _TOKENS_PER_WORD_EN + zh_chars * _TOKENS_PER_CHAR_ZH)

    def estimate_result_tokens(self, results: List[Dict[str, Any]]) -> int:
        """Estimate tokens for a list of result dicts."""
        if not results:
            return 0
        total = 0
        for r in results:
            content = r.get("content", r.get("content_preview", ""))
            total += self.estimate_tokens(str(content))
            total += _OVERHEAD_TOKENS_PER_RESULT
        return total

    # ═══════════════════════════════════════════════════════════════════
    #  Query Lifecycle
    # ═══════════════════════════════════════════════════════════════════

    def start_query(self, query: str) -> TokenBudget:
        """Initialize token budget and state for a new query."""
        if not self.enabled:
            return TokenBudget(limit=0)

        self._budget = TokenBudget(limit=self.token_budget_per_query)
        self._current_query = query
        self._consecutive_no_gain = 0
        self._best_score = 0.0
        self._seen_fingerprints.clear()
        self._dedup_count = 0
        self._stats["total_queries"] += 1
        return self._budget

    def end_query(self) -> TokenBudget:
        """Finalize query and return budget summary."""
        return self._budget or TokenBudget(limit=0)

    # ═══════════════════════════════════════════════════════════════════
    #  Early Stopping
    # ═══════════════════════════════════════════════════════════════════

    def should_early_stop(self, results: List[Dict[str, Any]]) -> bool:
        """
        Determine whether to stop processing further channels.

        Returns True if the last N consecutive channels produced no
        meaningful relevance gain over the best-seen results.
        """
        if not self.enabled or not results:
            return False

        # Compute best score from current batch
        scores = [
            r.get("score", r.get("relevance", 0.0))
            for r in results
            if r.get("score") is not None or r.get("relevance") is not None
        ]
        current_best = max(scores) if scores else 0.0

        gain = current_best - self._best_score
        if gain > self.early_stop_min_gain and current_best > self._best_score:
            # Relevance gain detected
            self._consecutive_no_gain = 0
            self._best_score = max(self._best_score, current_best)
            return False

        self._consecutive_no_gain += 1
        if self._consecutive_no_gain >= self.early_stop_patience:
            self._stats["early_stops_triggered"] += 1
            return True

        return False

    def register_channel_no_gain(self):
        """Manually register a 'no gain' tick (e.g., channel returned empty)."""
        if not self.enabled:
            return
        self._consecutive_no_gain += 1
        if self._consecutive_no_gain >= self.early_stop_patience:
            self._stats["early_stops_triggered"] += 1

    # ═══════════════════════════════════════════════════════════════════
    #  Channel Deduplication
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _content_fingerprint(text: str) -> str:
        """Generate a normalized content fingerprint for dedup."""
        if not text:
            return ""
        # Normalize: lowercase, strip whitespace, remove punctuation
        normalized = re.sub(r"[^\w\s]", "", text.lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        if len(normalized) < 20:
            # Short text: use full fuzzy hash
            return hashlib.sha256(normalized.encode()).hexdigest()[:16]
        # Long text: use first 100 chars as fingerprint
        return hashlib.sha256(normalized[:100].encode()).hexdigest()[:16]

    def _is_duplicate(self, text: str) -> bool:
        """Check if content is a duplicate of previously seen content."""
        fp = self._content_fingerprint(text)
        if not fp:
            return False

        # Exact fingerprint match
        if fp in self._seen_fingerprints:
            return True

        # Fuzzy similarity check on short texts
        for seen_fp in list(self._seen_fingerprints.keys())[-50:]:  # Check last 50
            if self._fingerprint_similarity(fp, seen_fp) >= self.dedup_similarity_threshold:
                return True

        return False

    @staticmethod
    def _fingerprint_similarity(fp1: str, fp2: str) -> float:
        """Compute hex-digit overlap ratio between two fingerprints."""
        if len(fp1) != len(fp2):
            return 0.0
        matches = sum(1 for a, b in zip(fp1, fp2) if a == b)
        return matches / len(fp1) if fp1 else 0.0

    def deduplicate(
        self, results: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Remove duplicate results across channels based on content fingerprint.

        Preserves order and keeps the first occurrence (highest-ranked).
        """
        if not self.enabled or not self.enable_dedup or not results:
            return results

        unique = []
        for r in results:
            content = r.get("content", r.get("content_preview", ""))
            fp = self._content_fingerprint(str(content))

            if fp and fp in self._seen_fingerprints:
                self._dedup_count += 1
                continue

            # Fuzzy check
            is_dup = False
            for seen_fp in list(self._seen_fingerprints.keys())[-50:]:
                if self._fingerprint_similarity(fp, seen_fp) >= self.dedup_similarity_threshold:
                    is_dup = True
                    break
            if is_dup:
                self._dedup_count += 1
                continue

            unique.append(r)
            if fp:
                self._seen_fingerprints[fp] = True

        self._stats["total_dedup_removed"] += self._dedup_count
        return unique

    # ═══════════════════════════════════════════════════════════════════
    #  Dynamic Truncation
    # ═══════════════════════════════════════════════════════════════════

    def get_top_k(self, query: str) -> int:
        """Get appropriate top-k based on query complexity."""
        if not self.enabled or not self.enable_dynamic_truncation:
            return self.complex_query_top_k  # default to max

        complexity = self.classify_complexity(query)
        if complexity == "simple":
            return self.simple_query_top_k
        elif complexity == "complex":
            return self.complex_query_top_k
        else:
            return self.medium_query_top_k

    def dynamic_truncate(
        self, results: List[Dict[str, Any]], query: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Truncate results based on query complexity.
        """
        if not self.enabled or not self.enable_dynamic_truncation:
            return results

        q = query or self._current_query or ""
        top_k = self.get_top_k(q)

        before_count = len(results)
        truncated = results[:top_k]
        after_count = len(truncated)

        if before_count != after_count:
            self._stats["total_results_before"] += before_count
            self._stats["total_results_after"] += after_count
            tokens_saved = self.estimate_result_tokens(results[top_k:])
            self._stats["total_tokens_saved"] += tokens_saved

        return truncated

    # ═══════════════════════════════════════════════════════════════════
    #  Token Budget Tracker
    # ═══════════════════════════════════════════════════════════════════

    def track_tokens(
        self,
        channel_name: str,
        result_count: int,
        estimated_tokens: int,
        unique_added: int = 0,
        relevance_gain: float = 0.0,
    ) -> TokenBudget:
        """
        Record token consumption for a channel and update budget.

        Returns updated TokenBudget.
        """
        if not self.enabled or self._budget is None:
            return self._budget or TokenBudget(limit=0)

        self._budget.used += estimated_tokens
        record = ChannelRecord(
            channel_name=channel_name,
            result_count=result_count,
            estimated_tokens=estimated_tokens,
            unique_results_added=unique_added,
            relevance_gain=relevance_gain,
        )
        self._budget.channel_records.append(record)
        self._stats["total_channels_processed"] += 1

        if self._budget.used >= self._budget.limit:
            self._stats["budget_exhaustions"] += 1

        return self._budget

    # ═══════════════════════════════════════════════════════════════════
    #  Statistics & Reporting
    # ═══════════════════════════════════════════════════════════════════

    def statistics(self) -> Dict[str, Any]:
        """Return cumulative optimization statistics."""
        stats = dict(self._stats)
        if self._budget:
            stats["current_budget"] = {
                "limit": self._budget.limit,
                "used": self._budget.used,
                "remaining": self._budget.remaining,
                "usage_pct": round(self._budget.usage_pct, 1),
                "channels": len(self._budget.channel_records),
            }
        if stats["total_queries"] > 0:
            stats["avg_tokens_saved_per_query"] = round(
                stats["total_tokens_saved"] / stats["total_queries"]
            )
            stats["avg_dedup_per_query"] = round(
                stats["total_dedup_removed"] / stats["total_queries"], 1
            )
        return stats

    def reset_stats(self):
        """Reset cumulative statistics."""
        self._stats = {
            "total_queries": 0,
            "total_channels_processed": 0,
            "total_channels_skipped": 0,
            "total_results_before": 0,
            "total_results_after": 0,
            "total_tokens_saved": 0,
            "total_dedup_removed": 0,
            "early_stops_triggered": 0,
            "budget_exhaustions": 0,
        }

    def budget_summary(self) -> str:
        """Human-readable budget summary."""
        if not self._budget:
            return "TokenEfficiencyOptimizer: no active query"

        lines = [
            f"Token Budget: {self._budget.used}/{self._budget.limit} "
            f"({self._budget.usage_pct:.1f}%)",
            f"Channels processed: {len(self._budget.channel_records)}",
            f"Duplicates removed: {self._dedup_count}",
        ]
        for rec in self._budget.channel_records:
            lines.append(
                f"  [{rec.channel_name}] {rec.result_count} results, "
                f"~{rec.estimated_tokens} tokens"
            )
        return "\n".join(lines)


# ── Convenience: CRAG-style hook factory ──────────────────────────────


def create_crag_efficiency_hook(
    optimizer: TokenEfficiencyOptimizer,
) -> Callable[[List[Dict[str, Any]]], Tuple[List[Dict[str, Any]], bool]]:
    """
    Create an early-stop + dedup hook compatible with CRAG's evaluation loop.

    Returns a callable that takes (results) and returns (filtered_results, should_stop).

    Usage::

        opt = TokenEfficiencyOptimizer(early_stop_patience=3)
        hook = create_crag_efficiency_hook(opt)

        for channel_results in channel_stream:
            results, should_stop = hook(channel_results)
            if should_stop:
                break
    """

    def hook(results: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], bool]:
        if not optimizer.enabled:
            return results, False

        # Step 1: Deduplicate
        filtered = optimizer.deduplicate(results)

        # Step 2: Check early stop
        should_stop = optimizer.should_early_stop(filtered)

        return filtered, should_stop

    return hook


def create_search_hook(
    optimizer: TokenEfficiencyOptimizer,
    query: str,
) -> Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """
    Create a pre/post-processing hook for client.search().

    Returns a callable that wraps entire result list with dedup + truncation.

    Usage::

        opt = TokenEfficiencyOptimizer(enable_dynamic_truncation=True)
        hook = create_search_hook(opt, "what is machine learning")

        raw_results = adapter.search_memories(...)
        final_results = hook(raw_results)
    """

    optimizer.start_query(query)

    def hook(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not optimizer.enabled:
            return results

        # Dedup
        if optimizer.enable_dedup:
            results = optimizer.deduplicate(results)

        # Dynamic truncation
        if optimizer.enable_dynamic_truncation:
            results = optimizer.dynamic_truncate(results, query)

        return results

    return hook
