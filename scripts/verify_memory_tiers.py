#!/usr/bin/env python3
"""
Trinity — 三层记忆生命周期验证脚本
=======================================
Comprehensive verification of the three-tier memory lifecycle:

Tests:
  1. Core → Recall eviction on overflow
  2. Recall → Core promotion (high access frequency)
  3. Recall → Archival demotion (low access frequency)
  4. Core Memory token cap protection
  5. Persona block read-only protection
  6. Full lifecycle run (all three operations)
  7. Weighted scoring correctness
  8. Edge cases: empty tiers, single-block overflow

Usage:
    python scripts/verify_memory_tiers.py
    python scripts/verify_memory_tiers.py --verbose
"""

from __future__ import annotations

import math
import os
import sys
import time

# ── Path injection ────────────────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

from trinity.daemon.memory_tiers import (
    MemoryTier, BlockType, MemoryBlock, CoreMemory,
    RecallMemory, ArchivalMemory, MemoryTierManager,
    TierMigrationRecord,
    DEFAULT_CORE_TOKEN_LIMIT, DEFAULT_RECALL_PROMOTION_THRESHOLD,
    DEFAULT_RECALL_DEMOTION_THRESHOLD,
)


# ── Test Framework ────────────────────────────────────────────────────

VERBOSE = "--verbose" in sys.argv
TOTAL = 0
PASSED = 0
FAILED = 0


def _log(msg: str) -> None:
    if VERBOSE:
        print(f"  {msg}")


def describe(name: str) -> None:
    global TOTAL
    TOTAL += 1
    print(f"\n{'─' * 60}")
    print(f"[TEST {TOTAL}] {name}")
    print(f"{'─' * 60}")


def pass_(msg: str = "") -> None:
    global PASSED
    PASSED += 1
    label = f" - {msg}" if msg else ""
    print(f"  ✓ PASS{label}")


def fail_(msg: str) -> None:
    global FAILED
    FAILED += 1
    print(f"  ✗ FAIL: {msg}")


def check(condition: bool, label: str) -> bool:
    if condition:
        pass_(label)
    else:
        fail_(label)
    return condition


def check_eq(actual, expected, label: str) -> bool:
    if actual == expected:
        pass_(f"{label} = {actual}")
        return True
    else:
        fail_(f"{label}: expected {expected}, got {actual}")
        return False


# ── Test Helpers ──────────────────────────────────────────────────────

def _make_lorem_tokens(n: int) -> str:
    """Generate ~n tokens of filler text (~4 chars/token)."""
    base = (
        "Lorem ipsum dolor sit amet consectetur adipiscing elit "
        "sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. "
    )
    needed = n * 4
    repeats = math.ceil(needed / len(base))
    return (base * repeats)[:needed]


# ============================================================================
# Test 1: Core → Recall Eviction on Overflow
# ============================================================================


def test_core_to_recall_eviction():
    """验证 Core 溢出时自动降级到 Recall。"""
    describe("Core → Recall eviction on overflow")

    manager = MemoryTierManager(core_token_limit=200)

    # Add a read-only persona block (should survive eviction)
    manager.core.set_block("persona", "I am a test agent.", readonly=True)
    _log(f"Persona block: {manager.core.get_block('persona').estimated_tokens} tokens")

    # Add many blocks to overflow
    for i in range(10):
        manager.core.set_block(
            f"block_{i}",
            _make_lorem_tokens(40),  # ~40 tokens each
            importance=0.2 + i * 0.05,
        )

    before_tokens = manager.core.total_tokens
    before_blocks = len(manager.core.list_blocks())
    _log(f"Before eviction: {before_tokens} tokens, {before_blocks} blocks")

    check(before_tokens > 200, f"Core is overflowing ({before_tokens} > 200)")

    # Trigger eviction
    migrations = manager.evict_from_core()
    _log(f"Evictions: {len(migrations)}")

    after_tokens = manager.core.total_tokens
    after_blocks = len(manager.core.list_blocks())

    check(after_tokens <= 200, f"After eviction: {after_tokens} <= 200 tokens")
    check(len(migrations) > 0, f"{len(migrations)} blocks evicted to Recall")
    check(
        manager.core.get_block("persona") is not None,
        "Persona block survives eviction",
    )
    check(manager.recall.size >= len(migrations), f"Recall has {manager.recall.size} blocks")

    for m in migrations:
        check(m.from_tier == MemoryTier.CORE, f"Migration from CORE: {m.block_id[:16]}")
        check(m.to_tier == MemoryTier.RECALL, f"Migration to RECALL: {m.block_id[:16]}")


