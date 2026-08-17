"""Trinity Benchmark Suite — Standardized Performance Measurement.

Provides QPS throughput, P50/P95/P99 latency percentiles, and Recall@K
retrieval accuracy benchmarks with standardized JSON reporting.

Usage::

    from trinity.benchmark import PerformanceBench, measure_qps, measure_latency, measure_recall

    bench = PerformanceBench()
    bench.run_all(search_fn)
    bench.save_report("output/perf.json")
"""

from trinity.benchmark.perf import (
    LatencyStats,
    measure_latency,
    measure_qps,
    measure_recall,
    PerformanceBench,
    QPSResult,
    RecallResult,
)

__all__ = [
    "LatencyStats",
    "PerformanceBench",
    "QPSResult",
    "RecallResult",
    "measure_latency",
    "measure_qps",
    "measure_recall",
]
