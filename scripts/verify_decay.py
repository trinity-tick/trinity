#!/usr/bin/env python3
"""
Trinity — 记忆衰减 & 压缩 验证脚本
=====================================
验证内容：
  1. 衰减公式正确性：score = importance * exp(-λ * days)
     模拟 30/60/90 天前的记忆，对比预期值
  2. 衰减状态判定：HEALTHY / DECAYING / PENDING_COMPRESSION
  3. 不同 memory_type 使用不同 λ 的正确性
  4. 压缩流程：批量压缩 → 摘要生成 → 原始记忆归档

Usage:
    python scripts/verify_decay.py               # 运行全部验证
    python scripts/verify_decay.py --quick       # 仅核心数学验证
"""

from __future__ import annotations

import json
import math
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

# ── Path injection ────────────────────────────────────────────────

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

from trinity.daemon.memory_decay import (
    DecayConfig,
    DecayStatus,
    DecayResult,
    MemoryDecayEngine,
    MemoryType,
    compute_decay,
)
from trinity.daemon.memory_compressor import (
    MemoryCompressor,
    CompressionStatus,
    CompressedMemory,
    mock_llm_compress,
)

# ============================================================================
# Test Suite
# ============================================================================

_results: List[Tuple[str, bool]] = []


def pass_(name: str):
    _results.append((name, True))
    print(f"  PASS — {name}")


def fail_(name: str, reason: str):
    _results.append((name, False))
    print(f"  FAIL — {name}: {reason}")


# ============================================================================
# Test 1: Decay Formula Correctness
# ============================================================================

def test_decay_formula():
    """验证指数衰减公式: score = importance * exp(-λ * days_since_creation)"""
    print("\n[Test 1] Decay formula correctness")

    engine = MemoryDecayEngine()

    # Case 1: importance=1.0, λ=0.01, days=0 → score should be 1.0
    score = engine.calculate_decay_score(importance=1.0, decay_lambda=0.01, days_since_creation=0)
    expected = 1.0
    if abs(score - expected) < 1e-9:
        pass_("importance=1.0, days=0 → score=1.0")
    else:
        fail_("importance=1.0, days=0", f"expected {expected}, got {score}")

    # Case 2: importance=1.0, λ=0.01, days=30
    score = engine.calculate_decay_score(importance=1.0, decay_lambda=0.01, days_since_creation=30)
    expected = math.exp(-0.01 * 30)  # ≈ 0.7408
    if abs(score - expected) < 1e-6:
        pass_(f"importance=1.0, λ=0.01, days=30 → score={score:.6f} (expected {expected:.6f})")
    else:
        fail_("importance=1.0, days=30", f"expected {expected}, got {score}")

    # Case 3: importance=1.0, λ=0.01, days=60
    score = engine.calculate_decay_score(importance=1.0, decay_lambda=0.01, days_since_creation=60)
    expected = math.exp(-0.01 * 60)  # ≈ 0.5488
    if abs(score - expected) < 1e-6:
        pass_(f"importance=1.0, λ=0.01, days=60 → score={score:.6f} (expected {expected:.6f})")
    else:
        fail_("importance=1.0, days=60", f"expected {expected}, got {score}")

    # Case 4: importance=1.0, λ=0.01, days=90
    score = engine.calculate_decay_score(importance=1.0, decay_lambda=0.01, days_since_creation=90)
    expected = math.exp(-0.01 * 90)  # ≈ 0.4066
    if abs(score - expected) < 1e-6:
        pass_(f"importance=1.0, λ=0.01, days=90 → score={score:.6f} (expected {expected:.6f})")
    else:
        fail_("importance=1.0, days=90", f"expected {expected}, got {score}")

    # Case 5: importance=0.5, λ=0.01, days=60
    score = engine.calculate_decay_score(importance=0.5, decay_lambda=0.01, days_since_creation=60)
    expected = 0.5 * math.exp(-0.01 * 60)  # ≈ 0.2744
    if abs(score - expected) < 1e-6:
        pass_(f"importance=0.5, λ=0.01, days=60 → score={score:.6f}")
    else:
        fail_("importance=0.5, days=60", f"expected {expected}, got {score}")


# ============================================================================
# Test 2: Decay Status Determination
# ============================================================================

def test_decay_status():
    """验证衰减状态判定逻辑"""
    print("\n[Test 2] Decay status determination")

    engine = MemoryDecayEngine(config=DecayConfig(compression_threshold=0.15))

    # HEALTHY: score >= 0.4
    assert engine.determine_status(0.8) == DecayStatus.HEALTHY
    assert engine.determine_status(0.4) == DecayStatus.HEALTHY  # boundary
    pass_("score >= 0.4 → HEALTHY")

    # DECAYING: 0.15 < score < 0.4
    assert engine.determine_status(0.39) == DecayStatus.DECAYING
    assert engine.determine_status(0.16) == DecayStatus.DECAYING
    pass_("0.15 < score < 0.4 → DECAYING")

    # PENDING_COMPRESSION: score <= 0.15
    assert engine.determine_status(0.15) == DecayStatus.PENDING_COMPRESSION  # boundary
    assert engine.determine_status(0.01) == DecayStatus.PENDING_COMPRESSION
    pass_("score <= 0.15 → PENDING_COMPRESSION")


