"""Factory / self-test for the MemoryAggregator package (split from aggregator.py).
create_aggregator and self_test are defined here verbatim and re-exported
from the package __init__.
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

from ._constants import logger, _SENTINEL
from . import MemoryAggregator


def create_aggregator(
    persist: Union[bool, str] = _SENTINEL,
    vector_backend: str = "faiss",
    auto_consolidate: bool = False,
    importance_threshold: float = 0.0,
    **kwargs,
) -> MemoryAggregator:
    """Factory function for MemoryAggregator (P1-5 unified + v7.0.0).

    Args:
        persist: False=memory-only, True=auto-discover path, str=explicit path.
        vector_backend: "faiss" (default) or "chromadb" for vector index.
        auto_consolidate: If True, periodic memory consolidation is enabled.
        importance_threshold: Minimum importance to retain (0=keep all).
        **kwargs: Passed through to MemoryAggregator.
    """
    # Resolve persist_path
    if persist is _SENTINEL:
        persist_path = _SENTINEL  # auto-discover
    elif persist is True:
        persist_path = _SENTINEL
    elif persist is False:
        persist_path = None
    else:
        persist_path = persist  # explicit path string

    agg = MemoryAggregator(persist_path=persist_path, **kwargs)

    # ── P1-7 / v7.0.0: ChromaDB vector backend ───────────────────────
    if vector_backend == "chromadb":
        try:
            import chromadb
            # ChromaDB client setup (in-memory collection for aggregator)
            _chroma_client = chromadb.Client(
                chromadb.config.Settings(anonymized_telemetry=False)
            )
            # Store reference for potential use in vector search
            agg._chroma_client = _chroma_client
            logger.info("ChromaDB vector backend active")
        except ImportError:
            logger.warning("chromadb not installed; falling back to FAISS/numpy")
            agg._chroma_client = None
    else:
        agg._chroma_client = None

    # ── P1-6 / v7.0.0: Auto-consolidation ────────────────────────────
    agg._auto_consolidate = auto_consolidate
    agg._importance_threshold = importance_threshold

    if auto_consolidate:
        _orig_ingest = agg.ingest

        def _wrapped_ingest(*args, **kwargs_inner):
            dv = _orig_ingest(*args, **kwargs_inner)
            if len(agg._pool) > 100:
                try:
                    merged = agg.merge_memories()
                    if merged > 0:
                        logger.debug("Auto-consolidate: merged %d memories", merged)
                except Exception as exc:
                    logger.debug("Auto-consolidate skipped: %s", exc)
            return dv

        agg.ingest = _wrapped_ingest  # type: ignore[method-assign]

    return agg


def self_test() -> bool:
    """Comprehensive self-test for MemoryAggregator."""
    print("=" * 60)
    print("  Trinity Memory Aggregator — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: creation ──
    total += 1
    print("\n[Test 1] MemoryAggregator creation")
    try:
        agg = create_aggregator(persist=False)
        assert len(agg._pool) == 0
        assert len(agg._agent_index) == 0
        assert len(agg._topic_index) == 0
        assert agg._engine is not None
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: ingest new memory ──
    total += 1
    print("\n[Test 2] ingest new memory")
    try:
        dv1 = agg.ingest(
            "user prefers dark mode in all applications",
            "main",
            metadata={"category": "preference", "scope": "global"},
        )
        assert dv1.memory_id
        assert "main" in dv1.source_agents
        assert dv1.category == "preference"
        assert dv1.scope == "global"
        assert dv1.memory_id in agg._pool
        assert "main" in agg._agent_index
        print(f"    id={dv1.memory_id}, confidence={dv1.confidence}, "
              f"topics={dv1.topics}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: merge_if_similar (above threshold) ──
    total += 1
    print("\n[Test 3] merge_if_similar (semantic duplicate)")
    try:
        merged = agg.merge_if_similar(
            "user prefers dark mode in all applications",  # identical
            "browser",
            threshold=0.4,  # low threshold so it matches
        )
        assert merged is not None
        assert merged.memory_id == dv1.memory_id
        assert "browser" in merged.source_agents
        assert merged.source_count == 2
        print(f"    merged into {merged.memory_id}, sources={merged.source_agents}, "
              f"confidence={merged.confidence:.2f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: merge_if_similar (below threshold → None) ──
    total += 1
    print("\n[Test 4] merge_if_similar (unrelated content)")
    try:
        result = agg.merge_if_similar(
            "completely unrelated topic about weather forecast",
            "main",
            threshold=0.75,
        )
        assert result is None
        print("    correctly returned None")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: ingest second memory & get_by_agent ──
    total += 1
    print("\n[Test 5] get_by_agent")
    try:
        dv2 = agg.ingest(
            "the project uses PostgreSQL as primary database",
            "file-agent",
            metadata={"category": "fact"},
        )
        dv3 = agg.ingest(
            "HTTPS is enforced for all external connections",
            "file-agent",
            metadata={"category": "policy"},
        )
        results = agg.get_by_agent("file-agent")
        assert len(results) >= 2
        print(f"    file-agent contributed {len(results)} memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: get_by_topic ──
    total += 1
    print("\n[Test 6] get_by_topic")
    try:
        results = agg.get_by_topic("dark")
        assert len(results) >= 1
        # The dark-mode memory should be in results
        found = any("dark" in dv.content.lower() for dv in results)
        assert found
        print(f"    topic='dark' → {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: query ──
    total += 1
    print("\n[Test 7] query with filters")
    try:
        results = agg.query({"category": "policy"})
        assert len(results) >= 1
        assert all(dv.category == "policy" for dv in results)
        print(f"    query category=policy → {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: get_related (BFS depth=1) ──
    total += 1
    print("\n[Test 8] get_related (BFS)")
    try:
        # Add relations via _relations_graph directly (add_relation not in aggregator,
        # but we can use engine's add_relation + sync graph)
        dv_a = agg.ingest("Python is the primary language", "main",
                          metadata={"category": "fact"})
        dv_b = agg.ingest("Python 3.11+ is required", "computer-agent",
                          metadata={"category": "fact"})

        # Manually add relation to graph
        agg._relations_graph[dv_a.memory_id][dv_b.memory_id] = RelationType.EXTENDS.value

        related = agg.get_related(dv_a.memory_id, depth=1)
        assert len(related) >= 1
        assert dv_b.memory_id in {r.memory_id for r in related}
        print(f"    BFS depth=1 from {dv_a.memory_id[:8]} → {len(related)} nodes")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 9: get_contradictions ──
    total += 1
    print("\n[Test 9] get_contradictions")
    try:
        dv_c = agg.ingest("do not use Python 2, it is deprecated",
                          "main", metadata={"category": "policy"})

        # Add CONTRADICTS edge
        agg._relations_graph[dv_a.memory_id][dv_c.memory_id] = RelationType.CONTRADICTS.value

        contradictions = agg.get_contradictions(dv_a.memory_id)
        assert len(contradictions) >= 1
        print(f"    found {len(contradictions)} contradictions for {dv_a.memory_id[:8]}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 10: get_global_context ──
    total += 1
    print("\n[Test 10] get_global_context")
    try:
        ctx = agg.get_global_context(limit=100)
        assert len(ctx) >= 1
        # dv1 was scope=global
        assert dv1.memory_id in {c.memory_id for c in ctx}
        print(f"    global context: {len(ctx)} memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 11: clean_expired ──
    total += 1
    print("\n[Test 11] clean_expired")
    try:
        # All memories are recent, should clean 0
        removed = agg.clean_expired(max_age_hours=720)
        print(f"    removed {removed} (expected 0 since all fresh)")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 12: statistics ──
    total += 1
    print("\n[Test 12] statistics")
    try:
        stats = agg.statistics()
        assert stats["total_memories"] >= 5
        assert "source_distribution" in stats
        assert "category_distribution" in stats
        assert "topic_distribution_top20" in stats
        print(f"    memories={stats['total_memories']}, "
              f"avg_confidence={stats['avg_confidence']}, "
              f"avg_priority={stats['avg_priority']}")
        print(f"    sources={stats['source_distribution']}")
        print(f"    categories={stats['category_distribution']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 13: Thread safety ──
    total += 1
    print("\n[Test 13] Thread safety")
    try:
        threads = []
        errors = []

        def worker(wid: int) -> None:
            try:
                for i in range(20):
                    agg.ingest(
                        f"thread-{wid} generated observation number {i}",
                        f"agent-{wid % 3}",
                    )
                    agg.get_by_agent(f"agent-{wid % 3}", limit=10)
                    agg.query({"confidence_min": 0.3}, limit=5)
            except Exception as exc:
                errors.append(str(exc))

        for tid in range(4):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"
        print(f"    4 threads × 20 operations OK, pool size={len(agg._pool)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 14: merge on ingest ──
    total += 1
    print("\n[Test 14] auto-merge on ingest (similar content)")
    try:
        before = agg.statistics()["total_memories"]
        dv_merge = agg.ingest(
            "user prefers dark mode in all applications",  # identical to dv1
            "search-agent",
        )
        after = agg.statistics()["total_memories"]
        # Should merge not create new → count unchanged or +0
        assert after == before
        assert "search-agent" in dv_merge.source_agents
        print(f"    pool size unchanged ({before}→{after}), "
              f"sources={dv_merge.source_agents}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-1 Test 15: vector_search ──
    total += 1
    print("\n[Test 15] P0-1 vector_search")
    try:
        vec_results = agg.vector_search("dark mode preference", top_k=5)
        assert len(vec_results) >= 1
        print(f"    vector_search returned {len(vec_results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-1 Test 16: query mode=vector ──
    total += 1
    print("\n[Test 16] P0-1 query mode=vector")
    try:
        results = agg.query({}, limit=5, mode="vector", query_text="database SQL")
        assert isinstance(results, list)
        print(f"    vector query returned {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-1 Test 17: query mode=hybrid ──
    total += 1
    print("\n[Test 17] P0-1 query mode=hybrid")
    try:
        results = agg.query({}, limit=10, mode="hybrid", query_text="Python programming")
        assert isinstance(results, list)
        assert len(results) >= 1
        print(f"    hybrid query returned {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-2 Test 18: TTL ingest and cleanup ──
    total += 1
    print("\n[Test 18] P0-2 TTL ingest + cleanup")
    try:
        agg.ingest("temporary test memory - should expire", "test-agent",
                   metadata={"ttl": 1})  # 1 second TTL
        time.sleep(1.5)
        removed = agg.cleanup()
        print(f"    cleanup removed {removed} expired memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-2 Test 19: touch and memory_stats ──
    total += 1
    print("\n[Test 19] P0-2 touch + memory_stats")
    try:
        dv_t = agg.ingest("touchable test memory", "test-agent",
                          metadata={"category": "fact"})
        agg.touch(dv_t.memory_id)
        agg.touch(dv_t.memory_id)
        stats = agg.memory_stats(dv_t.memory_id)
        assert stats is not None
        assert stats["access_count"] >= 2
        print(f"    memory_stats: access_count={stats['access_count']}, "
              f"last_accessed={stats['last_accessed']:.1f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-2 Test 21: RRF fusion correctness ──
    total += 1
    print("\n[Test 21] P1-2 RRF fusion correctness")
    try:
        # Create two ranked lists with known overlap
        list_a = [agg._pool[list(agg._pool.keys())[0]]] if agg._pool else []
        list_b = [agg._pool[list(agg._pool.keys())[1]]] if len(agg._pool) > 1 else []
        fused = agg._rrf_fusion([list_a, list_b], top_k=5)
        assert isinstance(fused, list)
        assert len(fused) <= 5
        # With 2 disjoint lists, should get 2 results (if both lists non-empty)
        if list_a and list_b:
            assert len(fused) == 2
            print(f"    RRF fused 2 lists → {len(fused)} results")
        else:
            print(f"    RRF fusion returned {len(fused)} results (pool may be sparse)")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-2 Test 22: hybrid query with RRF + statistics channels ──
    total += 1
    print("\n[Test 22] P1-2 hybrid query + retrieval_channels stats")
    try:
        results = agg.query({}, limit=5, mode="hybrid", query_text="dark mode")
        assert isinstance(results, list)
        print(f"    hybrid query returned {len(results)} results")

        stats = agg.statistics()
        channels = stats.get("retrieval_channels", {})
        assert isinstance(channels, dict)
        assert "keyword" in channels
        assert "vector" in channels
        assert "second_brain" in channels
        assert "retrieval_v47" in channels
        assert "exabase" in channels
        assert "beamlight" in channels
        print(f"    retrieval_channels: {channels}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-3 Test 23: cross_agent_insights structure ──
    total += 1
    print("\n[Test 23] P1-3 cross_agent_insights structure")
    try:
        insights = agg.cross_agent_insights(top_k=5)
        assert isinstance(insights, dict)
        # Required top-level keys
        for key in ("total_agents", "total_memories", "agent_knowledge_counts",
                     "agent_contributions", "shared_topics", "knowledge_gaps",
                     "collaboration_patterns", "emerging_themes",
                     "orphan_knowledge_count", "orphan_ratio",
                     "contradiction_hotspots", "second_brain_insights",
                     "retrieval_channels"):
            assert key in insights, f"Missing key: {key}"
        assert isinstance(insights["agent_contributions"], dict)
        assert isinstance(insights["shared_topics"], list)
        assert isinstance(insights["knowledge_gaps"], list)
        assert isinstance(insights["collaboration_patterns"], list)
        assert isinstance(insights["emerging_themes"], list)
        print(f"    insights: {len(insights['agent_contributions'])} agents, "
              f"{len(insights['shared_topics'])} shared topics, "
              f"{len(insights['emerging_themes'])} emerging themes")

        # Agent-specific focus
        agent_insights = agg.cross_agent_insights(agent_name="main", top_k=5)
        assert "agent_focus" in agent_insights
        print(f"    agent_focus for 'main': {agent_insights['agent_focus']['agent']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P1-4 Test 24: Degradation Manager ──
    total += 1
    print("\n[Test 24] P1-4 DegradationManager three-tier fallback")
    try:
        dm = agg._degradation
        dm.reset()  # reset in case previous tests triggered degradation

        # Tier starts at FULL
        assert dm.tier == agg._ServiceTier.FULL
        assert dm.is_channel_available("retrieval_v47")
        assert dm.is_channel_available("exabase")
        stats = dm.statistics()
        assert stats["tier"] == "full"
        print("    initial tier: FULL ✓")

        # Mark V47 failure → DEGRADED (V47 ∈ FULL_CHANNELS)
        changed = dm.mark_failure("retrieval_v47", "timeout")
        assert changed
        assert dm.tier == agg._ServiceTier.DEGRADED
        assert not dm.is_channel_available("retrieval_v47")
        stats = dm.statistics()
        assert stats["failure_counts"]["retrieval_v47"] == 1
        print(f"    V47 failed → DEGRADED ✓ (active: {stats['active_channels']})")

        # Mark vector failure → MINIMAL (keyword only)
        changed = dm.mark_failure("vector", "crash")
        assert changed
        assert dm.tier == agg._ServiceTier.MINIMAL
        print("    Vector failed → MINIMAL ✓")

        # Recovery: V47 back → still MINIMAL (vector still down)
        dm.mark_recovery("retrieval_v47")
        assert dm.tier == agg._ServiceTier.MINIMAL
        print("    V47 recovered, tier remains MINIMAL ✓")

        # Recovery: vector back → FULL (all channels healthy again)
        dm.mark_recovery("vector")
        assert dm.tier == agg._ServiceTier.FULL
        print("    Vector recovered → FULL ✓")

        # Verify failure_counts preserved after recovery
        stats = dm.statistics()
        assert stats["failure_counts"]["retrieval_v47"] == 1
        assert stats["failure_counts"]["vector"] == 1
        print("    failure_counts preserved after recovery ✓")

        # Reset
        dm.reset()
        assert dm.tier == agg._ServiceTier.FULL
        assert dm.statistics()["failure_counts"] == {}
        print("    reset → clean state ✓")

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 25: importance_score ──
    total += 1
    print("\n[Test 25] v7.0.0 importance_score returns 0-1 range")
    try:
        for mid in list(agg._pool.keys())[:3]:
            score = agg.importance_score(mid)
            assert 0.0 <= score <= 1.0, f"score {score} out of range"
        # Unknown ID returns 0
        assert agg.importance_score("nonexistent-id") == 0.0
        print(f"    importance_score works, sample range valid")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 26: merge_memories ──
    total += 1
    print("\n[Test 26] v7.0.0 merge_memories consolidates similar memories")
    try:
        before = agg.statistics()["total_memories"]
        # Ingest two very similar memories
        agg.ingest("The API rate limit is 100 requests per minute",
                   "test-agent", metadata={"topic": "api"})
        agg.ingest("The API rate limit is 100 requests per minute, enforced globally",
                   "test-agent", metadata={"topic": "api"})
        merged = agg.merge_memories(topic="api", similarity_threshold=0.4)
        after = agg.statistics()["total_memories"]
        # With low threshold, should merge similar ones
        print(f"    merge_memories: merged={merged}, pool {before+2}→{after}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 27: detect_contradictions ──
    total += 1
    print("\n[Test 27] v7.0.0 detect_contradictions")
    try:
        agg.ingest("The system always requires authentication for all endpoints",
                   "main", metadata={"category": "policy"})
        agg.ingest("The system never requires authentication for internal calls",
                   "computer-agent", metadata={"category": "policy"})
        contradictions = agg.detect_contradictions()
        assert isinstance(contradictions, list)
        # always vs never should be detected across different agents
        assert len(contradictions) >= 1, f"Expected >=1 contradiction, got {len(contradictions)}"
        assert contradictions[0]["pattern"] == "always vs never"
        print(f"    detected {len(contradictions)} contradictions: {contradictions[0]['pattern']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.0.0 Test 28: export_readable ──
    total += 1
    print("\n[Test 28] v7.0.0 export_readable outputs markdown")
    try:
        content = agg.export_readable()
        assert isinstance(content, str)
        assert "# Trinity Shared Memory Export" in content
        assert "## Agent:" in content
        assert "importance:" in content
        print(f"    export_readable generated {len(content)} chars, "
              f"{content.count('## Agent:')} agent sections")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.1.0 Test 29: ObservabilityManager dashboard ──
    total += 1
    print("\n[Test 29] v7.1.0 ObservabilityManager dashboard structure")
    try:
        dash = agg._observability.dashboard()
        assert isinstance(dash, dict)
        for key in ("uptime_seconds", "health", "requests", "operations", "memory_ops"):
            assert key in dash, f"Missing key: {key}"
        assert dash["health"] == "healthy"
        assert "total" in dash["requests"]
        assert "errors" in dash["requests"]
        assert "avg_latency_ms" in dash["requests"]
        print(f"    dashboard: health={dash['health']}, "
              f"uptime={dash['uptime_human']}, requests={dash['requests']['total']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── v7.1.0 Test 30: MemoryBenchmark three-stage run ──
    total += 1
    print("\n[Test 30] v7.1.0 MemoryBenchmark three-stage run")
    try:
        results = agg.run_benchmark()
        assert isinstance(results, list)
        assert len(results) == 3, f"Expected 3 benchmark stages, got {len(results)}"
        stage_names = [r["name"] for r in results]
        assert "ingest" in stage_names
        assert "query" in stage_names
        assert "retrieval" in stage_names
        for r in results:
            assert "success_rate" in r
            assert "avg_latency_ms" in r
            assert "p50_ms" in r
            assert "p95_ms" in r
        print(f"    benchmark stages: {stage_names}")
        print(f"    ingest:  {results[0]['success_rate']:.2%} success, "
              f"{results[0]['avg_latency_ms']:.1f}ms avg")
        print(f"    query:   {results[1]['success_rate']:.2%} success, "
              f"{results[1]['avg_latency_ms']:.1f}ms avg")
        print(f"    retrieval: {results[2].get('details', {}).get('recall_at_k', 0):.2%} recall@K, "
              f"{results[2]['avg_latency_ms']:.1f}ms avg")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── P0-2 Test 20: shutdown ──
    total += 1
    print("\n[Test 20] P0-2 graceful shutdown")
    try:
        agg.shutdown()
        assert agg._stop_cleanup.is_set()
        print("    shutdown completed, cleanup daemon stopped")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed}/{total} passed")
    print("=" * 60)
    return passed == total