# ============================================================================
# Test 2: Recall → Core Promotion
# ============================================================================


def test_recall_to_core_promotion():
    """验证 Recall 高频访问块提升到 Core。"""
    describe("Recall → Core promotion (high access frequency)")

    manager = MemoryTierManager(
        core_token_limit=500,
        promotion_threshold=3,  # Lower threshold for testing
    )

    # Setup: add some recall blocks and simulate high access
    for i in range(8):
        block = manager.recall.add_entry(
            content=f"Important fact #{i}: user prefers dark mode and Python type hints.",
            label=f"fact_{i}",
            importance=0.7 + i * 0.02,
        )

    # Simulate high access on first 3 blocks
    for i in range(3):
        block = manager.recall.get_block(
            list(manager.recall._blocks.values())[i].block_id
        )
        for _ in range(5):
            manager.recall.record_access(block.block_id)

    # Verify access counts
    for i, b in enumerate(manager.recall.list_blocks()):
        _log(f"  block {b.label}: access_count={b.access_count}, freq={b.access_frequency:.2f}/h")

    before_core = manager.core.total_tokens
    before_recall = manager.recall.size

    # Promote
    migrations = manager.promote_to_core()
    _log(f"Promotions: {len(migrations)}")

    after_core = manager.core.total_tokens
    after_recall = manager.recall.size

    check(len(migrations) > 0, f"{len(migrations)} blocks promoted")
    check(after_core > before_core, f"Core grew: {before_core} → {after_core} tokens")
    check(after_recall < before_recall, f"Recall shrunk: {before_recall} → {after_recall}")

    for m in migrations:
        check(m.from_tier == MemoryTier.RECALL, f"From RECALL: {m.block_id[:16]}")
        check(m.to_tier == MemoryTier.CORE, f"To CORE: {m.block_id[:16]}")

    # Verify promoted blocks are now in Core
    core_labels = [b.label for b in manager.core.list_blocks()]
    _log(f"Core labels after promotion: {core_labels}")
    check(len(core_labels) > 0, "Core has blocks after promotion")


# ============================================================================
# Test 3: Recall → Archival Demotion
# ============================================================================


def test_recall_to_archival_demotion():
    """验证 Recall 低频/低分记忆降级到 Archival。"""
    describe("Recall → Archival demotion (low frequency)")

    manager = MemoryTierManager(demotion_threshold=0.5)

    # Add 5 Recall blocks with low importance (will demote)
    for i in range(5):
        block = manager.recall.add_entry(
            content=f"Old conversation #{i}: user talked about weather.",
            label=f"old_chat_{i}",
            importance=0.05,  # Very low
        )
        # Simulate age by hacking created_at (7+ days ago)
        block.created_at = time.time() - 86400 * 30  # 30 days old

    # Also add 2 high-importance blocks (should NOT demote)
    for i in range(2):
        block = manager.recall.add_entry(
            content=f"Important recall #{i}: critical project deadline info.",
            label=f"important_{i}",
            importance=0.9,
        )
        block.created_at = time.time() - 86400 * 5  # 5 days

    before_recall = manager.recall.size
    before_archival = manager.archival.size

    _log(f"Before demotion: recall={before_recall}, archival={before_archival}")

    migrations = manager.demote_to_archival()
    _log(f"Demotions: {len(migrations)}")

    after_recall = manager.recall.size
    after_archival = manager.archival.size

    check(len(migrations) > 0, f"{len(migrations)} blocks demoted")
    check(
        len(migrations) <= 5,
        f"Only low-importance blocks demoted: {len(migrations)} <= 5",
    )
    check(after_recall < before_recall, f"Recall shrunk: {before_recall} → {after_recall}")
    check(after_archival > before_archival, f"Archival grew: {before_archival} → {after_archival}")

    for m in migrations:
        check(m.from_tier == MemoryTier.RECALL, f"From RECALL: {m.block_id[:16]}")
        check(m.to_tier == MemoryTier.ARCHIVAL, f"To ARCHIVAL: {m.block_id[:16]}")

    # Verify high-importance blocks remain in Recall
    remaining_labels = [b.label for b in manager.recall.list_blocks()]
    for i in range(2):
        check(f"important_{i}" in remaining_labels, f"Important block 'important_{i}' stays in Recall")


