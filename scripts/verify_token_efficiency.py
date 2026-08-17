"""
Token Efficiency Verification Script
=====================================
Validates the TokenEfficiencyOptimizer by comparing query processing
with and without optimization across 5 queries of varying complexity.

Produces a comparison report showing token savings, dedup counts,
and early-stop effectiveness.

Usage:
    python scripts/verify_token_efficiency.py
"""

import json
import os
import sys
import time
from datetime import datetime

# Inject project root
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# ── Test Data: 5 queries of varying complexity ──────────────────────

TEST_QUERIES = [
    {
        "id": "Q1",
        "query": "What is my name?",
        "complexity": "simple",
        "description": "Simple factual lookup",
    },
    {
        "id": "Q2",
        "query": "What are the project deadlines for Q3?",
        "complexity": "simple",
        "description": "Simple deadline query",
    },
    {
        "id": "Q3",
        "query": "Explain the PostgreSQL migration and how it affects performance.",
        "complexity": "medium",
        "description": "Medium explain query",
    },
    {
        "id": "Q4",
        "query": "Compare the FAISS HNSW and Annoy indexing approaches, analyze their tradeoffs in terms of recall, latency, and memory usage for large-scale retrieval.",
        "complexity": "complex",
        "description": "Complex comparative analysis",
    },
    {
        "id": "Q5",
        "query": "Analyze the Trinity memory system architecture: how do CRAG, the 50-layer guardian chain, and the second-brain evolution pipeline interact? Evaluate the overall design for reliability and scalability.",
        "complexity": "complex",
        "description": "Complex architectural analysis",
    },
]

# ── Simulated Channel Results ────────────────────────────────────────

