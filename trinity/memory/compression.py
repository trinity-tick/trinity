"""
Trinity Memory Compression Engine
===================================
Letta-style virtual context management with automatic summarisation,
hierarchical deduplication, and context-window trimming.

Core classes
------------
  TokenCounter      — fast token estimation & budget tracking
  ImportanceScorer  — composite scoring (access freq + time decay + correlation)
  MemoryCompressor  — three-stage compression pipeline (dedup → sort → summarise)
  CompressedContext — structured result container
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class CompressedContext:
    """Result of a compression run.

    Attributes
    ----------
    active_memories
        Memories retained in full-text form after compression.
    summary
        Summarised text for trimmed memories (empty string if none trimmed).
    trimmed_ids
        List of memory IDs that were trimmed and can be restored later.
    budget_usage
        Token usage ratio (0.0 – 1.0) after compression.
    original_token_count
        Token count before compression.
    compressed_token_count
        Token count after compression.
    """

    active_memories: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    trimmed_ids: List[str] = field(default_factory=list)
    budget_usage: float = 0.0
    original_token_count: int = 0
    compressed_token_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "active_count": len(self.active_memories),
            "trimmed_count": len(self.trimmed_ids),
            "summary": self.summary[:500] + "..." if len(self.summary) > 500 else self.summary,
            "budget_usage": round(self.budget_usage, 4),
            "original_tokens": self.original_token_count,
            "compressed_tokens": self.compressed_token_count,
            "trimmed_ids": self.trimmed_ids,
        }


# ═══════════════════════════════════════════════════════════════════════════
# TokenCounter
# ═══════════════════════════════════════════════════════════════════════════

class TokenCounter:
    """Fast token-count estimator.

    Falls back to char / 4 heuristic when ``tiktoken`` is unavailable.
    """

    # Approximate overhead per memory entry (id + metadata + delimiters)
    ENTRY_OVERHEAD = 12

    def __init__(self, encoding_name: str = "cl100k_base"):
        self._encoding_name = encoding_name
        self._encoder = None
        self._init_encoder()

    def _init_encoder(self):
        try:
            import tiktoken
            self._encoder = tiktoken.get_encoding(self._encoding_name)
        except Exception:
            self._encoder = None

    def count_tokens(self, text: str) -> int:
        """Return estimated token count for *text*."""
        if not text:
            return 0
        if self._encoder is not None:
            try:
                return len(self._encoder.encode(text))
            except Exception:
                pass
        # Fallback: ~4 chars / token
        return max(1, math.ceil(len(text) / 4))

    def estimate_budget(self, memories: List[Dict[str, Any]]) -> int:
        """Return total estimated token count for a list of memories.

        Each memory includes content + entry overhead.
        """
        total = 0
        for m in memories:
            content = m.get("content", "") if isinstance(m, dict) else str(m)
            total += self.count_tokens(content) + self.ENTRY_OVERHEAD
        return total


# ═══════════════════════════════════════════════════════════════════════════
# ImportanceScorer
# ═══════════════════════════════════════════════════════════════════════════

class ImportanceScorer:
    """Composite memory-importance scorer.

    Weights (normalised to 1.0):
      - access_frequency  : 0.5
      - time_decay        : 0.3
      - correlation_count : 0.2
    """

    # Half-life for time decay (in seconds) — 30 days default
    DEFAULT_HALF_LIFE_SECONDS = 30 * 86400  # 30 days

    def __init__(
        self,
        freq_weight: float = 0.5,
        time_weight: float = 0.3,
        corr_weight: float = 0.2,
        half_life_seconds: float = DEFAULT_HALF_LIFE_SECONDS,
    ):
        total = freq_weight + time_weight + corr_weight
        self._fw = freq_weight / total
        self._tw = time_weight / total
        self._cw = corr_weight / total
        self._half_life = half_life_seconds

    def score(self, memory: Dict[str, Any]) -> float:
        """Return composite importance score (0.0–1.0).

        Scoring components
        ------------------
        *access_frequency*
            Normalised by log(1 + count).  Max expected access_count is
            capped at 1000 for normalisation.

        *time_decay*
            Exponential decay: 2^(-age_seconds / half_life).

        *correlation*
            Normalised by count of associated / linked memory IDs.
        """
        freq = self._normalise_access_freq(memory)
        time_val = self._time_decay(memory)
        corr = self._normalise_correlation(memory)
        return self._fw * freq + self._tw * time_val + self._cw * corr

    # ── helpers ────────────────────────────────────────────────────────

    def _normalise_access_freq(self, memory: Dict[str, Any]) -> float:
        access_count = memory.get("access_count", memory.get("hit_count", 0))
        if not isinstance(access_count, (int, float)):
            try:
                access_count = int(access_count)
            except Exception:
                access_count = 0
        return math.log(1 + min(access_count, 1000)) / math.log(1001)

    def _time_decay(self, memory: Dict[str, Any]) -> float:
        created = memory.get("created_at", memory.get("timestamp", 0))
        try:
            if isinstance(created, str):
                from datetime import datetime
                created = datetime.fromisoformat(created).timestamp()
        except Exception:
            created = 0.0
        if not isinstance(created, (int, float)):
            created = 0.0
        age_seconds = max(0.0, time.time() - created)
        if age_seconds <= 0:
            return 1.0
        # 2^(-age / half_life)
        return math.pow(2, -age_seconds / self._half_life)

    def _normalise_correlation(self, memory: Dict[str, Any]) -> float:
        linked = memory.get("linked_memory_ids", memory.get("correlations", []))
        if isinstance(linked, list):
            count = len(linked)
        else:
            count = 0
        return min(1.0, count / 20.0)


# ═══════════════════════════════════════════════════════════════════════════
# MemoryCompressor
# ═══════════════════════════════════════════════════════════════════════════

class MemoryCompressor:
    """Three-stage memory compression engine.

    Pipeline
    --------
    1. **Deduplicate** — merge highly-similar memories into one.
    2. **Sort by importance** — rank memories via ImportanceScorer, trim to budget.
    3. **Summarise** — generate a summary for trimmed low-importance memories.

    Parameters
    ----------
    trinity_instance
        Reference to the Trinity object (used for adapter access, e.g. restore).
    max_tokens : int
        Token budget ceiling for the active working set.
    compression_threshold : float
        Fraction of max_tokens at which compression is triggered (0.0–1.0).
    """

    def __init__(
        self,
        trinity_instance: Any = None,
        max_tokens: int = 4096,
        compression_threshold: float = 0.8,
    ):
        self._trinity = trinity_instance
        self.max_tokens = max_tokens
        self.compression_threshold = compression_threshold
        self._token_counter = TokenCounter()
        self._scorer = ImportanceScorer()

        # ── Stats tracking ─────────────────────────────────────────
        self._history: List[Dict[str, Any]] = []

    # ── Public API ─────────────────────────────────────────────────────

    def compress(
        self, agent_id: str, memories: List[Dict[str, Any]],
    ) -> CompressedContext:
        """Run the full compression pipeline.

        Returns a ``CompressedContext`` with active memories, summary,
        trimmed IDs, and budget usage.
        """
        if not memories:
            return CompressedContext(
                active_memories=[],
                trimmed_ids=[],
                summary="",
                budget_usage=0.0,
                original_token_count=0,
                compressed_token_count=0,
            )

        original_budget = self._token_counter.estimate_budget(memories)

        # Stage 1: Deduplicate
        deduped = self.deduplicate(memories)

        # Stage 2: Importance sort + trim
        sorted_mems = self.sort_by_importance(deduped)
        active, trimmed = self._trim_to_budget(sorted_mems, self.max_tokens)

        # Stage 3: Summarise trimmed memories
        summary = ""
        if trimmed:
            summary = self.summarize(trimmed)

        compressed_budget = self._token_counter.estimate_budget(active)
        if self.max_tokens > 0:
            budget_usage = compressed_budget / self.max_tokens
        else:
            budget_usage = 0.0

        trimmed_ids = [m.get("memory_id", "") for m in trimmed if m.get("memory_id")]

        result = CompressedContext(
            active_memories=active,
            summary=summary,
            trimmed_ids=trimmed_ids,
            budget_usage=budget_usage,
            original_token_count=original_budget,
            compressed_token_count=compressed_budget,
        )

        # Record stats
        self._history.append({
            "agent_id": agent_id,
            "timestamp": time.time(),
            "original_count": len(memories),
            "active_count": len(active),
            "trimmed_count": len(trimmed),
            "original_tokens": original_budget,
            "compressed_tokens": compressed_budget,
            "budget_usage": budget_usage,
        })
        # Keep history bounded
        if len(self._history) > 1000:
            self._history = self._history[-1000:]

        return result

    def deduplicate(
        self,
        memories: List[Dict[str, Any]],
        similarity_threshold: float = 0.85,
    ) -> List[Dict[str, Any]]:
        """Merge highly-similar memories using overlap-coefficient dedup.

        Similarity is computed on normalised content text using the
        *overlap coefficient* of token sets.  When two memories exceed
        *similarity_threshold*, the older one is merged into the newer one
        (keeping the newer memory_id).

        Returns the deduplicated list (may be shorter than input).
        """
        if len(memories) <= 1:
            return memories

        # Simple Jaccard-like overlap on token-sets
        def _tokens(text: str) -> set:
            text = text.lower().strip()
            # Character-level bigrams for language-agnostic similarity
            chars = [text[i:i + 2] for i in range(len(text) - 1)]
            return set(chars)

        token_sets = [(_tokens(m.get("content", "")), i) for i, m in enumerate(memories)]
        merged = set()  # indices to skip (already merged into another)
        result: List[Dict[str, Any]] = []

        for i, mem in enumerate(memories):
            if i in merged:
                continue
            si = token_sets[i][0]
            for j in range(i + 1, len(memories)):
                if j in merged:
                    continue
                sj = token_sets[j][0]
                if not si or not sj:
                    continue
                overlap = len(si & sj)
                # Overlap coefficient: |A∩B| / min(|A|,|B|)
                overlap_coeff = overlap / min(len(si), len(sj))
                if overlap_coeff >= similarity_threshold:
                    # Merge: keep newer memory (higher index), absorb older content
                    merged.add(i)  # skip older
                    # Append a note about the merge to the newer memory
                    newer = memories[j]
                    older_id = mem.get("memory_id", "")
                    newer["content"] = (
                        f"{newer.get('content', '')}\n"
                        f"[merged from memory {older_id}]"
                    )
                    break
            if i not in merged:
                result.append(mem)

        return result

    def sort_by_importance(self, memories: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return memories sorted by composite importance score (descending)."""
        scored = [(self._scorer.score(m), m) for m in memories]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored]

    def summarize(
        self,
        old_memories: List[Dict[str, Any]],
        prompt: Optional[str] = None,
    ) -> str:
        """Generate a summary string for a list of memories.

        When a custom *prompt* is not given, the summary is built by
        concatenating the first 100 characters of each memory's content
        with a '...' truncation marker and compressing into a single
        paragraph.

        In production, this would invoke an LLM summarisation call; the
        current heuristic fallback preserves semantic hints without
        exploding the token budget.
        """
        if not old_memories:
            return ""

        lines = []
        base_prompt = prompt or "The following memories have been trimmed:"
        lines.append(base_prompt)

        for i, m in enumerate(old_memories):
            content = m.get("content", "")
            mid = m.get("memory_id", f"mem_{i}")
            snippet = content[:120].replace("\n", " ")
            lines.append(f"- [{mid}]: {snippet}{'...' if len(content) > 120 else ''}")

        return "\n".join(lines)

    # ── Stats ──────────────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """Return compression statistics across all runs.

        Returns
        -------
        dict with *total_runs*, *avg_compression_ratio*, *total_trimmed*,
        and recent history.
        """
        if not self._history:
            return {
                "total_runs": 0,
                "avg_compression_ratio": 0.0,
                "total_trimmed": 0,
                "history": [],
            }

        total_runs = len(self._history)
        ratios = []
        total_trimmed = 0
        for entry in self._history:
            orig = entry.get("original_tokens", 0)
            comp = entry.get("compressed_tokens", 0)
            if orig > 0:
                ratios.append(comp / orig)
            total_trimmed += entry.get("trimmed_count", 0)

        avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
        recent = self._history[-10:]

        return {
            "total_runs": total_runs,
            "avg_compression_ratio": round(avg_ratio, 4),
            "total_trimmed": total_trimmed,
            "history": recent,
        }

    # ── Trim helper ────────────────────────────────────────────────────

    def _trim_to_budget(
        self,
        memories: List[Dict[str, Any]],
        max_tokens: int,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Partition memories into *active* (fit within budget) and *trimmed*.

        Threshold-based early exit: only trim when estimated usage exceeds
        ``compression_threshold * max_tokens``.
        """
        active: List[Dict[str, Any]] = []
        trimmed: List[Dict[str, Any]] = []
        budget_used = 0
        threshold = int(self.compression_threshold * max_tokens)

        for m in memories:
            content = m.get("content", "")
            tokens = self._token_counter.count_tokens(content) + self._token_counter.ENTRY_OVERHEAD

            if budget_used + tokens <= max_tokens and budget_used < threshold:
                active.append(m)
                budget_used += tokens
            else:
                trimmed.append(m)

        # If nothing was trimmed but we're over threshold, force trim
        if not trimmed and budget_used > max_tokens:
            # Start from the end (least important) and move to trimmed
            while budget_used > max_tokens and active:
                removed = active.pop()
                trimmed.append(removed)
                content = removed.get("content", "")
                budget_used -= self._token_counter.count_tokens(content) + self._token_counter.ENTRY_OVERHEAD

        return active, trimmed