# ============================================================================
# Test 4: Core Memory Token Cap Protection
# ============================================================================


def test_core_token_cap():
    """验证 Core Memory 的 token 限制保护。"""
    describe("Core Memory token cap protection")

    # Test with a small limit
    core = CoreMemory(token_limit=100)

    # Add blocks until overflow
    blocks_added = 0
    for i in range(50):
        try:
            core.set_block(
                f"block_{i}",
                _make_lorem_tokens(30),  # ~30 tokens each
            )
            blocks_added += 1
            if core.is_overflowing:
                break
        except ValueError:
            break

    stats = core.stats()

    _log(f"Blocks added before overflow: {blocks_added}")
    _log(f"Stats: tokens={stats.total_tokens}, blocks={stats.total_blocks}, "
         f"util={stats.utilization_pct}%")

    check(stats.total_tokens > 0, f"Core has {stats.total_tokens} tokens")
    check(core.is_overflowing, "Core correctly detects overflow")

    # Test eviction to bring back under limit
    candidates = core.get_eviction_candidates(target_reduction=stats.total_tokens - 100)
    check(len(candidates) > 0, f"{len(candidates)} eviction candidates identified")

    # Verify candidates are sorted (low importance first)
    if len(candidates) >= 2:
        imp_sorted = all(
            candidates[i].importance <= candidates[i + 1].importance
            for i in range(len(candidates) - 1)
        )
        check(imp_sorted, "Eviction candidates sorted by importance (ascending)")

    # Test set_block raises error for readonly block
    core.set_block("persona", "I am a helpful agent.", readonly=True)
    try:
        core.set_block("persona", "I am a malicious agent.")
        fail_("Should have raised ValueError for readonly block overwrite")
    except ValueError as e:
        pass_("Read-only block protection prevents overwrite")
        _log(f"  ValueError: {e}")


# ============================================================================
# Test 5: Persona Block Read-Only Protection
# ============================================================================


def test_persona_readonly_protection():
    """验证 persona block 只读保护。"""
    describe("Persona block read-only protection")

    manager = MemoryTierManager(core_token_limit=500)

    # Set persona block as read-only
    manager.core.set_block(
        "system_persona",
        "You are Trinity, a helpful memory assistant.",
        block_type=BlockType.PERSONA,
        readonly=True,
    )

    # Attempt illegal write
    try:
        manager.core.set_block("system_persona", "You are evil.")
        fail_("set_block should raise ValueError for readonly persona")
    except ValueError:
        pass_("set_block rejected overwrite of readonly persona block")

    # Verify persona unchanged
    block = manager.core.get_block("system_persona")
    check(block is not None, "Persona block exists")
    check(
        "Trinity" in block.content,
        "Persona content unchanged (still 'Trinity')",
    )
    check(block.is_readonly, f"is_readonly = {block.is_readonly}")

    # Test that force_set_block bypasses protection
    manager.core.force_set_block(
        "system_persona",
        "You are Trinity v2, upgraded assistant.",
        block_type=BlockType.PERSONA,
        readonly=True,
    )
    block2 = manager.core.get_block("system_persona")
    check(
        "v2" in block2.content,
        "force_set_block bypasses readonly for admin override",
    )

    # Test that eviction tries to preserve persona
    for i in range(20):
        try:
            manager.core.set_block(f"temp_{i}", _make_lorem_tokens(50))
        except ValueError:
            pass

    migrations = manager.evict_from_core()
    persona_still_there = manager.core.get_block("system_persona") is not None
    check(persona_still_there, "Persona block survives eviction overflow")


# ============================================================================
# Test 6: Full Lifecycle Run
# ============================================================================


