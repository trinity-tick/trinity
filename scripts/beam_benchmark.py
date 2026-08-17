"""
BEAM Scale Benchmark for Trinity
=================================
Runs 50 queries against 1K/10K/100K scale PostgreSQL data, measuring:
- P50 / P95 / P99 latency per query (ms)
- Queries Per Second (QPS)
- Recall@5

Usage:
    python beam_benchmark.py --scale 1K
    python beam_benchmark.py --scale all
    python beam_benchmark.py --scale 1K --queries 30

Output:
    benchmark/beam_results.csv
    benchmark/beam_report.md

Dependencies: psycopg2, numpy
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import numpy as np

# ── Project paths ────────────────────────────────────────────────────
BENCHMARK_DIR = r"C:\Users\Administrator\trinity\benchmark"
os.makedirs(BENCHMARK_DIR, exist_ok=True)

# ── PostgreSQL config ─────────────────────────────────────────────────
PG_CONFIG = {
    "host": os.environ.get("PGHOST", "127.0.0.1"),
    "port": int(os.environ.get("PGPORT", "5432")),
    "dbname": os.environ.get("PGDATABASE", os.environ.get("PGDBNAME", "trinity")),
    "user": os.environ.get("PGUSER", "postgres"),
    "password": os.environ.get("PGPASSWORD", ""),
}

# ── 10 topics × 5 query variants = 50 queries ─────────────────────────
# Each query is designed to match memories tagged with [topic:T#]
QUERY_SET = [
    # T0: paper_review — "paper review", "SOTA", "outperforms", "architecture", "code available"
    ("T0", "paper review SOTA outperforms architecture training"),
    ("T0", "paper review SOTA model comparison neural technique"),
    ("T0", "outperforms SOTA percent dataset architecture layers"),
    ("T0", "quantization paper review outperforms SOTA model technique"),
    ("T0", "paper review architecture layers training parameters outperforms"),

    # T1: wms_system — "WMS", "warehouse", "picking", "throughput", "order-router"
    ("T1", "WMS module warehouse picking order-router throughput"),
    ("T1", "warehouse optimization picking time ROI beam search"),
    ("T1", "WMS dock-scheduler multi-warehouse allocation throughput"),
    ("T1", "inventory SKU warehouse accuracy WMS module throughput"),
    ("T1", "warehouse picking batch genetic algorithm orders throughput"),

    # T2: database_optimization — "query optimization", "execution", "PostgreSQL", "index"
    ("T2", "query optimization rewritten execution time index database"),
    ("T2", "PostgreSQL performance tuning query latency index type"),
    ("T2", "database migration data transfer execution time query"),
    ("T2", "query optimization case study execution time method index"),
    ("T2", "full-text search query execution time reduction database"),

    # T3: agent_handoff — "handoff", "cross-agent", "routing", "capability", "browser"
    ("T3", "handoff cross-agent task routing capability match score"),
    ("T3", "agent handoff from browser to app context preserved"),
    ("T3", "cross agent routing capability match browser computer agent"),
    ("T3", "handoff agent task transfer context capability score"),
    ("T3", "agent collaboration completed steps pending handoff routing"),

    # T4: memory_consolidation — "forgetting", "retention", "consolidation", "session", "merge"
    ("T4", "forgetting curve retention analysis days review interval"),
    ("T4", "memory consolidation cycle processed merged archived duration"),
    ("T4", "session boundary summarizing turns consolidated memories themes"),
    ("T4", "memory merge deduplication consolidation forgetting retention"),
    ("T4", "consolidation cycle merged archived forgetting curve analysis"),

    # T5: model_serving — "LLM inference", "tokens per second", "model serving", "throughput"
    ("T5", "LLM inference optimization throughput tokens per second cost"),
    ("T5", "model serving benchmark tokens per second latency batch"),
    ("T5", "embedding model comparison winner scores task LLM"),
    ("T5", "inference optimization TensorRT SGLang throughput tokens"),
    ("T5", "LLM serving cost per million tokens inference optimization"),

    # T6: personal_preferences — "configuration preference", "dark mode", "confirmed", "autosave"
    ("T6", "configuration preference autosave dark mode user confirmed"),
    ("T6", "user preference configuration setting confirmed workflow"),
    ("T6", "prefers dark mode working configuration confirmed preference"),
    ("T6", "configuration setting preference user confirmed performance"),
    ("T6", "workflow preference user confirmed configuration dark mode"),

    # T7: evolution_tracking — "self-improvement", "evolution state", "pattern library", "phase"
    ("T7", "self-improvement log strategy before after metric improvement"),
    ("T7", "evolution state phase active strategies pattern library"),
    ("T7", "evolution pattern library size phase active strategies"),
    ("T7", "self improvement evolution certified delta strategy boosting"),
    ("T7", "evolution observed analyzed planned executed certified phase"),

    # T8: skill_definitions — "skill registry", "activation cost", "dependencies", "tokens"
    ("T8", "skill registry entry activation cost tokens dependencies version"),
    ("T8", "skill definition triggers tools success rate description"),
    ("T8", "skill evaluation test cases accuracy latency status registry"),
    ("T8", "activation cost estimate tokens skill registry dependencies"),
    ("T8", "skill registry version dependencies activation cost estimate"),

    # T9: safety_audit — "guardian chain", "audit log", "security", "incident", "risk"
    ("T9", "guardian chain check evaluated verdict confidence audit log"),
    ("T9", "security audit incident report type severity detected resolved"),
    ("T9", "audit log risk action guardian chain check safety"),
    ("T9", "guardian evaluated confidence risk auditor security incident"),
    ("T9", "safety audit log guardian chain level rule risk check"),
]


def connect_pg():
    """Connect to PostgreSQL with dict cursor."""
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(**PG_CONFIG)
    return conn


def get_total_memories(conn) -> int:
    """Count total memories in database."""
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'")
        return cur.fetchone()[0]


def get_scale_label(conn) -> str:
    """Determine current scale based on test data count."""
    total = get_total_memories(conn)
    if total >= 90000:
        return "100K"
    elif total >= 9000:
        return "10K"
    elif total >= 900:
        return "1K"
    return f"{total}"


def _build_or_tsquery(query: str) -> str:
    """Convert a space-separated keyword query into tsquery with OR (|) logic."""
    words = [w.strip() for w in query.split() if w.strip()]
    if not words:
        return ""
    return " | ".join(words)


def search_memories(conn, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Execute FTS search on PostgreSQL memories table.
    Uses to_tsquery with OR (|) logic for broad keyword matching,
    with ts_rank for relevance scoring.
    """
    import psycopg2.extras

    tsquery_expr = _build_or_tsquery(query)
    if not tsquery_expr:
        return []

    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("""
            SELECT memory_id, content, category, importance, tags,
                   ts_rank(to_tsvector('simple', content),
                           to_tsquery('simple', %s)) as score
            FROM memories
            WHERE status = 'active'
              AND to_tsvector('simple', content) @@ to_tsquery('simple', %s)
            ORDER BY score DESC, importance DESC, created_at DESC
            LIMIT %s
        """, (tsquery_expr, tsquery_expr, top_k))
        results = []
        for row in cur.fetchall():
            results.append({
                "memory_id": str(row["memory_id"]),
                "content": row["content"],
                "category": row["category"],
                "importance": float(row["importance"]),
                "tags": row["tags"],
                "score": float(row["score"]) if row["score"] else 0.0,
            })
        return results