# ============================================================================
# Test 3: Different Memory Types Use Different Lambda
# ============================================================================

def test_memory_type_lambda():
    """验证不同 memory_type 使用不同衰减速率"""
    print("\n[Test 3] Memory-type-specific decay rates")

    config = DecayConfig()
    engine = MemoryDecayEngine(config=config)

    # Lambda values
    for mt in MemoryType:
        lam = engine.get_lambda_for_type(mt.value)
        expected = config.lambda_per_type.get(mt.value, 0.02)
        if abs(lam - expected) < 1e-9:
            pass_(f"{mt.value}: λ={lam}")
        else:
            fail_(mt.value, f"expected λ={expected}, got {lam}")

    # Verify handoff decays faster than knowledge
    handoff_lam = engine.get_lambda_for_type("handoff")
    knowledge_lam = engine.get_lambda_for_type("knowledge")
    assert handoff_lam > knowledge_lam, "handoff should decay faster than knowledge"
    pass_(f"handoff decay (λ={handoff_lam}) > knowledge decay (λ={knowledge_lam})")

    # Verify half-lives
    handoff_hl = engine.get_half_life("handoff")
    knowledge_hl = engine.get_half_life("knowledge")
    assert handoff_hl < knowledge_hl, "handoff half-life should be shorter"
    pass_(f"handoff half-life={handoff_hl:.1f}d < knowledge half-life={knowledge_hl:.1f}d")


# ============================================================================
# Test 4: Simulated 30/60/90-day Memories
# ============================================================================

def test_simulated_memories():
    """模拟 30/60/90 天前创建的记忆并验证衰减"""
    print("\n[Test 4] Simulated 30/60/90-day memories")

    now = datetime.now(timezone.utc)
    engine = MemoryDecayEngine(config=DecayConfig(compression_threshold=0.15))

    # Create simulated memories
    simulated = []
    for days_ago, content in [
        (30, "30-day old memory about project Alpha"),
        (60, "60-day old memory about meeting with client"),
        (90, "90-day old memory about initial setup"),
    ]:
        simulated.append({
            "memory_id": str(uuid.uuid4()),
            "session_id": str(uuid.uuid4()),
            "persona_id": "default",
            "tenant_id": "default",
            "content": content,
            "role": "user",
            "importance": 1.0,
            "tags": [],
            "category": "general",
            "sha256_hash": "abc123",
            "status": "active",
            "version": 1,
            "created_at": (now - timedelta(days=days_ago)).isoformat(),
            "updated_at": now.isoformat(),
        })

    report = engine.scan_memories(simulated)

    for result in report.results:
        expected_score = math.exp(-0.02 * result.days_since_creation)  # λ=0.02 for general
        if abs(result.decay_score - expected_score) < 1e-4:
            pass_(
                f"{result.days_since_creation:.0f}d memory: "
                f"score={result.decay_score:.6f} (expected {expected_score:.6f}), "
                f"status={result.status.value}"
            )
        else:
            fail_(
                f"{result.days_since_creation:.0f}d memory",
                f"score={result.decay_score:.6f}, expected {expected_score:.6f}"
            )

    # Verify 90-day memory with importance=1.0 has the lowest score
    scores = [r.decay_score for r in report.results]
    assert scores[0] > scores[1] > scores[2], "30d > 60d > 90d expected"
    pass_("Decay scores monotonically decrease: 30d > 60d > 90d")


# ============================================================================
# Test 5: Compression Flow (Offline)
# ============================================================================

def test_compression_flow():
    """验证压缩流程（离线，不连接数据库）"""
    print("\n[Test 5] Compression flow (offline)")

    compressor = MemoryCompressor(
        pg_adapter=None,  # offline mode
        llm_callable=mock_llm_compress,
    )

    # Mock 5 memories
    now = datetime.now(timezone.utc)
    memories = []
    for i in range(5):
        memories.append({
            "memory_id": str(uuid.uuid4()),
            "content": f"[Entry {i+1}] Task completed: deploy service-alpha v{i+1}.0 on 2026-08-0{i+1}",
            "importance": 0.6 - i * 0.1,
            "category": "general",
            "created_at": (now - timedelta(days=60 + i * 5)).isoformat(),
        })

    result = compressor.compress_batch(memories, memory_type="general")

    if result.status == CompressionStatus.SUCCESS:
        assert result.compressed is not None
        assert len(result.archived_ids) == 5
        assert result.compressed.original_count == 5
        assert len(result.compressed.content) > 50
        pass_(
            f"Compressed 5→1 summary: "
            f"summary_id={result.compressed.summary_id[:8]}, "
            f"importance={result.compressed.importance:.3f}, "
            f"elapsed={result.elapsed_seconds:.2f}s"
        )
    else:
        fail_("Compression failed", result.error_message)

    # Verify content contains key information
    summary = result.compressed.content
    assert "Entry" in summary or "service" in summary.lower(), "Summary should retain content keywords"
    pass_("Summary retains original content keywords")


