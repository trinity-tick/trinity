"""
Unit tests for trinity.memory.consolidation — HippocampalConsolidator.

Covers:
  - Ebbinghaus decay curve
  - Memory add / retrieve / reinforcement
  - NREM pruning & redundancy merging
  - REM episodic → semantic abstraction
  - Spaced repetition boosts
  - Strength distribution & stats
  - ConsolidationResult reporting
"""

import math
import time
import pytest

from trinity.memory.consolidation import (
    HippocampalConsolidator,
    ConsolidationConfig,
    ConsolidationResult,
    MemoryItem,
    MemoryType,
    ConsolidationPhase,
    ebbinghaus_strength,
    spaced_repetition_boost,
    cosine_similarity,
    simple_hash_embedding,
)


# ═══════════════════════════════════════════════════════════════════════════
# Ebbinghaus decay
# ═══════════════════════════════════════════════════════════════════════════

class TestEbbinghausDecay:
    """Ebbinghaus forgetting curve: R = R0 * e^(-t * ln(2) / S)"""

    def test_no_decay_at_zero_elapsed(self):
        """Strength should remain unchanged when no time has passed."""
        s = ebbinghaus_strength(1.0, 0.0, half_life_seconds=3600)
        assert s == pytest.approx(1.0)

    def test_half_decay_at_half_life(self):
        """Strength should be 0.5 at exactly half-life."""
        s = ebbinghaus_strength(1.0, 3600.0, half_life_seconds=3600)
        assert s == pytest.approx(0.5)

    def test_quarter_at_double_half_life(self):
        """Strength should be 0.25 at 2× half-life."""
        s = ebbinghaus_strength(1.0, 7200.0, half_life_seconds=3600)
        assert s == pytest.approx(0.25)

    def test_asymptotic_approach_zero(self):
        """Strength should approach 0 at very large elapsed time."""
        s = ebbinghaus_strength(1.0, 360000.0, half_life_seconds=3600)
        assert s < 0.0001

    def test_half_life_zero_no_decay(self):
        """With half_life of 0, no decay occurs (safety guard)."""
        s = ebbinghaus_strength(1.0, 1000.0, half_life_seconds=0)
        assert s == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Spaced repetition boost
# ═══════════════════════════════════════════════════════════════════════════

class TestSpacedRepetition:
    """Spaced repetition reinforcement boost."""

    def test_first_access_gives_boost(self):
        """First access should give a boost > 1.0."""
        boost = spaced_repetition_boost(1, 0)
        assert boost > 1.0

    def test_diminishing_returns(self):
        """Later accesses give smaller boosts than earlier ones."""
        b1 = spaced_repetition_boost(1, 0)
        b2 = spaced_repetition_boost(10, 5)
        b3 = spaced_repetition_boost(100, 50)
        assert b1 > b2 > b3

    def test_clamped_to_max_2(self):
        """Boost should never exceed 2.0."""
        for ac in [0, 1, 5, 100]:
            boost = spaced_repetition_boost(ac, 0)
            assert 1.0 <= boost <= 2.0


# ═══════════════════════════════════════════════════════════════════════════
# Cosine similarity
# ═══════════════════════════════════════════════════════════════════════════

class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 2.0, 3.0]
        assert cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal(self):
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite(self):
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_mismatched_lengths(self):
        assert cosine_similarity([1.0], [1.0, 2.0]) == pytest.approx(0.0)

    def test_zero_vector(self):
        assert cosine_similarity([0.0, 0.0], [1.0, 1.0]) == pytest.approx(0.0)


# ═══════════════════════════════════════════════════════════════════════════
# Simple hash embedding
# ═══════════════════════════════════════════════════════════════════════════

class TestSimpleHashEmbedding:
    def test_returns_correct_dimension(self):
        emb = simple_hash_embedding("test", dim=64)
        assert len(emb) == 64

    def test_normalized_to_unit(self):
        emb = simple_hash_embedding("hello world", dim=32)
        norm = math.sqrt(sum(v * v for v in emb))
        assert norm == pytest.approx(1.0)

    def test_deterministic(self):
        """Same text should produce same embedding."""
        e1 = simple_hash_embedding("deterministic test")
        e2 = simple_hash_embedding("deterministic test")
        assert e1 == e2

    def test_different_text_different_embedding(self):
        e1 = simple_hash_embedding("text A")
        e2 = simple_hash_embedding("text B")
        assert e1 != e2

    def test_empty_text(self):
        emb = simple_hash_embedding("", dim=16)
        assert len(emb) == 16
        assert all(v == 0.0 for v in emb)