def test_full_lifecycle():
    """验证完整生命周期：eviction → promotion → demotion。"""
    describe("Full lifecycle: eviction → promotion → demotion")

    manager = MemoryTierManager(
        core_token_limit=300,
        promotion_threshold=2,
        demotion_threshold=0.3,
    )

    # Seed Core with persona + many blocks to trigger overflow
    manager.core.set_block("persona", "I am Trinity.", readonly=True)
    for i in range(15):
        manager.core.set_block(f"data_{i}", _make_lorem_tokens(40), importance=0.3)

    # Seed Recall with high-access blocks (should get promoted)
    for i in range(5):
        block = manager.recall.add_entry(
            content=f"Frequently accessed memory #{i}.",
            label=f"hot_{i}",
            importance=0.8,
        )
        # Simulate high access
        for _ in range(10):
            manager.recall.record_access(block.block_id)

    # Seed Recall with old low-access blocks (should get demoted)
    for i in range(5):
        block = manager.recall.add_entry(
            content=f"Cold memory #{i} about nothing.",
            label=f"cold_{i}",
            importance=0.05,
        )
        block.created_at = time.time() - 86400 * 60  # 60 days old

    _log("Before lifecycle:")
    _log(f"  Core: {manager.core.stats()}")
    _log(f"  Recall: {manager.recall.stats()}")
    _log(f"  Archival: {manager.archival.stats()}")

    migrations = manager.run_lifecycle()

    _log(f"After lifecycle — {len(migrations)} migrations:")
    for m in migrations:
        _log(f"  {m.from_tier.value} → {m.to_tier.value}: {m.reason}")

    # Verify each migration type
    evictions = [m for m in migrations if m.from_tier == MemoryTier.CORE]
    promotions = [m for m in migrations if (
        m.from_tier == MemoryTier.RECALL and m.to_tier == MemoryTier.CORE
    )]
    demotions = [m for m in migrations if (
        m.from_tier == MemoryTier.RECALL and m.to_tier == MemoryTier.ARCHIVAL
    )]

    check(len(evictions) > 0, f"Evictions (Core→Recall): {len(evictions)}")
    check(len(promotions) > 0, f"Promotions (Recall→Core): {len(promotions)}")
    check(len(demotions) > 0, f"Demotions (Recall→Archival): {len(demotions)}")
    check(len(migrations) > 0, f"Total migrations: {len(migrations)}")

    # Verify final state is sane
    final_core = manager.core.stats()
    check(
        final_core.total_tokens <= manager.core.token_limit,
        f"Core tokens within limit: {final_core.total_tokens} <= {manager.core.token_limit}",
    )
    check(
        manager.core.get_block("persona") is not None,
        "Persona block survived full lifecycle",
    )

    snapshot = manager.snapshot()
    _log(f"Snapshot summary: {json.dumps({k: v for k, v in snapshot.items() if k != 'core'}, indent=2, default=str)}")


# ============================================================================
# Test 7: Weighted Scoring Correctness
# ============================================================================


def test_weighted_scoring():
    """验证加权评分正确性。"""
    describe("Weighted scoring correctness")

    manager = MemoryTierManager(
        w_recency=0.40, w_importance=0.35, w_access=0.25,
    )

    # Block 1: fresh + important + high access → high score
    fresh_block = MemoryBlock(
        block_id="test_fresh",
        label="fresh",
        content="Fresh important block.",
        importance=0.95,
        access_count=50,
        last_accessed=time.time(),
        created_at=time.time() - 60,  # 1 minute ago
    )

    # Block 2: old + unimportant + low access → low score
    old_block = MemoryBlock(
        block_id="test_old",
        label="old",
        content="Old unimportant block.",
        importance=0.05,
        access_count=0,
        created_at=time.time() - 86400 * 180,  # 180 days ago
    )

    fresh_score = manager.compute_tier_score(fresh_block)
    old_score = manager.compute_tier_score(old_block)

    _log(f"Fresh block score: {fresh_score:.4f}")
    _log(f"Old block score: {old_score:.4f}")

    check(fresh_score > 0.6, f"Fresh+important block score > 0.6: {fresh_score:.4f}")
    check(old_score < 0.2, f"Old+unimportant block score < 0.2: {old_score:.4f}")
    check(fresh_score > old_score, f"Fresh ({fresh_score:.4f}) > Old ({old_score:.4f})")

    # Verify score is in [0, 1]
    check(0 <= fresh_score <= 1, f"Fresh score {fresh_score:.4f} in [0,1]")
    check(0 <= old_score <= 1, f"Old score {old_score:.4f} in [0,1]")


