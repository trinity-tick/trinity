#!/usr/bin/env python3
"""
P1: GraphQL Production Load Test for Trinity.

Uses strawberry.Schema.execute_sync() to measure GraphQL execution
performance at 3 QPS levels (10/50/100 queries) with concurrent workers.

Metrics: latency p50/p95/p99, throughput, error rate.
"""
import json, os, sys, time, statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"


# ── Queries ─────────────────────────────────────────────────────────────

QUERIES = [
    # Q1: Health status (lightweight)
    {
        "name": "healthQuery",
        "query": """
        query {
            health {
                status
                uptime
            }
        }
        """,
    },
    # Q2: Memory search (medium)
    {
        "name": "searchMemories",
        "query": """
        query {
            searchMemories(query: "machine learning", limit: 5) {
                items {
                    memoryId
                    content
                }
            }
        }
        """,
    },
    # Q3: List agents (medium)
    {
        "name": "listAgents",
        "query": """
        query {
            listAgents(limit: 5) {
                items {
                    agentId
                    name
                    status
                }
            }
        }
        """,
    },
    # Q4: Get memory by ID (lightweight)
    {
        "name": "getMemory",
        "query": """
        query {
            getMemory(memoryId: "mem_test_001") {
                memoryId
                content
                status
            }
        }
        """,
    },
    # Q5: Complex diagnostic query (heavy)
    {
        "name": "diagnostics",
        "query": """
        query {
            diagnostics {
                componentStatus
                version
            }
        }
        """,
    },
]


# ── Load Test ───────────────────────────────────────────────────────────

def run_single_query(schema, query_def: Dict) -> Dict[str, Any]:
    """Execute a single GraphQL query and return timing info."""
    t0 = time.perf_counter()
    try:
        result = schema.execute_sync(query_def["query"])
        elapsed = time.perf_counter() - t0
        errors = getattr(result, "errors", None)
        return {
            "query": query_def["name"],
            "latency_ms": round(elapsed * 1000, 2),
            "success": errors is None or len(errors) == 0,
            "error": str(errors[0])[:200] if errors else None,
        }
    except Exception as e:
        elapsed = time.perf_counter() - t0
        return {
            "query": query_def["name"],
            "latency_ms": round(elapsed * 1000, 2),
            "success": False,
            "error": str(e)[:200],
        }


def run_load_test(schema, num_queries: int, concurrency: int) -> Dict[str, Any]:
    """Run N queries with C concurrent workers."""
    import itertools
    query_cycle = itertools.cycle(QUERIES)
    tasks = [next(query_cycle) for _ in range(num_queries)]

    results = []
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(run_single_query, schema, q) for q in tasks]
        for f in as_completed(futures):
            results.append(f.result())

    total_elapsed = time.perf_counter() - t0
    latencies = [r["latency_ms"] for r in results]
    latencies.sort()
    successes = sum(1 for r in results if r["success"])
    errors = len(results) - successes

    return {
        "num_queries": num_queries,
        "concurrency": concurrency,
        "total_elapsed_s": round(total_elapsed, 2),
        "throughput_qps": round(num_queries / total_elapsed, 2),
        "success_count": successes,
        "error_count": errors,
        "error_rate_pct": round(errors / num_queries * 100, 2) if num_queries else 0,
        "latency_p50_ms": round(latencies[len(latencies)//2], 2),
        "latency_p95_ms": round(latencies[int(len(latencies)*0.95)], 2),
        "latency_p99_ms": round(latencies[int(len(latencies)*0.99)], 2),
        "latency_min_ms": round(min(latencies), 2),
        "latency_max_ms": round(max(latencies), 2),
        "latency_mean_ms": round(statistics.mean(latencies), 2),
    }


# ── Main ────────────────────────────────────────────────────────────────

def main():
    from trinity.api.graphql_schema import schema

    print("Warming up...")
    run_single_query(schema, QUERIES[0])

    levels = [
        (10, 5, "10 QPS warmup"),
        (50, 10, "50 QPS medium"),
        (100, 20, "100 QPS high"),
    ]

    all_results = []
    for num_queries, concurrency, label in levels:
        print(f"  Running {label} ({num_queries} queries, {concurrency} workers)...")
        result = run_load_test(schema, num_queries, concurrency)
        result["label"] = label
        all_results.append(result)
        print(f"    P50={result['latency_p50_ms']}ms P95={result['latency_p95_ms']}ms "
              f"P99={result['latency_p99_ms']}ms QPS={result['throughput_qps']} "
              f"errors={result['error_count']}")

    output_dir = str(TRINITY_ROOT / "output")
    os.makedirs(output_dir, exist_ok=True)
    json_path = output_dir + "/graphql_load_results.json"

    output = {
        "test_date": "2026-08-12",
        "test_method": "strawberry.Schema.execute_sync() with ThreadPoolExecutor",
        "graphql_schema": "trinity.api.graphql_schema (Strawberry)",
        "query_types": [q["name"] for q in QUERIES],
        "levels": all_results,
    }

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\nResults saved to {json_path}")

    # Summary table
    print(f"\n{'='*70}")
    print(f"{'Label':<20} {'P50(ms)':<10} {'P95(ms)':<10} {'P99(ms)':<10} {'QPS':<10} {'Errors':<8}")
    print(f"{'-'*70}")
    for r in all_results:
        print(f"{r['label']:<20} {r['latency_p50_ms']:<10} {r['latency_p95_ms']:<10} "
              f"{r['latency_p99_ms']:<10} {r['throughput_qps']:<10} {r['error_count']:<8}")
    print(f"{'='*70}")

    return all_results


if __name__ == "__main__":
    main()