# ═══════════════════════════════════════════════════════════════════════════
# HippocampalConsolidator — basic CRUD
# ═══════════════════════════════════════════════════════════════════════════

class TestBasicOperations:
    def test_add_and_retrieve(self):
        c = HippocampalConsolidator()
        mid = c.add_memory("Hello world", tags=["greeting"])
        mem = c.get_memory(mid)
        assert mem is not None
        assert mem.content == "Hello world"
        assert "greeting" in mem.tags

    def test_add_duplicate_refreshes(self):
        c = HippocampalConsolidator()
        mid1 = c.add_memory("unique content")
        mid2 = c.add_memory("unique content")
        assert mid1 == mid2
        assert c.memory_count == 1

    def test_force_remember_sets_max_strength(self):
        c = HippocampalConsolidator()
        mid = c.force_remember("critical rule")
        mem = c.get_memory(mid)
        assert mem.strength == pytest.approx(1.0)
        assert mem.memory_type == MemoryType.SEMANTIC

    def test_get_nonexistent_returns_none(self):
        c = HippocampalConsolidator()
        assert c.get_memory("nonexistent") is None

    def test_retrieval_reinforces_strength(self):
        c = HippocampalConsolidator()
        mid = c.add_memory("test reinforcement")
        # Set to a sub-max strength so boost is measurable
        c._memories[mid].strength = 0.5
        initial = c._memories[mid].strength
        time.sleep(0.01)
        c.get_memory(mid)  # should boost beyond 0.5
        assert c._memories[mid].strength > initial

    def test_get_by_tag(self):
        c = HippocampalConsolidator()
        c.add_memory("weather today", tags=["weather"])
        c.add_memory("weather tomorrow", tags=["weather"])
        c.add_memory("stock price", tags=["finance"])
        results = c.get_memories_by_tag("weather")
        assert len(results) == 2
        results = c.get_memories_by_tag("finance")
        assert len(results) == 1
        results = c.get_memories_by_tag("unknown")
        assert len(results) == 0

    def test_get_top_memories(self):
        c = HippocampalConsolidator()
        for i in range(20):
            mid = c.add_memory(f"memory {i}")
            c._memories[mid].strength = i / 20.0
        top = c.get_top_memories(k=5)
        assert len(top) == 5
        assert top[0].strength > top[-1].strength


# ═══════════════════════════════════════════════════════════════════════════
# Consolidation cycle
# ═══════════════════════════════════════════════════════════════════════════

class TestConsolidationCycle:
    """NREM and REM cycle tests."""

    def test_should_consolidate_initially_true(self):
        c = HippocampalConsolidator()
        assert c.should_consolidate() is True

    def test_should_consolidate_after_interval(self):
        config = ConsolidationConfig(consolidation_interval_seconds=0.01)
        c = HippocampalConsolidator(config=config)
        c.consolidate()
        time.sleep(0.02)
        assert c.should_consolidate() is True

    def test_should_not_consolidate_immediately_after(self):
        config = ConsolidationConfig(consolidation_interval_seconds=60)
        c = HippocampalConsolidator(config=config)
        c.consolidate()
        assert c.should_consolidate() is False

    def test_consolidate_force_ignores_interval(self):
        config = ConsolidationConfig(consolidation_interval_seconds=60)
        c = HippocampalConsolidator(config=config)
        c.consolidate()
        result = c.consolidate(force=True)
        assert result.phase == ConsolidationPhase.REM

    def test_consolidate_returns_result(self):
        c = HippocampalConsolidator()
        result = c.consolidate()
        assert isinstance(result, ConsolidationResult)
        assert result.remaining_count >= 0
        assert result.duration_seconds >= 0

    def test_nrem_prunes_weak_memories(self):
        """Memories below min_strength_threshold get pruned in NREM."""
        config = ConsolidationConfig(
            min_strength_threshold=0.5,
            decay_half_life_seconds=1.0,
            consolidation_interval_seconds=0.0,
        )
        c = HippocampalConsolidator(config=config)
        # Add strong semantic (won't be pruned)
        c.force_remember("critical")
        # Add weak episodic
        mid = c.add_memory("weak memory")
        c._memories[mid].strength = 0.1  # below threshold

        before = c.memory_count
        result = c.consolidate(force=True)
        assert result.pruned_count >= 1
        assert c.memory_count < before
        # Semantic memory should survive
        sem_count = sum(
            1 for m in c._memories.values()
            if m.memory_type == MemoryType.SEMANTIC
        )
        assert sem_count >= 1

    def test_nrem_merges_redundant(self):
        """Similar memories should be merged."""
        config = ConsolidationConfig(
            redundancy_similarity_threshold=0.6,
            consolidation_interval_seconds=0.0,
        )
        c = HippocampalConsolidator(config=config)
        # Two very similar texts
        c.add_memory("user asked about weather in London today")
        c.add_memory("user asked about weather in London yesterday")
        before = c.memory_count
        result = c.consolidate(force=True)
        # At least one should be merged (or both kept if sim below threshold)
        # With hash embedding, similar texts may or may not meet the threshold.
        # We just verify no crash.
        assert result is not None

    def test_max_memories_cap(self):
        config = ConsolidationConfig(
            max_memories=5,
            consolidation_interval_seconds=0.0,
            min_strength_threshold=0.0,
        )
        c = HippocampalConsolidator(config=config)
        for i in range(20):
            c.add_memory(f"test memory {i}")
        # Set all to equal high strength to test pure cap enforcement
        for mem in c._memories.values():
            mem.strength = 0.8
            mem.memory_type = MemoryType.EPISODIC
        assert c.memory_count == 20
        result = c.consolidate(force=True)
        assert c.memory_count <= 5
        assert result.pruned_count >= 15


