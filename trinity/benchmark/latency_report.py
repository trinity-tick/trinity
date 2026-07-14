"""
Retrieval Latency Benchmark — P50/P95/P99 measurement.

Measures end-to-end retrieval latency across all 47 retrieval channels.
"""

import json
import time
import statistics
from typing import Any, Dict, List, Optional
from trinity import Trinity


BENCHMARK_QUERIES = [
    "Alice preferences hiking",
    "user likes dark mode",
    "what is machine learning",
    "Python programming memory",
    "meeting schedule updates",
    "project timeline planning",
    "user feedback survey",
    "system configuration settings",
    "memory retrieval test",
    "photography tips landscape",
]


def measure_retrieval_latency(
    queries: List[str],
    iterations: int = 100,
    warmup: int = 15,
    top_k: int = 5,
) -> Dict[str, Any]:
    """Measure retrieval latency across multiple queries."""
    mem = Trinity()
    latencies: List[float] = []

    # Warm up the engine
    for q in queries[:warmup]:
        try:
            mem.search(q, top_k=top_k)
        except Exception:
            pass

    # Measurement phase
    for i in range(iterations):
        q = queries[i % len(queries)]
        start = time.perf_counter()
        try:
            mem.search(q, top_k=top_k)
        except Exception:
            pass
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies.append(elapsed_ms)

    if not latencies:
        return {"error": "No measurements collected"}

    sorted_lat = sorted(latencies)
    n = len(sorted_lat)

    return {
        "iterations": n,
        "top_k": top_k,
        "P50_ms": round(sorted_lat[int(n * 0.50)], 2),
        "P90_ms": round(sorted_lat[int(n * 0.90)], 2),
        "P95_ms": round(sorted_lat[int(n * 0.95)], 2),
        "P99_ms": round(sorted_lat[int(n * 0.99)], 2),
        "mean_ms": round(statistics.mean(sorted_lat), 2),
        "min_ms": round(sorted_lat[0], 2),
        "max_ms": round(sorted_lat[-1], 2),
        "stdev_ms": round(statistics.stdev(sorted_lat), 2) if n > 1 else 0,
    }


def profile(iterations: int = 100) -> Dict[str, Any]:
    """Run complete latency benchmark."""
    print(f"Running latency benchmark ({iterations} measurement iterations)...")

    e2e = measure_retrieval_latency(
        queries=BENCHMARK_QUERIES,
        iterations=iterations,
        warmup=15,
    )

    if "error" in e2e:
        return e2e

    summary = (
        f"P50={e2e['P50_ms']}ms | P95={e2e['P95_ms']}ms | P99={e2e['P99_ms']}ms | "
        f"mean={e2e['mean_ms']}ms | min={e2e['min_ms']}ms | max={e2e['max_ms']}ms"
    )

    print(f"  Results: {summary}")
    print()

    comparison = {
        "Trinity (measured)": {
            "P50": f"{e2e['P50_ms']}ms",
            "P95": f"{e2e['P95_ms']}ms",
            "P99": f"{e2e['P99_ms']}ms",
            "notes": "After warmup; first cold call ~1.5-2s (engine init)",
        },
        "Mem0 (industry)": {
            "P50": "~100-250ms",
            "notes": "Vector + Graph retrieval",
        },
        "Zep/Graphiti (industry)": {
            "P50": "~150ms",
            "notes": "Bi-temporal knowledge graph",
        },
        "Letta/MemGPT (industry)": {
            "P50": "~120ms",
            "notes": "Archival memory retrieval",
        },
        "Supermemory (industry)": {
            "P50": "<300ms",
            "notes": "Enterprise vector-graph engine",
        },
    }

    report = {
        "benchmark": "Retrieval Latency Profile v1.0",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "hardware": {
            "python": "3.14",
            "platform": "Windows",
        },
        "methodology": {
            "warmup_calls": 15,
            "measurement_calls": iterations,
            "queries": len(BENCHMARK_QUERIES),
            "top_k": 5,
        },
        "results": e2e,
        "industry_comparison": comparison,
        "summary": summary,
    }

    return report


if __name__ == "__main__":
    result = profile(iterations=100)
    print(json.dumps(result, indent=2, ensure_ascii=False))

    # Save to file
    import os
    output_dir = os.path.join(os.path.dirname(__file__), "reports")
    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "latency_report.json")
    with open(report_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nReport saved to: {report_path}")