def get_ground_truth(conn, topic_id: str) -> List[str]:
    """
    Get all memory IDs belonging to a topic.
    Topic is embedded as [topic:T#] in the content.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT memory_id FROM memories
            WHERE status = 'active' AND content LIKE %s
        """, (f"%[topic:{topic_id}]%",))
        return [str(row[0]) for row in cur.fetchall()]


def compute_recall_at_k(results: List[Dict], ground_truth_ids: List[str], k: int = 5) -> float:
    """
    Compute Recall@K.
    Recall@K = |top-K ∩ ground_truth| / min(K, |ground_truth|)
    """
    if not ground_truth_ids:
        return 1.0  # no ground truth → perfect recall (degenerate)

    top_k_ids = {r["memory_id"] for r in results[:k]}
    gt_set = set(ground_truth_ids)
    intersection = top_k_ids & gt_set
    return len(intersection) / min(k, len(gt_set))


def run_benchmark(conn, queries: List[Tuple[str, str]], warmup: int = 3) -> Dict[str, Any]:
    """
    Run benchmark: execute each query, measure latency and recall.

    Args:
        conn: PostgreSQL connection
        queries: List of (topic_id, query_text)
        warmup: Number of warmup queries (not counted)

    Returns:
        Dict with latency stats, qps, recall stats, per-query details
    """
    # Warmup
    for i in range(min(warmup, len(queries))):
        _, q = queries[i % len(queries)]
        search_memories(conn, q, top_k=5)

    # Pre-fetch ground truth for all topics
    topics = sorted(set(tid for tid, _ in queries))
    ground_truth_cache = {}
    for tid in topics:
        ground_truth_cache[tid] = get_ground_truth(conn, tid)

    latencies = []
    recalls = []
    per_query_details = []
    total_start = time.perf_counter()

    for tid, query_text in queries:
        # Measure single query latency
        t0 = time.perf_counter()
        results = search_memories(conn, query_text, top_k=5)
        t1 = time.perf_counter()

        latency_ms = (t1 - t0) * 1000
        latencies.append(latency_ms)

        # Compute recall@5
        gt = ground_truth_cache[tid]
        recall = compute_recall_at_k(results, gt, k=5)
        recalls.append(recall)

        per_query_details.append({
            "topic": tid,
            "query": query_text,
            "latency_ms": round(latency_ms, 2),
            "result_count": len(results),
            "recall_at_5": round(recall, 4),
            "ground_truth_count": len(gt),
        })

    total_time = time.perf_counter() - total_start
    total_queries = len(queries)
    qps = total_queries / total_time if total_time > 0 else 0

    # Compute percentiles
    lat_arr = np.array(latencies)
    p50 = float(np.percentile(lat_arr, 50))
    p95 = float(np.percentile(lat_arr, 95))
    p99 = float(np.percentile(lat_arr, 99))
    mean_lat = float(np.mean(lat_arr))
    min_lat = float(np.min(lat_arr))
    max_lat = float(np.max(lat_arr))

    mean_recall = float(np.mean(recalls)) if recalls else 0

    return {
        "total_queries": total_queries,
        "total_time_s": round(total_time, 3),
        "qps": round(qps, 2),
        "p50_ms": round(p50, 2),
        "p95_ms": round(p95, 2),
        "p99_ms": round(p99, 2),
        "mean_lat_ms": round(mean_lat, 2),
        "min_lat_ms": round(min_lat, 2),
        "max_lat_ms": round(max_lat, 2),
        "mean_recall_at_5": round(mean_recall, 4),
        "latencies": latencies,
        "recalls": recalls,
        "per_query": per_query_details,
    }


