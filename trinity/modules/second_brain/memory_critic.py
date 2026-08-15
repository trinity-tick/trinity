"""
# status: orphan (2026-08-15 audit, not in runtime path)
P3-2: Memory Critic + Reconstruction Pipeline (对标 MemHarness)
================================================================
Implements a four-stage pipeline: Retrieve -> Critique -> Reconstruct -> Action.

Based on the MemHarness framework (Shanghai AI Lab, 2026), this module inserts
explicit "critique" and "reconstruction" stages between retrieval and action
generation. Rather than injecting retrieved memories directly into context,
it first evaluates whether the memory fits the current context, then preserves
transferable portions while rewriting or discarding inapplicable parts.

Pipeline:
  Stage 1 — Retrieve:   Query the memory store for relevant past experiences.
  Stage 2 — Critique:   Judge whether retrieved memories are applicable to the
                         current context (context-relevance scoring, conflict
                         detection, obsolescence check).
  Stage 3 — Reconstruct: Preserve transferable parts, rewrite context-specific
                         portions, or discard entirely. Handles negative
                         transfer prevention.
  Stage 4 — Action:     Inject reconstructed guidance into the decision context.

Reference:
  - MemHarness: "LLM Agent Experience Reconstruction" (arXiv:2607.28272)
  - Learning to harness retrieved experiences via GRPO end-to-end optimization
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────

class CritiqueVerdict(Enum):
    """Verdict after critiquing a retrieved memory against current context."""
    FULLY_TRANSFERABLE = "fully_transferable"
    PARTIALLY_TRANSFERABLE = "partially_transferable"
    OBSOLETE = "obsolete"
    CONFLICTING = "conflicting"
    IRRELEVANT = "irrelevant"


@dataclass
class CritiquedMemory:
    """A memory that has passed through the Critique stage."""
    memory_id: str
    original_content: str
    verdict: CritiqueVerdict
    relevance_score: float          # 0.0 ~ 1.0
    conflict_details: List[str] = field(default_factory=list)
    transferable_parts: List[str] = field(default_factory=list)
    inapplicable_parts: List[str] = field(default_factory=list)
    obsolescence_reason: str = ""


@dataclass
class ReconstructedMemory:
    """A memory that has passed through the Reconstruct stage."""
    memory_id: str
    original_content: str
    reconstructed_content: str
    action: str                      # "keep" | "rewrite" | "discard"
    preserved_ratio: float           # 0.0 ~ 1.0
    rewrite_rationale: str = ""
    source_verdict: CritiqueVerdict = CritiqueVerdict.IRRELEVANT


@dataclass
class MemoryCriticResult:
    """Full pipeline result from one retrieval-to-action cycle."""
    query_context: str
    retrieved_count: int
    critiqued: List[CritiquedMemory] = field(default_factory=list)
    reconstructed: List[ReconstructedMemory] = field(default_factory=list)
    final_guidance: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)


# ── Scoring Engine (Stage 1–2) ───────────────────────────────────────────

class _ScoringEngine:
    """Retrieval and critique scoring — Stages 1 and 2.

    Handles memory retrieval, relevance scoring, conflict detection,
    obsolescence checks, and content partitioning into transferable /
    inapplicable parts.
    """

    def __init__(
        self,
        relevance_threshold: float,
        obsolescence_days: int,
        enable_conflict_detection: bool,
    ):
        self.relevance_threshold = relevance_threshold
        self.obsolescence_days = obsolescence_days
        self.enable_conflict_detection = enable_conflict_detection

    def retrieve(
        self,
        query_context: str,
        memory_store: Any,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        if not hasattr(memory_store, "search"):
            logger.warning("Memory store lacks search(); returning empty.")
            return []

        try:
            results = memory_store.search(query_context, top_k=top_k)
        except Exception as e:
            logger.error("Retrieval failed: %s", e)
            return []

        normalized = []
        for r in results:
            if isinstance(r, dict):
                normalized.append({
                    "id": r.get("id", r.get("entity_id", str(time.time()))),
                    "content": r.get("content", r.get("text", str(r))),
                    "metadata": r.get("metadata", r.get("properties", {})),
                    "timestamp": r.get("timestamp", r.get("created_at", 0)),
                    "score": r.get("score", r.get("ppr_score", 1.0)),
                })
        return normalized

    def critique(
        self,
        retrieved: List[Dict[str, Any]],
        current_state: Dict[str, Any],
        query_context: str,
    ) -> List[CritiquedMemory]:
        critiqued: List[CritiquedMemory] = []
        now = time.time()
        threshold_seconds = self.obsolescence_days * 86400

        for mem in retrieved:
            memory_id = mem.get("id", "unknown")
            content = mem.get("content", "")
            mem_ts = mem.get("timestamp", 0)

            relevance = self._compute_relevance(content, query_context)
            age = now - mem_ts if mem_ts > 0 else 0
            is_stale = age > threshold_seconds

            conflicts: List[str] = []
            if self.enable_conflict_detection:
                conflicts = self._detect_conflicts(content, current_state)

            if relevance < self.relevance_threshold:
                verdict = CritiqueVerdict.IRRELEVANT
            elif is_stale and relevance < 0.6:
                verdict = CritiqueVerdict.OBSOLETE
            elif conflicts:
                verdict = CritiqueVerdict.CONFLICTING
            elif relevance >= 0.8:
                verdict = CritiqueVerdict.FULLY_TRANSFERABLE
            else:
                verdict = CritiqueVerdict.PARTIALLY_TRANSFERABLE

            transferable, inapplicable = self._partition_content(
                content, current_state, verdict
            )

            critiqued.append(CritiquedMemory(
                memory_id=memory_id,
                original_content=content,
                verdict=verdict,
                relevance_score=relevance,
                conflict_details=conflicts,
                transferable_parts=transferable,
                inapplicable_parts=inapplicable,
                obsolescence_reason=(
                    f"Memory age {age / 86400:.1f} days exceeds threshold"
                    if is_stale else ""
                ),
            ))

        return critiqued

    @staticmethod
    def _compute_relevance(content: str, query_context: str) -> float:
        if not content or not query_context:
            return 0.0
        content_lower = content.lower()
        context_lower = query_context.lower()
        cwords = set(content_lower.split())
        qwords = set(context_lower.split())
        if not qwords:
            return 0.0
        overlap = cwords & qwords
        jaccard = len(overlap) / max(len(cwords | qwords), 1)
        phrase_bonus = 0.0
        for qw in qwords:
            if len(qw) >= 4 and qw in content_lower:
                phrase_bonus += 0.1
        return min(jaccard + phrase_bonus, 1.0)

    def _detect_conflicts(
        self,
        content: str,
        current_state: Dict[str, Any],
    ) -> List[str]:
        conflicts: List[str] = []
        content_lower = content.lower()
        for key, val in current_state.items():
            val_str = str(val).lower()
            if key.lower() in content_lower:
                negations = ["not", "no longer", "stopped", "switched", "changed"]
                for neg in negations:
                    if neg in content_lower and val_str not in content_lower:
                        conflicts.append(
                            f"Memory references '{key}' but current state "
                            f"differs: '{val_str}'"
                        )
                        break
        return conflicts[:5]

    @staticmethod
    def _partition_content(
        content: str,
        current_state: Dict[str, Any],
        verdict: CritiqueVerdict,
    ) -> Tuple[List[str], List[str]]:
        import re

        sentences = re.split(r'(?<=[.!?。！？])\s+', content)
        if not sentences:
            return [], []

        current_keywords = set()
        for v in current_state.values():
            for w in str(v).lower().split():
                if len(w) >= 3:
                    current_keywords.add(w)

        transferable: List[str] = []
        inapplicable: List[str] = []

        for sent in sentences:
            sent_lower = sent.lower()
            sent_words = set(sent_lower.split())
            overlap = sent_words & current_keywords

            if verdict == CritiqueVerdict.FULLY_TRANSFERABLE:
                transferable.append(sent.strip())
            elif verdict in (CritiqueVerdict.IRRELEVANT, CritiqueVerdict.OBSOLETE,
                             CritiqueVerdict.CONFLICTING):
                inapplicable.append(sent.strip())
            else:
                if overlap:
                    transferable.append(sent.strip())
                else:
                    inapplicable.append(sent.strip())

        return transferable, inapplicable

    @staticmethod
    def _extract_keywords(text: str, min_len: int = 3) -> List[str]:
        import re
        words = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', text.lower())
        return [w for w in words if len(w) >= min_len]


# ── Feedback Aggregator (Stage 3–4 + full pipeline) ──────────────────────

class _FeedbackAggregator:
    """Reconstruction, action generation, and feedback loop — Stages 3–4.

    Handles the reconstruct → action pipeline and collects user feedback
    to adaptively tune relevance threshold.
    """

    def __init__(
        self,
        enable_partial_reconstruction: bool,
        relevance_threshold: float,
    ):
        self.enable_partial_reconstruction = enable_partial_reconstruction
        self.relevance_threshold = relevance_threshold
        self._feedback_history: List[Dict[str, Any]] = []

    def reconstruct(
        self,
        critiqued: List[CritiquedMemory],
        query_context: str,
        current_state: Dict[str, Any],
    ) -> List[ReconstructedMemory]:
        reconstructed: List[ReconstructedMemory] = []

        for cm in critiqued:
            if cm.verdict == CritiqueVerdict.IRRELEVANT:
                reconstructed.append(ReconstructedMemory(
                    memory_id=cm.memory_id,
                    original_content=cm.original_content,
                    reconstructed_content="",
                    action="discard",
                    preserved_ratio=0.0,
                    rewrite_rationale="Irrelevant to current context",
                    source_verdict=cm.verdict,
                ))
                continue

            if cm.verdict == CritiqueVerdict.FULLY_TRANSFERABLE:
                reconstructed.append(ReconstructedMemory(
                    memory_id=cm.memory_id,
                    original_content=cm.original_content,
                    reconstructed_content=cm.original_content,
                    action="keep",
                    preserved_ratio=1.0,
                    source_verdict=cm.verdict,
                ))
                continue

            if cm.verdict in (CritiqueVerdict.OBSOLETE, CritiqueVerdict.CONFLICTING):
                reconstructed.append(ReconstructedMemory(
                    memory_id=cm.memory_id,
                    original_content=cm.original_content,
                    reconstructed_content="",
                    action="discard",
                    preserved_ratio=0.0,
                    rewrite_rationale=(
                        f"Conflicts: {'; '.join(cm.conflict_details)}"
                        if cm.conflict_details else cm.obsolescence_reason
                    ),
                    source_verdict=cm.verdict,
                ))
                continue

            # PARTIALLY_TRANSFERABLE → rewrite
            if not self.enable_partial_reconstruction:
                reconstructed.append(ReconstructedMemory(
                    memory_id=cm.memory_id,
                    original_content=cm.original_content,
                    reconstructed_content=cm.original_content,
                    action="keep",
                    preserved_ratio=0.5,
                    source_verdict=cm.verdict,
                ))
                continue

            kept_parts = cm.transferable_parts
            dropped_parts = cm.inapplicable_parts

            if not kept_parts:
                reconstructed.append(ReconstructedMemory(
                    memory_id=cm.memory_id,
                    original_content=cm.original_content,
                    reconstructed_content="",
                    action="discard",
                    preserved_ratio=0.0,
                    rewrite_rationale="No transferable parts identified",
                    source_verdict=cm.verdict,
                ))
                continue

            reconstructed_content = (
                "[Context-Adapted Memory]\n"
                + "\n".join(f"- {p}" for p in kept_parts)
                + f"\n\n[Adapted from: {cm.original_content[:100]}...]"
            )
            preserved_ratio = (
                len(kept_parts) / max(len(kept_parts) + len(dropped_parts), 1)
            )

            reconstructed.append(ReconstructedMemory(
                memory_id=cm.memory_id,
                original_content=cm.original_content,
                reconstructed_content=reconstructed_content,
                action="rewrite",
                preserved_ratio=round(preserved_ratio, 3),
                rewrite_rationale=f"Dropped {len(dropped_parts)} inapplicable part(s)",
                source_verdict=cm.verdict,
            ))

        return reconstructed

    def action(
        self,
        reconstructed: List[ReconstructedMemory],
    ) -> str:
        active = [rm for rm in reconstructed if rm.action != "discard"]
        if not active:
            return "[MemoryCritic] No applicable memories found for current context."
        lines = ["[MemoryCritic — Reconstructed Guidance]", ""]
        for i, rm in enumerate(active, 1):
            tag = "[KEPT]" if rm.action == "keep" else "[REWRITTEN]"
            lines.append(f"{tag} Memory {i} (relevance preserved: {rm.preserved_ratio:.0%})")
            lines.append(rm.reconstructed_content)
            lines.append("")
        return "\n".join(lines)

    def record_feedback(
        self,
        memory_id: str,
        was_helpful: bool,
        notes: str = "",
    ) -> None:
        self._feedback_history.append({
            "memory_id": memory_id,
            "was_helpful": was_helpful,
            "notes": notes,
            "timestamp": time.time(),
        })
        if len(self._feedback_history) >= 10:
            recent = self._feedback_history[-20:]
            helpful_rate = sum(1 for f in recent if f["was_helpful"]) / len(recent)
            if helpful_rate < 0.4:
                self.relevance_threshold = min(0.7, self.relevance_threshold + 0.05)
                logger.info(
                    "Low helpful rate %.2f, raising relevance threshold to %.2f",
                    helpful_rate, self.relevance_threshold,
                )
            elif helpful_rate > 0.8:
                self.relevance_threshold = max(0.1, self.relevance_threshold - 0.02)


# ── Facade ────────────────────────────────────────────────────────────────

class MemoryCriticPipeline:
    """四阶段记忆批判重建流水线：Retrieve→Critique→Reconstruct→Action。"""

    def __init__(self, relevance_threshold: float = 0.3, obsolescence_days: int = 90,
                 enable_conflict_detection: bool = True, enable_partial_reconstruction: bool = True):
        self._scoring = _ScoringEngine(relevance_threshold=relevance_threshold,
                                        obsolescence_days=obsolescence_days,
                                        enable_conflict_detection=enable_conflict_detection)
        self._feedback = _FeedbackAggregator(
            enable_partial_reconstruction=enable_partial_reconstruction,
            relevance_threshold=relevance_threshold)

    # ── Properties ──
    @property
    def relevance_threshold(self) -> float: return self._scoring.relevance_threshold
    @relevance_threshold.setter
    def relevance_threshold(self, v: float) -> None:
        self._scoring.relevance_threshold = v; self._feedback.relevance_threshold = v
    @property
    def obsolescence_days(self) -> int: return self._scoring.obsolescence_days
    @obsolescence_days.setter
    def obsolescence_days(self, v: int) -> None: self._scoring.obsolescence_days = v
    @property
    def enable_conflict_detection(self) -> bool: return self._scoring.enable_conflict_detection
    @enable_conflict_detection.setter
    def enable_conflict_detection(self, v: bool) -> None: self._scoring.enable_conflict_detection = v
    @property
    def enable_partial_reconstruction(self) -> bool: return self._feedback.enable_partial_reconstruction
    @enable_partial_reconstruction.setter
    def enable_partial_reconstruction(self, v: bool) -> None:
        self._feedback.enable_partial_reconstruction = v

    # ── Stage 1-2 ──
    def retrieve(self, query_context: str, memory_store: Any, top_k: int = 10) -> List[Dict[str, Any]]:
        return self._scoring.retrieve(query_context, memory_store, top_k)
    def critique(self, retrieved: List[Dict[str, Any]], current_state: Dict[str, Any],
                  query_context: str) -> List[CritiquedMemory]:
        return self._scoring.critique(retrieved, current_state, query_context)

    # ── Stage 3-4 ──
    def reconstruct(self, critiqued: List[CritiquedMemory], query_context: str,
                     current_state: Dict[str, Any]) -> List[ReconstructedMemory]:
        return self._feedback.reconstruct(critiqued, query_context, current_state)
    def action(self, reconstructed: List[ReconstructedMemory]) -> str:
        return self._feedback.action(reconstructed)

    # ── Full Pipeline ──
    def run(self, query_context: str, retrieved_memories: Optional[List[Dict[str, Any]]] = None,
            current_state: Optional[Dict[str, Any]] = None, memory_store: Any = None,
            top_k: int = 10) -> MemoryCriticResult:
        current_state = current_state or {}
        start = time.time()
        if retrieved_memories is None and memory_store is not None:
            retrieved_memories = self._scoring.retrieve(query_context, memory_store, top_k)
        if retrieved_memories is None:
            retrieved_memories = []
        critiqued = self._scoring.critique(retrieved_memories, current_state, query_context)
        reconstructed = self._feedback.reconstruct(critiqued, query_context, current_state)
        guidance = self._feedback.action(reconstructed)
        elapsed = time.time() - start
        kept = sum(1 for r in reconstructed if r.action == "keep")
        rewritten = sum(1 for r in reconstructed if r.action == "rewrite")
        discarded = sum(1 for r in reconstructed if r.action == "discard")
        return MemoryCriticResult(
            query_context=query_context, retrieved_count=len(retrieved_memories),
            critiqued=critiqued, reconstructed=reconstructed, final_guidance=guidance,
            stats={"elapsed_ms": round(elapsed * 1000, 1), "retrieved": len(retrieved_memories),
                   "kept": kept, "rewritten": rewritten, "discarded": discarded,
                   "avg_relevance": sum(c.relevance_score for c in critiqued) / max(len(critiqued), 1)})

    # ── Feedback ──
    def record_feedback(self, memory_id: str, was_helpful: bool, notes: str = "") -> None:
        self._feedback.record_feedback(memory_id, was_helpful, notes)

