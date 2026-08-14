"""Mutation Engine — memory mutation suggestion and auto-application.

Generates suggestions for merging, enriching, splitting, and synthesising
memories. Auto-applies high-confidence mutations within configured thresholds.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple


# ═══════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MergeSuggestion:
    """Suggestion to merge two similar memories."""
    type: str = "merge"
    memory_a: str = ""
    memory_b: str = ""
    similarity: float = 0.0        # 0.0–1.0
    confidence: float = 0.0
    reason: str = ""
    suggested_id: str = ""
    auto_applied: bool = False


@dataclass
class EnrichSuggestion:
    """Suggestion to enrich a memory with inferred context."""
    type: str = "enrich"
    memory_id: str = ""
    missing_fields: List[str] = field(default_factory=list)
    inferred_values: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""
    auto_applied: bool = False


@dataclass
class SplitSuggestion:
    """Suggestion to split an over-aggregated memory node."""
    type: str = "split"
    memory_id: str = ""
    suggested_parts: int = 0
    split_by: str = ""            # attribute to split on
    confidence: float = 0.0
    reason: str = ""
    auto_applied: bool = False


@dataclass
class SynthesisMemory:
    """A synthesised high-value composite memory from related sources."""
    type: str = "synthesis"
    source_ids: List[str] = field(default_factory=list)
    synthesized_content: str = ""
    tags: List[str] = field(default_factory=list)
    confidence: float = 0.0
    auto_created: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Engine
# ═══════════════════════════════════════════════════════════════════════════

class MutationEngine:
    """Generates and auto-applies memory mutation suggestions.

    Parameters
    ----------
    auto_confidence_threshold : float
        Mutations with confidence above this are auto-applied (if variant enabled).
    merge_similarity_threshold : float
        Memories more similar than this trigger a merge suggestion.
    """

    def __init__(
        self,
        auto_confidence_threshold: float = 0.85,
        merge_similarity_threshold: float = 0.75,
    ):
        self.auto_confidence_threshold = auto_confidence_threshold
        self.merge_similarity_threshold = merge_similarity_threshold
        self._pending: List[Any] = []  # low-confidence suggestions
        self._applied: List[Any] = []  # applied mutations

    # ── Suggestions ─────────────────────────────────────────────────────

    def suggest_merge(
        self,
        memory_a: str,
        memory_b: str,
        similarity: float = 0.0,
    ) -> MergeSuggestion:
        """Suggest merging two similar memories."""
        conf = similarity * 0.9
        suggestion = MergeSuggestion(
            memory_a=memory_a,
            memory_b=memory_b,
            similarity=round(similarity, 2),
            confidence=round(conf, 2),
            reason=f"Similarity score {similarity:.2f} exceeds threshold",
            suggested_id=f"{memory_a}_{memory_b}",
        )
        if conf >= self.auto_confidence_threshold:
            suggestion.auto_applied = True
            self._applied.append(suggestion)
        else:
            self._pending.append(suggestion)
        return suggestion

    def suggest_enrich(self, memory_id: str) -> EnrichSuggestion:
        """Suggest enriching a memory with inferred missing associations."""
        suggestion = EnrichSuggestion(
            memory_id=memory_id,
            missing_fields=[],
            inferred_values={},
            confidence=0.5,
            reason="Graph traversal identified potential missing context",
        )
        self._pending.append(suggestion)
        return suggestion

    def suggest_split(self, memory_id: str) -> SplitSuggestion:
        """Suggest splitting an over-aggregated memory."""
        suggestion = SplitSuggestion(
            memory_id=memory_id,
            suggested_parts=2,
            split_by="topic",
            confidence=0.6,
            reason="Memory appears to aggregate multiple distinct topics",
        )
        self._pending.append(suggestion)
        return suggestion

    def create_synthesis(
        self,
        related_memories: List[str],
        tags: Optional[List[str]] = None,
    ) -> SynthesisMemory:
        """Create a synthesised composite memory from related sources."""
        n = len(related_memories)
        conf = min(0.5 + 0.1 * n, 0.95)
        synthesis = SynthesisMemory(
            source_ids=related_memories,
            synthesized_content=f"Synthesis from {n} related memories",
            tags=tags or [],
            confidence=round(conf, 2),
        )
        if conf >= self.auto_confidence_threshold:
            synthesis.auto_created = True
            self._applied.append(synthesis)
        else:
            self._pending.append(synthesis)
        return synthesis

    # ── Auto-apply ──────────────────────────────────────────────────────

    def auto_apply(self, enabled_variants: List[str]) -> List[Dict[str, Any]]:
        """Auto-apply all pending mutations above confidence threshold.

        Only applies types listed in enabled_variants.
        """
        results: List[Dict[str, Any]] = []
        remaining: List[Any] = []

        for item in self._pending:
            item_type = getattr(item, "type", "unknown")
            conf = getattr(item, "confidence", 0.0)

            if item_type in enabled_variants and conf >= self.auto_confidence_threshold:
                item.auto_applied = True
                self._applied.append(item)
                results.append({
                    "type": item_type,
                    "status": "auto_applied",
                    "confidence": conf,
                })
            else:
                remaining.append(item)

        self._pending = remaining
        return results

    # ── Queries ─────────────────────────────────────────────────────────

    def get_pending(self) -> List[Any]:
        return list(self._pending)

    def get_applied(self) -> List[Any]:
        return list(self._applied)