def generate_report(all_results: List[Dict], csv_path: str, md_path: str):
    """Generate CSV and Markdown report from benchmark results."""
    # ── CSV ──────────────────────────────────────────────────────────
    csv_headers = [
        "scale", "memory_count", "queries", "qps",
        "p50_ms", "p95_ms", "p99_ms",
        "mean_lat_ms", "min_lat_ms", "max_lat_ms",
        "mean_recall_at_5",
    ]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(csv_headers)
        for r in all_results:
            writer.writerow([
                r["scale"], r["memory_count"], r["total_queries"], r["qps"],
                r["p50_ms"], r["p95_ms"], r["p99_ms"],
                r["mean_lat_ms"], r["min_lat_ms"], r["max_lat_ms"],
                r["mean_recall_at_5"],
            ])

    # ── Markdown Report ──────────────────────────────────────────────
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# Trinity BEAM Scale Benchmark Report",
        f"",
        f"> Generated: {now_str}  ",
        f"> Environment: PostgreSQL {PG_CONFIG['host']}:{PG_CONFIG['port']}/{PG_CONFIG['dbname']}  ",
        f"> Method: PostgreSQL FTS (`to_tsvector` + `to_tsquery` OR-logic) with `ts_rank` scoring",
        f"",
        f"## Summary",
        f"",
        f"| Scale | Memories | Queries | QPS | P50 (ms) | P95 (ms) | P99 (ms) | Mean Lat (ms) | Recall@5 |",
        f"|-------|----------|---------|-----|----------|----------|----------|---------------|----------|",
    ]

    for r in all_results:
        lines.append(
            f"| {r['scale']} | {r['memory_count']:,} | {r['total_queries']} | "
            f"{r['qps']:.1f} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | "
            f"{r['p99_ms']:.1f} | {r['mean_lat_ms']:.1f} | {r['mean_recall_at_5']:.3f} |"
        )

    lines.extend([
        f"",
        f"## Latency Distribution",
        f"",
        f"| Scale | Min (ms) | Max (ms) | Mean (ms) | P50 (ms) | P95 (ms) | P99 (ms) |",
        f"|-------|----------|----------|-----------|----------|----------|----------|",
    ])
    for r in all_results:
        lines.append(
            f"| {r['scale']} | {r['min_lat_ms']:.1f} | {r['max_lat_ms']:.1f} | "
            f"{r['mean_lat_ms']:.1f} | {r['p50_ms']:.1f} | {r['p95_ms']:.1f} | {r['p99_ms']:.1f} |"
        )

    # Per-scale per-query details
    for r in all_results:
        lines.extend([
            f"",
            f"## {r['scale']} Scale — Per-Query Details",
            f"",
            f"| # | Topic | Query (truncated) | Latency (ms) | Results | Recall@5 | GT Count |",
            f"|---|-------|-------------------|-------------|---------|----------|----------|",
        ])
        for i, qd in enumerate(r.get("per_query", []), 1):
            q_short = qd["query"][:60] + ("..." if len(qd["query"]) > 60 else "")
            lines.append(
                f"| {i} | {qd['topic']} | {q_short} | {qd['latency_ms']:.1f} | "
                f"{qd['result_count']} | {qd['recall_at_5']:.3f} | {qd['ground_truth_count']} |"
            )

    lines.extend([
        f"",
        f"## Methodology",
        f"",
        f"- **Backend**: PostgreSQL FTS with `pg_trgm` extension, `simple` text search configuration",
        f"- **Query Set**: 50 queries (5 per topic × 10 topics), each query targets a specific topic cluster",
        f"- **Ground Truth**: Memories tagged with matching `[topic:T#]` marker in content",
        f"- **Recall@5**: |top-5 ∩ ground_truth| / min(5, |ground_truth|)",
        f"- **Latency**: Wall-clock time per single query execution (includes network + query + fetch)",
        f"- **QPS**: Total queries / total wall-clock time (sequential execution)",
        f"",
        f"## Notes",
        f"",
        f"- Benchmark runs queries sequentially (single-threaded). Parallel QPS would scale with connection pool size.",
        f"- PostgreSQL `ts_rank` uses TF-IDF-like scoring; ranking quality depends on term frequency distribution.",
        f"- For 100K scale, ensure PostgreSQL has adequate `shared_buffers` and `work_mem` for index scan performance.",
        f"- Results CSV: `{csv_path}`",
    ])

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description="BEAM Scale Benchmark for Trinity")
    parser.add_argument("--scale", choices=["1K", "10K", "100K", "all"], default="all",
                        help="Target scale to benchmark. 'all' runs 1K→10K→100K sequentially.")
    parser.add_argument("--queries", type=int, default=50,
                        help="Number of queries to run per scale (max 50)")
    parser.add_argument("--output-csv", default=os.path.join(BENCHMARK_DIR, "beam_results.csv"),
                        help="Output CSV path")
    parser.add_argument("--output-md", default=os.path.join(BENCHMARK_DIR, "beam_report.md"),
                        help="Output Markdown report path")
    args = parser.parse_args()

    scales = ["1K", "10K", "100K"] if args.scale == "all" else [args.scale]
    query_count = min(args.queries, len(QUERY_SET))
    queries = QUERY_SET[:query_count]

    print(f"BEAM Benchmark — Scales: {scales} × {query_count} queries each")
    print(f"PostgreSQL: {PG_CONFIG['host']}:{PG_CONFIG['port']}/{PG_CONFIG['dbname']}")

    conn = connect_pg()
    total_memories = get_total_memories(conn)
    print(f"Database contains {total_memories:,} total active memories")

    all_results = []

    for scale in scales:
        print(f"\n{'='*60}")
        print(f"  Running benchmark at scale {scale}...")

        scale_total = get_total_memories(conn)
        scale_label = get_scale_label(conn)

        if scale_total < 100:
            print(f"  WARNING: Only {scale_total} memories found. Run beam_data_generator.py --scale {scale} first.")
            print(f"  Skipping {scale} benchmark.")
            continue

        result = run_benchmark(conn, queries)

        result["scale"] = scale
        result["memory_count"] = scale_total

        print(f"  Memory count: {result['memory_count']:,}")
        print(f"  Queries run:  {result['total_queries']}")
        print(f"  QPS:          {result['qps']:.2f}")
        print(f"  P50 latency:  {result['p50_ms']:.2f} ms")
        print(f"  P95 latency:  {result['p95_ms']:.2f} ms")
        print(f"  P99 latency:  {result['p99_ms']:.2f} ms")
        print(f"  Mean latency: {result['mean_lat_ms']:.2f} ms")
        print(f"  Recall@5:     {result['mean_recall_at_5']:.4f}")

        all_results.append(result)

    conn.close()

    if all_results:
        generate_report(all_results, args.output_csv, args.output_md)
        print(f"\nReports generated:")
        print(f"  CSV:    {args.output_csv}")
        print(f"  Report: {args.output_md}")
    else:
        print("\nNo results generated. Check data availability.")


if __name__ == "__main__":
    main()
