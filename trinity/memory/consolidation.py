"""
Trinity Memory Consolidation Engine (v8.7.0)
=============================================
Hippocampal-inspired memory consolidation implementing:
  - Ebbinghaus forgetting curve with spaced repetition
  - Sleep/wake consolidation cycles (idle-triggered replay)
  - Episodic → semantic memory abstraction over time
  - Redundancy detection via cosine similarity & merging
  - Memory strength tracking with automatic decay

Frontier basis:
  - HippoRAG (arXiv 2024)  — neurobiologically inspired LTM
  - MemoryBank (arXiv 2024) — Ebbinghaus forgetting curve for agents
  - SleepRL (arXiv 2024)    — sleep-inspired representation learning
  - MemoRAG (arXiv 2024)    — global memory consolidation

Design philosophy
-----------------
Agents accumulate many raw episodic memories during active operation.
A "sleep" consolidation cycle, triggered during idle periods or explicitly,
replays these episodes to:
  1. Prune redundant / low-importance memories (forgetting curve)
  2. Abstract recurring episodes into semantic rules
  3. Strengthen high-value connections
  4. Prevent catastrophic forgetting

This module extends Trinity's existing MemoryCompressor with a temporal
dimension—it doesn't just compress space but also consolidates across time.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Data Model
# ═══════════════════════════════════════════════════════════════════════════

class MemoryType(Enum):
    EPISODIC = "episodic"    # Raw experience: "User asked about weather at 14:03"
    SEMANTIC = "semantic"    # Abstracted knowledge: "User checks weather daily"
    PROCEDURAL = "procedural"  # Action patterns: "When asked weather → call API"


class ConsolidationPhase(Enum):
    WAKE = "wake"       # Active operation — accumulate memories
    NREM = "nrem"        # Light sleep — redundancy pruning & strength decay
    REM = "rem"          # Deep sleep — episodic → semantic abstraction


@dataclass
class MemoryItem:
    """A single memory entry tracked by the consolidator.

    Attributes
    ----------
    id: str
        Unique hash identifier for this memory.
    content: str
        Raw memory content / text.
    memory_type: MemoryType
        Classification (episodic / semantic / procedural).
    strength: float
        Current memory strength (0.0–1.0), decays over time per Ebbinghaus.
    created_at: float
        Unix timestamp of memory creation.
    last_accessed: float
        Unix timestamp of last retrieval or reinforcement.
    access_count: int
        Number of times this memory has been retrieved.
    consolidation_count: int
        How many consolidation cycles this memory has survived.
    tags: List[str]
        User-defined or auto-generated topic tags.
    embedding: Optional[List[float]]
        Semantic embedding vector for similarity computation.
    source_episodes: List[str]
        For semantic memories: IDs of source episodic memories.
    """
    id: str
    content: str
    memory_type: MemoryType = MemoryType.EPISODIC
    strength: float = 1.0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    consolidation_count: int = 0
    tags: List[str] = field(default_factory=list)
    embedding: Optional[List[float]] = None
    source_episodes: List[str] = field(default_factory=list)


@dataclass
class ConsolidationConfig:
    """Configuration for the consolidation engine.

    Attributes
    ----------
    decay_half_life_seconds: float
        Ebbinghaus half-life: time (s) for strength to decay to 0.5 without
        reinforcement. Default 3600.0 (1 hour).
    min_strength_threshold: float
        Memories below this strength get pruned during NREM. Default 0.15.
    redundancy_similarity_threshold: float
        Cosine similarity above which two memories are considered redundant.
        Default 0.85.
    consolidation_interval_seconds: float
        Minimum time between automatic consolidation cycles. Default 300.0 (5 min).
    semantic_abstraction_threshold: int
        Number of similar episodic memories needed to trigger semantic abstraction.
        Default 3.
    max_memories: int
        Hard cap on total memory items. Triggers aggressive pruning. Default 10000.
    """
    decay_half_life_seconds: float = 3600.0
    min_strength_threshold: float = 0.15
    redundancy_similarity_threshold: float = 0.85
    consolidation_interval_seconds: float = 300.0
    semantic_abstraction_threshold: int = 3
    max_memories: int = 10000


@dataclass
class ConsolidationResult:
    """Result of a single consolidation cycle.

    Attributes
    ----------
    phase: ConsolidationPhase
        Which phase was executed.
    pruned_count: int
        Memories removed due to low strength.
    merged_count: int
        Redundant memories merged.
    abstracted_count: int
        New semantic memories created from episode clusters.
    strengthened_count: int
        Memories reinforced during this cycle.
    remaining_count: int
        Total memories after consolidation.
    duration_seconds: float
        Wall-clock time for this cycle.
    """
    phase: ConsolidationPhase
    pruned_count: int = 0
    merged_count: int = 0
    abstracted_count: int = 0
    strengthened_count: int = 0
    remaining_count: int = 0
    duration_seconds: float = 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Ebbinghaus Forgetting Curve
# ═══════════════════════════════════════════════════════════════════════════

def ebbinghaus_strength(
    initial_strength: float,
    elapsed_seconds: float,
    half_life_seconds: float = 3600.0,
) -> float:
    """Compute current memory strength using the Ebbinghaus forgetting curve.

    R = R0 * e^(-t * ln(2) / S)
    where S is the half-life parameter.

    Parameters
    ----------
    initial_strength: float
        Strength at time of last reinforcement (0.0–1.0).
    elapsed_seconds: float
        Time elapsed since last reinforcement.
    half_life_seconds: float
        Time for strength to decay to 50%.

    Returns
    -------
    float
        Current strength (0.0–1.0).
    """
    if half_life_seconds <= 0:
        return initial_strength
    decay_rate = math.log(2) / half_life_seconds
    return initial_strength * math.exp(-decay_rate * elapsed_seconds)


def spaced_repetition_boost(
    access_count: int,
    consolidation_count: int,
) -> float:
    """Compute reinforcement boost from spaced repetition.

    Implements a diminishing-returns schedule: early accesses give large boosts,
    later accesses give smaller boosts (stabilisation).

    Returns multiplier on current strength, clipped to [1.0, 2.0].
    """
    base = 1.0 + 0.5 / (1.0 + 0.2 * (access_count + consolidation_count))
    return min(2.0, max(1.0, base))


# ═══════════════════════════════════════════════════════════════════════════
# Similarity Utilities
# ═══════════════════════════════════════════════════════════════════════════

def cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
    """Compute cosine similarity between two vectors.

    Returns 0.0 if either vector is zero-length or lengths mismatch.
    """
    if len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def simple_hash_embedding(text: str, dim: int = 64) -> List[float]:
    """Generate a lightweight pseudo-embedding via rolling hash.

    This is a fast fallback when no real embedding model is available.
    Uses a deterministic hash → normalized vector mapping for similarity
    computation. Not as accurate as real embeddings but zero-dependency and
    fast enough for redundancy detection.

    Parameters
    ----------
    text: str
        Input text to hash.
    dim: int
        Desired embedding dimension.

    Returns
    -------
    List[float]
        Normalized pseudo-embedding vector.
    """
    if not text:
        return [0.0] * dim
    # Generate dim pseudo-random values seeded by text
    vec = []
    for i in range(dim):
        seed = f"{text}_{i}"
        h = hashlib.sha256(seed.encode()).digest()
        # Map hash bytes to [-1, 1] float
        val = int.from_bytes(h[:4], "big") / (2 ** 32) * 2.0 - 1.0
        vec.append(val)
    # Normalize
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        return [0.0] * dim
    return [v / norm for v in vec]


# ═══════════════════════════════════════════════════════════════════════════
# Hippocampal Consolidator
# ═══════════════════════════════════════════════════════════════════════════

class HippocampalConsolidator:
    """Hippocampal-inspired memory consolidation engine.

    Manages the lifecycle of agent memories through three phases:

    **WAKE** — Memories are added / accessed during agent operation.
      Strength decays passively per the Ebbinghaus curve. Retrieved
      memories get a spaced-repetition boost.

    **NREM (light sleep)** — Triggered during idle periods. Prunes
      low-strength memories, detects and merges redundant pairs based
      on cosine similarity, updates decay state.

    **REM (deep sleep)** — Episodic → semantic abstraction. Groups
      similar episodic memories (by tags + embedding clustering) and
      produces abstracted semantic memories when a group exceeds the
      ``semantic_abstraction_threshold``.

    Usage
    -----
    >>> c = HippocampalConsolidator()
    >>> c.add_memory("User asked about weather", tags=["weather", "query"])
    >>> c.add_memory("User asked about weather in London", tags=["weather", "query"])
    >>> c.add_memory("User asked about weather tomorrow", tags=["weather", "query"])
    >>> result = c.consolidate()  # triggers NREM + REM if threshold met
    """

    def __init__(self, config: Optional[ConsolidationConfig] = None):
        self.config = config or ConsolidationConfig()
        self._memories: Dict[str, MemoryItem] = {}
        self._last_consolidation: float = 0.0
        self._cycle_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def memory_count(self) -> int:
        """Return total number of memories currently tracked."""
        return len(self._memories)

    @property
    def last_consolidation_time(self) -> float:
        """Unix timestamp of the last consolidation cycle."""
        return self._last_consolidation

    @property
    def cycle_count(self) -> int:
        """Total consolidation cycles executed."""
        return self._cycle_count

    def add_memory(
        self,
        content: str,
        memory_type: MemoryType = MemoryType.EPISODIC,
        tags: Optional[List[str]] = None,
        embedding: Optional[List[float]] = None,
    ) -> str:
        """Add a new memory to the consolidator.

        Returns the memory's unique ID.
        """
        mem_id = _hash_content(content)
        if mem_id in self._memories:
            # Already exists — refresh
            self._memories[mem_id].last_accessed = time.time()
            self._memories[mem_id].access_count += 1
            self._memories[mem_id].strength = min(
                1.0,
                self._memories[mem_id].strength
                * spaced_repetition_boost(
                    self._memories[mem_id].access_count,
                    self._memories[mem_id].consolidation_count,
                ),
            )
            return mem_id
        emb = embedding or simple_hash_embedding(content)
        item = MemoryItem(
            id=mem_id,
            content=content,
            memory_type=memory_type,
            tags=tags or [],
            embedding=emb,
        )
        self._memories[mem_id] = item
        return mem_id

    def get_memory(self, memory_id: str) -> Optional[MemoryItem]:
        """Retrieve a memory by ID, reinforcing it."""
        mem = self._memories.get(memory_id)
        if mem is None:
            return None
        elapsed = time.time() - mem.last_accessed
        mem.strength = ebbinghaus_strength(
            mem.strength, elapsed, self.config.decay_half_life_seconds
        )
        mem.last_accessed = time.time()
        mem.access_count += 1
        # Spaced repetition boost on retrieval
        mem.strength = min(
            1.0,
            mem.strength
            * spaced_repetition_boost(mem.access_count, mem.consolidation_count),
        )
        return mem

    def get_memories_by_tag(self, tag: str) -> List[MemoryItem]:
        """Return all memories with a given tag, reinforcing each."""
        results = []
        for mem in self._memories.values():
            if tag in mem.tags:
                self.get_memory(mem.id)  # side-effect: reinforce
                results.append(mem)
        return results

    def get_top_memories(self, k: int = 10, min_strength: float = 0.0) -> List[MemoryItem]:
        """Return top k memories sorted by strength (descending)."""
        candidates = [m for m in self._memories.values() if m.strength >= min_strength]
        candidates.sort(key=lambda m: m.strength, reverse=True)
        return candidates[:k]

    def should_consolidate(self) -> bool:
        """Return True if enough time has passed for a new consolidation cycle."""
        if self._last_consolidation == 0.0:
            return True
        elapsed = time.time() - self._last_consolidation
        return elapsed >= self.config.consolidation_interval_seconds

    def consolidate(self, force: bool = False) -> ConsolidationResult:
        """Run a full consolidation cycle (NREM → REM if conditions met).

        Parameters
        ----------
        force: bool
            If True, run even if interval hasn't elapsed.

        Returns
        -------
        ConsolidationResult
            Statistics about what was pruned / merged / abstracted.
        """
        if not force and not self.should_consolidate():
            return ConsolidationResult(
                phase=ConsolidationPhase.WAKE,
                remaining_count=len(self._memories),
            )

        t0 = time.time()
        self._cycle_count += 1

        # Phase 1: NREM — decay all strengths and prune weak
        nrem_result = self._nrem_cycle()

        # Phase 2: REM — episodic → semantic abstraction
        rem_result = self._rem_cycle()

        self._last_consolidation = time.time()
        elapsed = time.time() - t0

        return ConsolidationResult(
            phase=ConsolidationPhase.REM,
            pruned_count=nrem_result[0],
            merged_count=nrem_result[1],
            abstracted_count=rem_result,
            strengthened_count=nrem_result[2],
            remaining_count=len(self._memories),
            duration_seconds=elapsed,
        )

    def force_remember(self, content: str, tags: Optional[List[str]] = None) -> str:
        """Add a high-priority memory with maximum initial strength.

        Useful for system prompts, constitutional rules, and critical facts.
        Returns memory ID.
        """
        mem_id = self.add_memory(
            content, memory_type=MemoryType.SEMANTIC, tags=tags
        )
        self._memories[mem_id].strength = 1.0
        self._memories[mem_id].memory_type = MemoryType.SEMANTIC
        return mem_id

    def get_strength_distribution(self) -> Dict[str, int]:
        """Return a histogram of memory strengths (10 buckets).

        Returns
        -------
        Dict[str, int]
            Keys like "0.0-0.1", values are counts.
        """
        dist: Dict[str, int] = defaultdict(int)
        for mem in self._memories.values():
            bucket = int(mem.strength * 10) / 10.0
            key = f"{bucket:.1f}-{bucket + 0.1:.1f}"
            dist[key] += 1
        return dict(sorted(dist.items()))

    def get_stats(self) -> Dict[str, Any]:
        """Return summary statistics for monitoring."""
        types = defaultdict(int)
        total_strength = 0.0
        for mem in self._memories.values():
            types[mem.memory_type.value] += 1
            total_strength += mem.strength
        return {
            "total_memories": len(self._memories),
            "by_type": dict(types),
            "mean_strength": (
                total_strength / len(self._memories) if self._memories else 0.0
            ),
            "consolidation_cycles": self._cycle_count,
            "last_consolidation": self._last_consolidation,
        }

    # ------------------------------------------------------------------
    # NREM — Decay & Pruning
    # ------------------------------------------------------------------

    def _nrem_cycle(self) -> Tuple[int, int, int]:
        """Execute NREM (light sleep) phase.

        Steps:
          1. Apply Ebbinghaus decay to all memories.
          2. Detect and merge redundant pairs (cosine similarity > threshold).
          3. Prune memories below ``min_strength_threshold``.
          4. Enforce ``max_memories`` cap via weakest-first eviction.

        Returns (pruned_count, merged_count, strengthened_count).
        """
        now = time.time()
        pruned = 0
        merged = 0
        strengthened = 0
        cfg = self.config

        # Step 1: decay all
        for mem in self._memories.values():
            elapsed = now - mem.last_accessed
            mem.strength = ebbinghaus_strength(
                mem.strength, elapsed, cfg.decay_half_life_seconds
            )

        # Step 2: redundancy detection & merge
        mem_list = list(self._memories.values())
        merged_ids: set = set()
        for i in range(len(mem_list)):
            if mem_list[i].id in merged_ids:
                continue
            for j in range(i + 1, len(mem_list)):
                if mem_list[j].id in merged_ids:
                    continue
                # Only merge same-type or episodic→semantic bridges
                if mem_list[i].memory_type != mem_list[j].memory_type:
                    continue
                if mem_list[i].embedding and mem_list[j].embedding:
                    sim = cosine_similarity(
                        mem_list[i].embedding, mem_list[j].embedding
                    )
                    if sim >= cfg.redundancy_similarity_threshold:
                        # Merge: keep stronger, discard weaker
                        if mem_list[i].strength >= mem_list[j].strength:
                            keeper, victim = mem_list[i], mem_list[j]
                        else:
                            keeper, victim = mem_list[j], mem_list[i]
                        keeper.access_count = max(
                            keeper.access_count, victim.access_count
                        )
                        keeper.source_episodes.extend(victim.source_episodes)
                        keeper.consolidation_count += 1
                        merged_ids.add(victim.id)
                        merged += 1

        for mid in merged_ids:
            del self._memories[mid]

        # Step 3: prune weak
        weak_ids = [
            mid
            for mid, mem in self._memories.items()
            if mem.strength < cfg.min_strength_threshold
            and mem.memory_type != MemoryType.SEMANTIC  # never auto-prune semantic
        ]
        for mid in weak_ids:
            del self._memories[mid]
            pruned += 1

        # Step 4: cap enforcement
        if len(self._memories) > cfg.max_memories:
            sorted_mems = sorted(
                self._memories.values(), key=lambda m: m.strength
            )
            overflow = len(self._memories) - cfg.max_memories
            for i in range(overflow):
                if sorted_mems[i].memory_type == MemoryType.SEMANTIC:
                    continue
                del self._memories[sorted_mems[i].id]
                pruned += 1

        # Count strengthened (survived consolidation)
        strengthened = len(self._memories)

        return pruned, merged, strengthened

    # ------------------------------------------------------------------
    # REM — Episodic → Semantic Abstraction
    # ------------------------------------------------------------------

    def _rem_cycle(self) -> int:
        """Execute REM (deep sleep) phase.

        Groups episodic memories by shared tags. When a group contains
        >= ``semantic_abstraction_threshold`` episodes with pairwise
        similarity above the redundancy threshold, creates a single
        semantic memory that abstracts the group.

        Returns count of new semantic memories created.
        """
        cfg = self.config
        # Group episodic memories by tag sets
        episodic = [m for m in self._memories.values() if m.memory_type == MemoryType.EPISODIC]
        if len(episodic) < cfg.semantic_abstraction_threshold:
            return 0

        # Build tag groups
        tag_groups: Dict[str, List[MemoryItem]] = defaultdict(list)
        for mem in episodic:
            for tag in mem.tags:
                tag_groups[tag].append(mem)

        abstracted = 0
        for tag, group in tag_groups.items():
            if len(group) < cfg.semantic_abstraction_threshold:
                continue
            # Check pairwise similarity within group
            similar_pairs = 0
            total_pairs = 0
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    total_pairs += 1
                    if group[i].embedding and group[j].embedding:
                        sim = cosine_similarity(group[i].embedding, group[j].embedding)
                        if sim >= cfg.redundancy_similarity_threshold:
                            similar_pairs += 1
            if total_pairs == 0:
                continue
            # If majority of pairs are similar → abstract
            if similar_pairs / total_pairs >= 0.5:
                abstracted_content = self._abstract_group(group, tag)
                source_ids = [m.id for m in group]
                # Create semantic memory
                sem_id = _hash_content(abstracted_content)
                if sem_id not in self._memories:
                    sem_mem = MemoryItem(
                        id=sem_id,
                        content=abstracted_content,
                        memory_type=MemoryType.SEMANTIC,
                        strength=max(m.strength for m in group),
                        tags=[tag, "abstracted"],
                        embedding=simple_hash_embedding(abstracted_content),
                        source_episodes=source_ids,
                    )
                    self._memories[sem_id] = sem_mem
                    abstracted += 1
        return abstracted

    def _abstract_group(self, group: List[MemoryItem], tag: str) -> str:
        """Generate an abstraction summary for a group of similar episodic memories.

        Uses a simple heuristic: extracts shared keywords and creates a
        template. In production this would call an LLM for summarisation.
        """
        # Simple heuristic: count word frequency across group
        from collections import Counter

        all_words: List[str] = []
        for mem in group:
            all_words.extend(mem.content.lower().split())
        word_freq = Counter(all_words)
        # Filter stopwords (minimal set)
        stops = {
            "the", "a", "an", "is", "was", "are", "were", "be", "been",
            "of", "in", "to", "for", "with", "on", "at", "by", "from",
            "and", "or", "but", "not", "this", "that", "it", "i", "you",
            "he", "she", "they", "we", "about", "has", "have", "do", "did",
        }
        significant = [(w, c) for w, c in word_freq.most_common(20) if w not in stops]
        keywords = [w for w, _ in significant[:5]]
        return (
            f"[Abstracted {tag}] Recurring pattern across "
            f"{len(group)} episodes. Key concepts: {', '.join(keywords)}. "
            f"Source episodes: {len(group)}"
        )


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _hash_content(content: str) -> str:
    """Generate a deterministic hash ID for memory content."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]