# ============================================================================
# Test 8: Edge Cases
# ============================================================================


def test_edge_cases():
    """边缘情况测试。"""
    describe("Edge cases: empty tiers, single-block overflow")

    # Empty tiers
    manager = MemoryTierManager()
    migrations = manager.run_lifecycle()
    check(len(migrations) == 0, "Empty tiers produce zero migrations")

    snapshot = manager.snapshot()
    check(snapshot["recall"]["total_blocks"] == 0, "Empty Recall = 0 blocks")
    check(snapshot["archival"]["total_blocks"] == 0, "Empty Archival = 0 blocks")

    # Single block overflow
    manager2 = MemoryTierManager(core_token_limit=10)
    manager2.core.set_block("big_block", _make_lorem_tokens(20))
    check(manager2.core.is_overflowing, "Single block overflow detected")
    migrations = manager2.evict_from_core()
    check(len(migrations) > 0, "Single block evicted on overflow")

    # Read-only block search in Recall/Archival
    recall = RecallMemory()
    recall.add_entry("User likes Python.", label="pref_python", tags=["preference", "language"])
    results = recall.search("python")
    check(len(results) > 0, f"Recall keyword search returns {len(results)} results")
    check("Python" in results[0][0].content, "Search result matches content")

    results2 = recall.search_by_tags(["language"])
    check(len(results2) == 1, "Tag search returns 1 result")
    results3 = recall.search_by_tags(["language", "nonexistent"], match_all=True)
    check(len(results3) == 0, "AND tag search correctly returns empty")

    # Archival search
    archival = ArchivalMemory()
    block = MemoryBlock(
        block_id="arch_test",
        label="arch_test",
        content="Project Alpha was started in March 2025.",
        importance=0.7,
        created_at=time.time() - 86400 * 200,
    )
    archival.archive_block(block)
    results4 = archival.search("Project Alpha")
    check(len(results4) > 0, f"Archival search returns {len(results4)} results")

    # Core context assembly
    core = CoreMemory(token_limit=500)
    core.set_block("persona", "I am an agent.", readonly=True)
    core.set_block("task", "Analyze sales data.")
    ctx = core.assemble_context()
    check("[persona]" in ctx, "Context includes persona header")
    check("[task]" in ctx, "Context includes task header")
    check("Analyze sales data" in ctx, "Context includes task content")

    # Migration audit trail
    check(len(manager2._migrations) > 0, "Migration audit trail is populated")


# ============================================================================
# Main
# ============================================================================

import json  # noqa: E402 (needed in test_full_lifecycle)


def main():
    print("=" * 60)
    print("Trinity Three-Tier Memory Lifecycle Verification")
    print("=" * 60)
    print(f"  Module: trinity.daemon.memory_tiers")
    print(f"  Default Core limit: {DEFAULT_CORE_TOKEN_LIMIT} tokens")
    print(f"  Promotion threshold: {DEFAULT_RECALL_PROMOTION_THRESHOLD} accesses")
    print(f"  Demotion threshold:  {DEFAULT_RECALL_DEMOTION_THRESHOLD} freq")
    print()

    tests = [
        test_core_to_recall_eviction,
        test_recall_to_core_promotion,
        test_recall_to_archival_demotion,
        test_core_token_cap,
        test_persona_readonly_protection,
        test_full_lifecycle,
        test_weighted_scoring,
        test_edge_cases,
    ]

    for test_fn in tests:
        try:
            test_fn()
        except Exception as e:
            fail_(f"Test raised exception: {type(e).__name__}: {e}")
            if VERBOSE:
                import traceback
                traceback.print_exc()

    # ── Summary ────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"SUMMARY: {PASSED}/{TOTAL} passed, {FAILED} failed")
    print(f"{'=' * 60}")

    if FAILED > 0:
        print(f"\nFAILURES: {FAILED} test(s) failed!")
        sys.exit(1)
    else:
        print("All tests passed!")
        sys.exit(0)


if __name__ == "__main__":
    main()