def generate_mock_results(
    query_id: str,
    num_channels: int = 8,
    results_per_channel: int = 8,
) -> list:
    """
    Generate mock channel results simulating a 47-channel cascade.

    Each channel returns 5-10 results with varying relevance.
    Later channels tend to produce more duplicates and lower relevance.
    """
    import random

    # Seed based on query for reproducibility
    rng = random.Random(hash(query_id) & 0x7FFFFFFF)

    # Base content templates
    templates = [
        "The user profile indicates preferences for {topic}.",
        "Memory record about {topic}: key findings include {detail}.",
        "Session log entry: discussed {topic} with focus on {detail}.",
        "Configuration note: {topic} settings are configured for {detail}.",
        "Meeting notes from {date}: discussed {topic} — {detail}.",
        "Project documentation: {topic} module implements {detail}.",
        "Code review comment: {topic} requires optimization for {detail}.",
        "Architecture decision record: {topic} uses {detail}.",
        "Benchmark results for {topic}: achieved {detail} performance.",
        "Security audit note: {topic} passes compliance checks for {detail}.",
    ]

    topics = [
        "machine learning pipeline", "vector indexing", "FAISS HNSW",
        "PostgreSQL migration", "Trinity architecture", "second-brain engine",
        "CRAG module", "guardian chain", "token optimization",
        "distributed retrieval", "semantic search", "knowledge graph",
    ]

    details = [
        "high recall rates", "low latency profiles", "memory efficiency",
        "production readiness", "scalable design", "improved accuracy",
        "reduced token overhead", "streaming support", "batch processing",
        "real-time updates", "cache coherence", "fault tolerance",
    ]

    all_channels = []
    for ch_idx in range(num_channels):
        # Later channels: fewer results, more duplicates
        n_results = max(3, results_per_channel - ch_idx // 2)
        if ch_idx > num_channels // 2:
            n_results = max(2, n_results - 2)

        channel_results = []
        for i in range(n_results):
            topic = rng.choice(topics)
            detail = rng.choice(details)
            content = rng.choice(templates).format(
                topic=topic,
                detail=detail,
                date=f"2026-0{rng.randint(7,8):d}-{rng.randint(1,28):02d}",
            )

            # Score decays for later channels; first few channels have high scores
            base_score = 0.95 - ch_idx * 0.08
            jitter = rng.uniform(-0.05, 0.05)
            score = max(0.05, min(0.99, base_score + jitter))

            channel_results.append({
                "memory_id": f"mem_{query_id}_{ch_idx}_{i}",
                "content": content,
                "content_preview": content[:100],
                "score": round(score, 4),
                "channel": f"channel_{ch_idx}",
                "importance": rng.uniform(0.3, 0.9),
            })

        all_channels.append({
            "channel_name": f"channel_{ch_idx:02d}",
            "results": channel_results,
        })

    return all_channels


# ── Run Comparison ───────────────────────────────────────────────────

def run_baseline(channels: list) -> dict:
    """Run without optimization (all channels, no dedup, no truncation)."""
    all_results = []
    total_tokens = 0
    channels_processed = 0

    # Simple token estimator
    def _estimate(text: str) -> int:
        en_words = len(text.split()) if text else 0
        zh_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff') if text else 0
        en_only = max(0, en_words - zh_chars)
        return int(en_only * 1.3 + zh_chars * 2.5) + 20

    for ch in channels:
        channels_processed += 1
        for r in ch["results"]:
            all_results.append(r)
            total_tokens += _estimate(str(r.get("content", "")))

    # Sort by score
    all_results.sort(key=lambda r: r.get("score", 0), reverse=True)

    return {
        "total_results": len(all_results),
        "unique_results": len({r.get("content", "")[:100] for r in all_results}),
        "channels_processed": channels_processed,
        "estimated_tokens": total_tokens,
        "top_5_scores": [r["score"] for r in all_results[:5]],
    }


def run_optimized(channels: list, query: str = "") -> dict:
    """Run with TokenEfficiencyOptimizer."""
    from trinity.core.token_efficiency import TokenEfficiencyOptimizer

    opt = TokenEfficiencyOptimizer(
        early_stop_patience=3,
        early_stop_min_gain=0.03,
        enable_dedup=True,
        enable_dynamic_truncation=True,
        simple_query_top_k=3,
        medium_query_top_k=5,
        complex_query_top_k=10,
        token_budget_per_query=8192,
        enabled=True,
    )

    # Classify query complexity
    complexity = opt.classify_complexity(query or "mock query")
    opt.start_query(query or "mock query")

    all_results = []
    channels_processed = 0
    stopped_early = False

    for ch in channels:
        results = ch["results"]
        channels_processed += 1

        # Dedup (global fingerprint set, handles cross-channel dedup)
        filtered = opt.deduplicate(results)
        all_results.extend(filtered)

        # Track
        est = opt.estimate_result_tokens(filtered)
        opt.track_tokens(ch["channel_name"], len(results), est)

        # Check early stop
        if opt.should_early_stop(filtered):
            stopped_early = True
            break

    # Dynamic truncation (dedup already handled per-channel above)
    final = opt.dynamic_truncate(all_results)

    stats = opt.statistics()

    return {
        "total_results": len(final),
        "unique_results": len(final),
        "channels_processed": channels_processed,
        "channels_total": len(channels),
        "estimated_tokens": stats.get("current_budget", {}).get("used", 0),
        "dedup_removed": stats["total_dedup_removed"],
        "early_stop": stopped_early,
        "top_5_scores": [r["score"] for r in final[:5]],
        "complexity": complexity,
    }


def main():
    print("=" * 72)
    print("  Trinity Token Efficiency Verification")
    print(f"  Run time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 72)

    results_summary = []

    for tq in TEST_QUERIES:
        print(f"\n{'─' * 60}")
        print(f"  {tq['id']}: {tq['description']}")
        print(f"  Query: \"{tq['query'][:80]}{'...' if len(tq['query']) > 80 else ''}\"")
        print(f"  Expected complexity: {tq['complexity']}")

        channels = generate_mock_results(tq["id"], num_channels=8)

        # Baseline
        t0 = time.perf_counter()
        baseline = run_baseline(channels)
        t_baseline = time.perf_counter() - t0

        # Optimized
        t0 = time.perf_counter()
        optimized = run_optimized(channels, tq["query"])
        t_optimized = time.perf_counter() - t0

        # Comparison
        token_savings = baseline["estimated_tokens"] - optimized["estimated_tokens"]
        savings_pct = (
            (token_savings / baseline["estimated_tokens"] * 100)
            if baseline["estimated_tokens"] > 0
            else 0
        )
        channel_savings = baseline["channels_processed"] - optimized["channels_processed"]

        print(f"  Baseline:     {baseline['total_results']:>4d} results, "
              f"{baseline['channels_processed']:>2d} channels, "
              f"~{baseline['estimated_tokens']:>5d} tokens")
        print(f"  Optimized:    {optimized['total_results']:>4d} results, "
              f"{optimized['channels_processed']:>2d} channels, "
              f"~{optimized['estimated_tokens']:>5d} tokens"
              f"{' [EARLY STOP]' if optimized['early_stop'] else ''}")
        print(f"  Savings:      {token_savings:>+5d} tokens ({savings_pct:+.1f}%), "
              f"dedup: {optimized['dedup_removed']}, "
              f"complexity classified as: {optimized.get('complexity', 'N/A')}")

        results_summary.append({
            "id": tq["id"],
            "description": tq["description"],
            "complexity": tq["complexity"],
            "classified_as": optimized.get("complexity", "N/A"),
            "baseline_results": baseline["total_results"],
            "optimized_results": optimized["total_results"],
            "baseline_channels": baseline["channels_processed"],
            "optimized_channels": optimized["channels_processed"],
            "baseline_tokens": baseline["estimated_tokens"],
            "optimized_tokens": optimized["estimated_tokens"],
            "token_savings": token_savings,
            "savings_pct": round(savings_pct, 1),
            "dedup_removed": optimized["dedup_removed"],
            "early_stop": optimized["early_stop"],
            "time_baseline_ms": round(t_baseline * 1000, 2),
            "time_optimized_ms": round(t_optimized * 1000, 2),
        })

    # ── Aggregate Summary ────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  AGGREGATE SUMMARY")
    print(f"{'═' * 72}")

    total_baseline_tokens = sum(r["baseline_tokens"] for r in results_summary)
    total_optimized_tokens = sum(r["optimized_tokens"] for r in results_summary)
    total_savings = total_baseline_tokens - total_optimized_tokens
    total_pct = (
        (total_savings / total_baseline_tokens * 100)
        if total_baseline_tokens > 0
        else 0
    )
    total_dedup = sum(r["dedup_removed"] for r in results_summary)
    early_stops = sum(1 for r in results_summary if r["early_stop"])

    print(f"  Queries tested:          {len(results_summary)}")
    print(f"  Total baseline tokens:   {total_baseline_tokens}")
    print(f"  Total optimized tokens:  {total_optimized_tokens}")
    print(f"  Total token savings:     {total_savings} ({total_pct:.1f}%)")
    print(f"  Total dedup removed:     {total_dedup}")
    print(f"  Early stops triggered:   {early_stops}/{len(results_summary)}")

    # ── Detailed table ───────────────────────────────────────────────
    print(f"\n{'─' * 72}")
    print(f"  {'ID':<4} {'Complexity':<12} {'Baseline':>8} {'Optimized':>10} "
          f"{'Savings':>8} {'%':>6} {'Dedup':>6} {'EarlyStop':>10}")
    print(f"  {'─' * 68}")
    for r in results_summary:
        es = "YES" if r["early_stop"] else "no"
        print(f"  {r['id']:<4} {r['complexity']:<12} "
              f"{r['baseline_tokens']:>8d} {r['optimized_tokens']:>10d} "
              f"{r['token_savings']:>+8d} {r['savings_pct']:>5.1f}% "
              f"{r['dedup_removed']:>6d} {es:>10}")

    # ── Write JSON Report ────────────────────────────────────────────
    output_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "output"
    )
    os.makedirs(output_dir, exist_ok=True)

    report_path = os.path.join(output_dir, "token_efficiency_report.json")
    report = {
        "title": "Trinity Token Efficiency Verification Report",
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "queries_tested": len(results_summary),
            "total_baseline_tokens": total_baseline_tokens,
            "total_optimized_tokens": total_optimized_tokens,
            "total_token_savings": total_savings,
            "savings_percentage": round(total_pct, 1),
            "total_dedup_removed": total_dedup,
            "early_stops_triggered": early_stops,
        },
        "per_query": results_summary,
    }

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n  Report written to: {report_path}")

    # ── Module Import Sanity Check ───────────────────────────────────
    print(f"\n{'═' * 72}")
    print("  IMPORT & CLASS SANITY CHECK")
    print(f"{'═' * 72}")

    checks_passed = 0
    checks_total = 0

    # Check 1: token_efficiency module imports
    checks_total += 1
    try:
        from trinity.core.token_efficiency import (
            TokenEfficiencyOptimizer,
            create_crag_efficiency_hook,
            create_search_hook,
        )
        print("  [PASS] token_efficiency module import")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] token_efficiency module import: {e}")

    # Check 2: CRAG integration
    checks_total += 1
    try:
        from trinity.core.crag import CorrectiveRAG
        crag = CorrectiveRAG(
            enable_token_efficiency=True,
            early_stop_patience=3,
            token_budget_per_query=4096,
        )
        assert crag.token_optimizer is not None, "token_optimizer should be initialized"
        print("  [PASS] CRAG TokenEfficiency integration")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] CRAG TokenEfficiency integration: {e}")

    # Check 3: CRAG disabled mode
    checks_total += 1
    try:
        crag_disabled = CorrectiveRAG(enable_token_efficiency=False)
        assert crag_disabled.token_optimizer is None, "token_optimizer should be None when disabled"
        results, should_stop = crag_disabled.apply_token_efficiency(
            [{"content": "test", "score": 0.9}], "ch_00"
        )
        assert not should_stop, "should_stop should be False when disabled"
        print("  [PASS] CRAG disabled mode")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] CRAG disabled mode: {e}")

    # Check 4: Query complexity classifier
    checks_total += 1
    try:
        opt = TokenEfficiencyOptimizer()
        assert opt.classify_complexity("what is AI") == "simple"
        assert opt.classify_complexity("explain how transformers work") == "medium"
        assert opt.classify_complexity(
            "compare and contrast BERT and GPT architectures in detail"
        ) == "complex"
        print("  [PASS] Query complexity classification")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] Query complexity classification: {e}")

    # Check 5: Early stop logic
    checks_total += 1
    try:
        opt = TokenEfficiencyOptimizer(early_stop_patience=2, enabled=True)
        opt.start_query("test")
        # Channel 1: good results
        results1 = [{"score": 0.8}, {"score": 0.7}]
        assert not opt.should_early_stop(results1)
        # Channel 2: no gain
        results2 = [{"score": 0.6}, {"score": 0.5}]
        assert not opt.should_early_stop(results2)
        # Channel 3: still no gain → should trigger
        results3 = [{"score": 0.6}, {"score": 0.5}]
        assert opt.should_early_stop(results3)
        print("  [PASS] Early stop logic")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] Early stop logic: {e}")

    # Check 6: Dedup logic
    checks_total += 1
    try:
        opt = TokenEfficiencyOptimizer(enable_dedup=True, enabled=True)
        opt.start_query("test")
        r1 = [{"content": "The system uses FAISS for indexing.", "score": 0.9}]
        r2 = [{"content": "The system uses FAISS for indexing.", "score": 0.8}]
        r3 = [{"content": "PostgreSQL handles relational data.", "score": 0.7}]

        d1 = opt.deduplicate(r1)
        d2 = opt.deduplicate(r2)
        d3 = opt.deduplicate(r3)

        assert len(d1) == 1, f"expected 1, got {len(d1)}"
        assert len(d2) == 0, f"expected 0 (duplicate), got {len(d2)}"
        assert len(d3) == 1, f"expected 1, got {len(d3)}"
        print("  [PASS] Dedup logic")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] Dedup logic: {e}")

    # Check 7: Dynamic truncation
    checks_total += 1
    try:
        opt = TokenEfficiencyOptimizer(
            enable_dynamic_truncation=True,
            simple_query_top_k=3,
            medium_query_top_k=5,
            complex_query_top_k=10,
            enabled=True,
        )

        results = [{"content": f"result_{i}", "score": 1.0 - i * 0.1} for i in range(15)]

        simple_trunc = opt.dynamic_truncate(results, "what is python")
        assert len(simple_trunc) == 3, f"expected 3, got {len(simple_trunc)}"

        complex_trunc = opt.dynamic_truncate(
            results, "compare and analyze all approaches in detail"
        )
        assert len(complex_trunc) == 10, f"expected 10, got {len(complex_trunc)}"

        print("  [PASS] Dynamic truncation")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] Dynamic truncation: {e}")

    # Check 8: Hook factory
    checks_total += 1
    try:
        opt = TokenEfficiencyOptimizer(enabled=True, enable_dedup=True)
        hook = create_crag_efficiency_hook(opt)
        opt.start_query("test")

        results1 = [{"content": "FAISS HNSW indexing.", "score": 0.9}]
        filtered1, stop1 = hook(results1)
        assert not stop1
        assert len(filtered1) == 1

        results2 = [{"content": "FAISS HNSW indexing.", "score": 0.85}]
        filtered2, stop2 = hook(results2)
        assert len(filtered2) == 0  # duplicate

        print("  [PASS] Hook factory")
        checks_passed += 1
    except Exception as e:
        print(f"  [FAIL] Hook factory: {e}")

    # ── Final verdict ────────────────────────────────────────────────
    print(f"\n{'═' * 72}")
    print(f"  VERDICT: {checks_passed}/{checks_total} checks passed")
    if checks_passed == checks_total:
        print("  ALL CHECKS PASSED")
    else:
        print(f"  {checks_total - checks_passed} CHECKS FAILED")
    print(f"{'═' * 72}")

    return 0 if checks_passed == checks_total else 1


if __name__ == "__main__":
    sys.exit(main())