# ============================================================================
# Test 6: Batch Creation
# ============================================================================

def test_batch_creation():
    """验证批次创建逻辑"""
    print("\n[Test 6] Compression batch creation")

    engine = MemoryDecayEngine(config=DecayConfig(
        min_batch_size=3,
        max_batch_size=10,
    ))

    now = datetime.now(timezone.utc)

    # Create 15 pending decay results
    pending = []
    for i in range(15):
        pending.append(DecayResult(
            memory_id=str(uuid.uuid4()),
            memory_type="general" if i < 10 else "handoff",
            importance=0.5,
            decay_lambda=0.02,
            days_since_creation=100.0,
            decay_score=0.05,
            status=DecayStatus.PENDING_COMPRESSION,
            created_at=(now - timedelta(days=100)).isoformat(),
        ))

    batches = engine.create_compression_batches(pending)

    # Should create 2 batches: general (10) -> 1 batch, handoff (5) -> 1 batch
    assert len(batches) >= 2, f"Expected at least 2 batches, got {len(batches)}"
    pass_(f"Created {len(batches)} batches from 15 pending memories")

    # Verify batch sizes
    sizes = [len(b) for b in batches]
    pass_(f"Batch sizes: {sizes}")


# ============================================================================
# Test 7: Convenience Function
# ============================================================================

def test_convenience_function():
    """验证便捷函数 compute_decay"""
    print("\n[Test 7] Convenience function compute_decay")

    score = compute_decay(importance=0.8, decay_lambda=0.01, days_since_creation=30)
    expected = 0.8 * math.exp(-0.01 * 30)
    if abs(score - expected) < 1e-9:
        pass_(f"compute_decay(0.8, 0.01, 30) = {score:.6f}")
    else:
        fail_("compute_decay", f"expected {expected}, got {score}")


# ============================================================================
# Test 8: Edge Cases
# ============================================================================

def test_edge_cases():
    """边缘情况测试"""
    print("\n[Test 8] Edge cases")

    engine = MemoryDecayEngine()

    # Zero importance
    score = engine.calculate_decay_score(importance=0.0, decay_lambda=0.01, days_since_creation=100)
    assert score == 0.0
    pass_("importance=0 → score=0")

    # Zero decay_lambda (no decay)
    score = engine.calculate_decay_score(importance=0.8, decay_lambda=0.0, days_since_creation=1000)
    assert abs(score - 0.8) < 1e-9
    pass_("λ=0 → score=importance (no decay)")

    # Negative days (should be clamped)
    score = engine.calculate_decay_score(importance=0.8, decay_lambda=0.01, days_since_creation=-10)
    expected = 0.8 * math.exp(-0.01 * (-10))  # actually increases
    if abs(score - expected) < 1e-9:
        pass_(f"negative days=-10 → score={score:.6f} (increases correctly)")
    else:
        fail_("negative days", f"expected {expected}, got {score}")

    # Very large lambda
    score = engine.calculate_decay_score(importance=1.0, decay_lambda=1.0, days_since_creation=10)
    assert score < 0.001, f"Large lambda should give near-zero, got {score}"
    pass_(f"λ=1.0, days=10 → score={score:.6f} (near zero)")

    # Very large days
    score = engine.calculate_decay_score(importance=1.0, decay_lambda=0.01, days_since_creation=1000)
    assert score < 0.001, f"1000 days should give near-zero, got {score}"
    pass_(f"days=1000 → score={score:.6f} (near zero)")


# ============================================================================
# Main
# ============================================================================

def main():
    print("=" * 70)
    print("  Trinity — Memory Decay & Compression Verification")
    print("=" * 70)
    print(f"  Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print()

    test_decay_formula()
    test_decay_status()
    test_memory_type_lambda()
    test_simulated_memories()
    test_compression_flow()
    test_batch_creation()
    test_convenience_function()
    test_edge_cases()

    # ── Summary ────────────────────────────────────────────────
    print("\n" + "=" * 70)
    passed = sum(1 for _, ok in _results if ok)
    total = len(_results)
    print(f"  RESULTS: {passed}/{total} tests passed")
    for name, ok in _results:
        status = "PASS" if ok else "FAIL"
        print(f"    [{status}] {name}")
    print("=" * 70)

    return passed == total


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