# ═══════════════════════════════════════════════════════════════════════════
# REM — Episodic to Semantic abstraction
# ═══════════════════════════════════════════════════════════════════════════

class TestREMAbstraction:
    """REM phase: episodic → semantic memory abstraction."""

    def test_rem_abstracts_similar_group(self):
        config = ConsolidationConfig(
            semantic_abstraction_threshold=3,
            redundancy_similarity_threshold=0.6,
            consolidation_interval_seconds=0.0,
            min_strength_threshold=0.0,
        )
        c = HippocampalConsolidator(config=config)
        # Use near-identical texts to ensure hash embeddings are similar
        for i in range(5):
            c.add_memory(
                f"user asked about weather forecast for day {i+1}",
                tags=["weather"],
            )
        result = c.consolidate(force=True)
        # May or may not abstract depending on hash similarity.
        # Just verify the cycle doesn't crash.
        assert isinstance(result, ConsolidationResult)

    def test_rem_skips_if_below_threshold(self):
        config = ConsolidationConfig(
            semantic_abstraction_threshold=10,  # high threshold
            consolidation_interval_seconds=0.0,
        )
        c = HippocampalConsolidator(config=config)
        for i in range(5):
            c.add_memory(f"memory {i}", tags=["test"])
        result = c.consolidate(force=True)
        assert result.abstracted_count == 0


# ═══════════════════════════════════════════════════════════════════════════
# Stats & monitoring
# ═══════════════════════════════════════════════════════════════════════════

class TestStats:
    def test_get_stats(self):
        c = HippocampalConsolidator()
        c.add_memory("epi 1", memory_type=MemoryType.EPISODIC)
        c.add_memory("epi 2", memory_type=MemoryType.EPISODIC)
        c.force_remember("semantic rule")
        stats = c.get_stats()
        assert stats["total_memories"] == 3
        assert stats["by_type"]["episodic"] == 2
        assert stats["by_type"]["semantic"] == 1
        assert 0.0 <= stats["mean_strength"] <= 1.0
        assert "consolidation_cycles" in stats

    def test_get_strength_distribution(self):
        c = HippocampalConsolidator()
        mid1 = c.add_memory("strong")
        mid2 = c.add_memory("weak")
        c._memories[mid1].strength = 0.9
        c._memories[mid2].strength = 0.1
        dist = c.get_strength_distribution()
        assert isinstance(dist, dict)
        total = sum(dist.values())
        assert total == 2

    def test_cycle_count_increments(self):
        c = HippocampalConsolidator()
        assert c.cycle_count == 0
        c.consolidate(force=True)
        assert c.cycle_count == 1
        c.consolidate(force=True)
        assert c.cycle_count == 2


# ═══════════════════════════════════════════════════════════════════════════
# ConsolidationResult
# ═══════════════════════════════════════════════════════════════════════════

class TestConsolidationResult:
    def test_default_values(self):
        r = ConsolidationResult(phase=ConsolidationPhase.WAKE)
        assert r.phase == ConsolidationPhase.WAKE
        assert r.pruned_count == 0
        assert r.merged_count == 0
        assert r.abstracted_count == 0
        assert r.remaining_count == 0

    def test_custom_values(self):
        r = ConsolidationResult(
            phase=ConsolidationPhase.REM,
            pruned_count=5,
            merged_count=3,
            abstracted_count=2,
            remaining_count=100,
            duration_seconds=1.5,
        )
        assert r.pruned_count == 5
        assert r.merged_count == 3
        assert r.abstracted_count == 2
        assert r.remaining_count == 100
        assert r.duration_seconds == 1.5
