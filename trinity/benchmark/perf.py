"""
Trinity Performance Benchmark Framework — Standardized QPS / Latency / Recall@K.

Provides a unified, reproducible performance benchmarking system:
  - QPS (Queries Per Second): throughput under concurrent load
  - P50 / P95 / P99: end-to-end percentile latency
  - Recall@K: retrieval accuracy at K

All benchmarks are self-contained (no external services required for mock mode)
and produce standardized JSON reports.

Usage::

    from trinity.benchmark.perf import PerformanceBench, measure_qps, measure_recall

    bench = PerformanceBench()
    bench.run_all()  # QPS + latency + recall

    qps = measure_qps(iterations=1000, concurrency=4)
    lat = measure_latency(iterations=500, warmup=50)
    rec = measure_recall(test_queries, expected_ids, ks=[1, 5, 10])
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import math
import os
import random
import statistics
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

# ── Core Metrics Dataclasses ─────────────────────────────────────────────


@dataclass
class LatencyStats:
    """Standardized latency statistics."""
    count: int = 0
    mean_ms: float = 0.0
    min_ms: float = 0.0
    max_ms: float = 0.0
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    p999_ms: float = 0.0
    stdev_ms: float = 0.0

    @classmethod
    def from_samples(cls, samples_ms: List[float]) -> LatencyStats:
        """Compute LatencyStats from raw latency samples.

        Args:
            samples_ms: List of latency measurements in milliseconds.

        Returns:
            LatencyStats with computed percentiles.
        """
        if not samples_ms:
            return cls()

        sorted_lat = sorted(samples_ms)
        n = len(sorted_lat)

        return cls(
            count=n,
            mean_ms=round(statistics.mean(sorted_lat), 3),
            min_ms=round(sorted_lat[0], 3),
            max_ms=round(sorted_lat[-1], 3),
            p50_ms=round(cls._percentile(sorted_lat, 50), 3),
            p90_ms=round(cls._percentile(sorted_lat, 90), 3),
            p95_ms=round(cls._percentile(sorted_lat, 95), 3),
            p99_ms=round(cls._percentile(sorted_lat, 99), 3),
            p999_ms=round(cls._percentile(sorted_lat, 99.9), 3),
            stdev_ms=round(statistics.stdev(sorted_lat), 3) if n > 1 else 0.0,
        )

    @staticmethod
    def _percentile(sorted_data: List[float], percentile: float) -> float:
        """Compute a percentile from sorted data."""
        if not sorted_data:
            return 0.0
        n = len(sorted_data)
        idx = (percentile / 100.0) * (n - 1)
        lower = int(math.floor(idx))
        upper = int(math.ceil(idx))
        if lower == upper:
            return sorted_data[lower]
        frac = idx - lower
        return sorted_data[lower] * (1 - frac) + sorted_data[upper] * frac

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return {
            "count": self.count,
            "mean_ms": self.mean_ms,
            "min_ms": self.min_ms,
            "max_ms": self.max_ms,
            "p50_ms": self.p50_ms,
            "p90_ms": self.p90_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "p999_ms": self.p999_ms,
            "stdev_ms": self.stdev_ms,
        }


@dataclass
class QPSResult:
    """Standardized QPS benchmark result."""
    total_queries: int = 0
    total_time_s: float = 0.0
    qps: float = 0.0
    concurrency: int = 0
    errors: int = 0
    latency: LatencyStats = field(default_factory=LatencyStats)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        return {
            "total_queries": self.total_queries,
            "total_time_s": round(self.total_time_s, 3),
            "qps": round(self.qps, 2),
            "concurrency": self.concurrency,
            "errors": self.errors,
            "error_rate": round(self.errors / max(self.total_queries, 1), 4),
            "latency": self.latency.to_dict(),
        }


@dataclass
class RecallResult:
    """Standardized Recall@K benchmark result."""
    total_queries: int = 0
    recall_at: Dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0  # Mean Reciprocal Rank
    ndcg_at_10: float = 0.0  # NDCG@10

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict."""
        recall_dict = {f"Recall@{k}": round(v, 4) for k, v in self.recall_at.items()}
        return {
            "total_queries": self.total_queries,
            **recall_dict,
            "MRR": round(self.mrr, 4),
            "NDCG@10": round(self.ndcg_at_10, 4),
        }


# ── QPS Measurement ───────────────────────────────────────────────────────


def measure_qps(
    search_fn: Callable[[str], Any],
    queries: Optional[List[str]] = None,
    iterations: int = 1000,
    concurrency: int = 1,
    warmup: int = 20,
) -> QPSResult:
    """Measure queries per second (QPS) under load.

    Args:
        search_fn: Callable that takes a query string and returns results.
            Should raise on error.
        queries: List of query strings. If None, synthetic queries are used.
        iterations: Total number of queries to execute.
        concurrency: Number of concurrent workers (ThreadPoolExecutor).
        warmup: Number of warmup queries before measurement.

    Returns:
        QPSResult with throughput and latency breakdown.
    """
    if queries is None:
        queries = _synthetic_queries(100)

    latencies: List[float] = []
    errors = 0
    lock = threading.Lock()

    # Warmup phase
    for _ in range(min(warmup, iterations // 2)):
        q = queries[_ % len(queries)]
        try:
            search_fn(q)
        except Exception:
            pass

    # Measurement phase
    start_time = time.perf_counter()

    def worker(batch: List[str]) -> None:
        nonlocal errors
        for q in batch:
            t0 = time.perf_counter()
            try:
                search_fn(q)
                elapsed = (time.perf_counter() - t0) * 1000
                with lock:
                    latencies.append(elapsed)
            except Exception:
                with lock:
                    errors += 1

    if concurrency > 1:
        # Distribute queries across workers
        batches = _split_batches(queries, iterations, concurrency)
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
            list(executor.map(worker, batches))
    else:
        # Sequential execution
        for i in range(iterations):
            q = queries[i % len(queries)]
            t0 = time.perf_counter()
            try:
                search_fn(q)
                latencies.append((time.perf_counter() - t0) * 1000)
            except Exception:
                errors += 1

    total_time = time.perf_counter() - start_time
    qps = len(latencies) / total_time if total_time > 0 else 0.0

    return QPSResult(
        total_queries=iterations,
        total_time_s=total_time,
        qps=qps,
        concurrency=concurrency,
        errors=errors,
        latency=LatencyStats.from_samples(latencies),
    )


# ── Latency Percentile Measurement ────────────────────────────────────────


def measure_latency(
    search_fn: Callable[[str], Any],
    queries: Optional[List[str]] = None,
    iterations: int = 500,
    warmup: int = 50,
) -> LatencyStats:
    """Measure end-to-end latency percentiles.

    Args:
        search_fn: Callable search function.
        queries: Query strings.
        iterations: Number of measurement iterations.
        warmup: Warmup iterations.

    Returns:
        LatencyStats with P50/P90/P95/P99/P99.9.
    """
    if queries is None:
        queries = _synthetic_queries(100)

    samples: List[float] = []

    # Warmup
    for i in range(min(warmup, iterations // 2)):
        q = queries[i % len(queries)]
        try:
            search_fn(q)
        except Exception:
            pass

    # Measurement
    for i in range(iterations):
        q = queries[i % len(queries)]
        t0 = time.perf_counter()
        try:
            search_fn(q)
            samples.append((time.perf_counter() - t0) * 1000)
        except Exception:
            pass

    return LatencyStats.from_samples(samples)


# ── Recall@K Measurement ─────────────────────────────────────────────────


def measure_recall(
    search_fn: Callable[[str], Optional[List[str]]],
    test_queries: List[str],
    expected_ids: List[List[str]],
    ks: Optional[List[int]] = None,
) -> RecallResult:
    """Measure Recall@K retrieval accuracy.

    For each test query, executes search_fn and checks how many expected
    document IDs appear in the top-K results.

    Args:
        search_fn: Callable(query) → list of retrieved document IDs
            (or None/empty on failure). Results should be ranked.
        test_queries: List of query strings.
        expected_ids: Parallel list of expected document ID lists.
        ks: List of K values (default [1, 5, 10, 20]).

    Returns:
        RecallResult with Recall@K, MRR, and NDCG@10.
    """
    if ks is None:
        ks = [1, 5, 10, 20]

    n = len(test_queries)
    recalls: Dict[int, List[float]] = defaultdict(list)
    reciprocal_ranks: List[float] = []
    ndcg_scores: List[float] = []

    for query, expected in zip(test_queries, expected_ids):
        expected_set = set(expected)
        if not expected_set:
            n -= 1
            continue

        try:
            retrieved = search_fn(query) or []
        except Exception:
            continue

        # Recall@K
        for k in ks:
            hits = sum(1 for rid in retrieved[:k] if rid in expected_set)
            recall = hits / len(expected_set) if expected_set else 0.0
            recalls[k].append(recall)

        # MRR
        rr = 0.0
        for rank, rid in enumerate(retrieved, start=1):
            if rid in expected_set:
                rr = 1.0 / rank
                break
        reciprocal_ranks.append(rr)

        # NDCG@10
        dcg = 0.0
        idcg = _compute_idcg(min(len(expected_set), 10))
        for rank, rid in enumerate(retrieved[:10], start=1):
            if rid in expected_set:
                dcg += 1.0 / math.log2(rank + 1)
        ndcg = dcg / idcg if idcg > 0 else 0.0
        ndcg_scores.append(ndcg)

    recall_at_k = {
        k: statistics.mean(vals) if vals else 0.0
        for k, vals in recalls.items()
    }
    mrr = statistics.mean(reciprocal_ranks) if reciprocal_ranks else 0.0
    ndcg = statistics.mean(ndcg_scores) if ndcg_scores else 0.0

    return RecallResult(
        total_queries=n,
        recall_at=recall_at_k,
        mrr=mrr,
        ndcg_at_10=ndcg,
    )


def _compute_idcg(n: int) -> float:
    """Compute ideal DCG for n relevant items."""
    return sum(1.0 / math.log2(i + 2) for i in range(n))


# ── PerformanceBench (Unified Runner) ─────────────────────────────────────


class PerformanceBench:
    """Unified performance benchmark runner.

    Orchestrates QPS, latency, and Recall@K benchmarks with standardized
    reporting and JSON export.

    Usage::

        bench = PerformanceBench()
        bench.run_qps(search_fn, iterations=1000, concurrency=4)
        bench.run_latency(search_fn, iterations=500)
        bench.run_recall(search_fn, test_queries, expected_ids)
        report = bench.generate_report()
        bench.save_report("output/perf_report.json")
    """

    def __init__(self, name: str = "trinity-perf-bench"):
        self.name = name
        self.start_time = datetime.now(timezone.utc)
        self._qps: Optional[QPSResult] = None
        self._latency: Optional[LatencyStats] = None
        self._recall: Optional[RecallResult] = None
        self._lock = threading.RLock()

    def run_qps(
        self,
        search_fn: Callable[[str], Any],
        iterations: int = 1000,
        concurrency: int = 1,
        queries: Optional[List[str]] = None,
    ) -> QPSResult:
        """Run QPS benchmark and store result."""
        with self._lock:
            self._qps = measure_qps(
                search_fn=search_fn,
                queries=queries,
                iterations=iterations,
                concurrency=concurrency,
            )
        return self._qps

    def run_latency(
        self,
        search_fn: Callable[[str], Any],
        iterations: int = 500,
        queries: Optional[List[str]] = None,
    ) -> LatencyStats:
        """Run latency benchmark and store result."""
        with self._lock:
            self._latency = measure_latency(
                search_fn=search_fn,
                queries=queries,
                iterations=iterations,
            )
        return self._latency

    def run_recall(
        self,
        search_fn: Callable[[str], Optional[List[str]]],
        test_queries: List[str],
        expected_ids: List[List[str]],
        ks: Optional[List[int]] = None,
    ) -> RecallResult:
        """Run Recall@K benchmark and store result."""
        with self._lock:
            self._recall = measure_recall(
                search_fn=search_fn,
                test_queries=test_queries,
                expected_ids=expected_ids,
                ks=ks,
            )
        return self._recall

    def run_all(
        self,
        search_fn: Callable[[str], Any],
        recall_search_fn: Optional[Callable[[str], Optional[List[str]]]] = None,
        iterations: int = 500,
        concurrency: int = 1,
    ) -> Dict[str, Any]:
        """Run all benchmarks (QPS + latency; recall requires test data).

        Args:
            search_fn: Function for QPS and latency.
            recall_search_fn: Function returning document ID lists for Recall@K.
            iterations: Measurement iterations.
            concurrency: Concurrency level for QPS.

        Returns:
            Full report dict.
        """
        self.run_qps(search_fn, iterations=iterations, concurrency=concurrency)
        self.run_latency(search_fn, iterations=iterations)

        if recall_search_fn is not None:
            test_queries, expected_ids = _synthetic_recall_data()
            self.run_recall(recall_search_fn, test_queries, expected_ids)

        return self.generate_report()

    def generate_report(self) -> Dict[str, Any]:
        """Generate a comprehensive benchmark report.

        Returns:
            Dict with benchmark metadata and results.
        """
        end_time = datetime.now(timezone.utc)
        elapsed = (end_time - self.start_time).total_seconds()

        report: Dict[str, Any] = {
            "benchmark": self.name,
            "version": "1.0.0",
            "timestamp": self.start_time.isoformat(),
            "elapsed_seconds": round(elapsed, 2),
            "environment": {
                "python_version": _python_version(),
                "platform": os.name,
            },
        }

        if self._qps is not None:
            report["qps"] = self._qps.to_dict()

        if self._latency is not None:
            report["latency"] = self._latency.to_dict()

        if self._recall is not None:
            report["recall"] = self._recall.to_dict()

        # Summary line
        parts = []
        if self._qps is not None:
            parts.append(f"QPS={self._qps.qps:.1f}")
        if self._latency is not None:
            parts.append(f"P50={self._latency.p50_ms}ms P95={self._latency.p95_ms}ms P99={self._latency.p99_ms}ms")
        if self._recall is not None:
            parts.append(f"R@5={self._recall.recall_at.get(5, 0):.3f}")
        report["summary"] = " | ".join(parts)

        return report

    def save_report(self, output_path: str) -> str:
        """Save benchmark report to JSON file.

        Args:
            output_path: Destination file path.

        Returns:
            The output path.
        """
        report = self.generate_report()
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info("Benchmark report saved to %s", output_path)
        return output_path

    def statistics(self) -> Dict[str, Any]:
        """Return benchmark statistics summary."""
        return {
            "name": self.name,
            "started": self.start_time.isoformat(),
            "has_qps": self._qps is not None,
            "has_latency": self._latency is not None,
            "has_recall": self._recall is not None,
        }


# ── Mock / Synthetic Helpers ─────────────────────────────────────────────


def _synthetic_queries(count: int = 100) -> List[str]:
    """Generate synthetic queries for benchmarking when real data unavailable.

    Args:
        count: Number of queries to generate.

    Returns:
        List of synthetic query strings.
    """
    templates = [
        "user prefers {topic}",
        "find documents about {topic}",
        "what is the status of {topic}",
        "retrieve memories related to {topic}",
        "search for {topic} configuration",
        "list all {topic} entries",
        "query {topic} with filters",
        "look up {topic} records",
        "index {topic} data now",
        "update {topic} metadata",
    ]
    topics = [
        "machine learning", "deep learning", "NLP", "computer vision",
        "reinforcement learning", "GAN", "transformer", "BERT",
        "GPU training", "model deployment", "data pipeline",
        "feature engineering", "hyperparameter tuning", "ensemble methods",
        "classification", "regression", "clustering", "dimensionality reduction",
        "time series", "anomaly detection",
    ]
    queries = []
    for i in range(count):
        tpl = templates[i % len(templates)]
        topic = topics[i % len(topics)]
        queries.append(tpl.format(topic=topic))
    return queries


def _synthetic_recall_data(
    num_queries: int = 20,
    num_docs_per_query: int = 5,
) -> Tuple[List[str], List[List[str]]]:
    """Generate synthetic Recall@K test data.

    Args:
        num_queries: Number of test queries.
        num_docs_per_query: Expected relevant documents per query.

    Returns:
        Tuple of (test_queries, expected_ids).
    """
    all_docs = [f"doc-{i:04d}" for i in range(100)]
    queries = _synthetic_queries(num_queries)
    expected = []
    for i in range(num_queries):
        start = (i * 7) % (100 - num_docs_per_query)
        expected.append(all_docs[start : start + num_docs_per_query])
    return queries, expected


def _split_batches(
    queries: List[str],
    total: int,
    num_batches: int,
) -> List[List[str]]:
    """Split total query executions into num_batches roughly equal batches."""
    batch_size = max(1, total // num_batches)
    batches = []
    for b in range(num_batches):
        batch = []
        for j in range(batch_size):
            idx = (b * batch_size + j) % len(queries)
            batch.append(queries[idx])
        batches.append(batch)
    # Distribute remainder
    remainder = total - sum(len(b) for b in batches)
    for j in range(remainder):
        batches[j % num_batches].append(queries[j % len(queries)])
    return batches


def _python_version() -> str:
    """Get the Python version string."""
    import sys
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


# ── Mock Search Function for Self-Test ────────────────────────────────────


class _MockSearchEngine:
    """Mock search engine for benchmark self-tests.

    Simulates realistic latency distribution (~1-15ms) with
    configurable Recall@K behavior.
    """

    def __init__(self, recall_at_5: float = 0.85, mean_latency_ms: float = 5.0):
        self._doc_ids = [f"doc-{i:04d}" for i in range(1000)]
        self._recall_at_5 = recall_at_5
        self._mean_latency = mean_latency_ms

    def search(self, query: str) -> List[str]:
        """Simulate a search with realistic latency."""
        # Log-normal distributed latency
        latency = random.lognormvariate(
            mu=math.log(self._mean_latency),
            sigma=0.3,
        )
        time.sleep(latency / 1000.0)
        return self._doc_ids[:10]

    def search_ids(self, query: str) -> Optional[List[str]]:
        """Simulate a search returning document IDs with Recall@K behavior."""
        latency = random.lognormvariate(
            mu=math.log(self._mean_latency),
            sigma=0.3,
        )
        time.sleep(latency / 1000.0)

        # Simulate imperfect recall
        if random.random() < self._recall_at_5:
            return self._doc_ids[:10]
        else:
            # Return some wrong docs for missed recall
            random.shuffle(self._doc_ids)
            return self._doc_ids[:10]


# ── Self-Test ────────────────────────────────────────────────────────────


def self_test() -> str:
    """Run performance benchmark self-tests and return PASS/FAIL."""
    results = []
    engine = _MockSearchEngine()

    # 1. LatencyStats
    samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    stats = LatencyStats.from_samples(samples)
    results.append(("LatencyStats count", "PASS" if stats.count == 10 else "FAIL"))
    results.append(("LatencyStats p50", "PASS" if stats.p50_ms > 0 else "FAIL"))
    results.append(("LatencyStats p95", "PASS" if stats.p95_ms > stats.p50_ms else "FAIL"))
    results.append(("LatencyStats p99", "PASS" if stats.p99_ms >= stats.p95_ms else "FAIL"))

    # 2. QPS measurement (mock, low iterations)
    qps_result = measure_qps(
        search_fn=engine.search,
        iterations=50,
        concurrency=1,
        warmup=5,
    )
    results.append(("QPS total queries", "PASS" if qps_result.total_queries == 50 else "FAIL"))
    results.append(("QPS > 0", "PASS" if qps_result.qps > 0 else "FAIL"))
    results.append(("QPS latency count", "PASS" if qps_result.latency.count > 0 else "FAIL"))

    # 3. QPS with concurrency
    qps_conc = measure_qps(
        search_fn=engine.search,
        iterations=50,
        concurrency=4,
        warmup=5,
    )
    results.append(("QPS concurrency=4", "PASS" if qps_conc.concurrency == 4 else "FAIL"))

    # 4. Latency measurement
    lat = measure_latency(
        search_fn=engine.search,
        iterations=30,
        warmup=5,
    )
    results.append(("Latency measurement count", "PASS" if lat.count >= 25 else "FAIL"))
    results.append(("Latency p99 >= p95", "PASS" if lat.p99_ms >= lat.p95_ms else "FAIL"))

    # 5. Recall@K measurement
    test_q, expected_q = _synthetic_recall_data(10, 5)
    rec = measure_recall(
        search_fn=engine.search_ids,
        test_queries=test_q,
        expected_ids=expected_q,
        ks=[1, 5, 10],
    )
    results.append(("Recall total queries", "PASS" if rec.total_queries > 0 else "FAIL"))
    results.append(("Recall@5 in [0,1]", "PASS" if 0 <= rec.recall_at.get(5, -1) <= 1 else "FAIL"))
    results.append(("Recall MRR in [0,1]", "PASS" if 0 <= rec.mrr <= 1 else "FAIL"))
    results.append(("Recall NDCG@10 in [0,1]", "PASS" if 0 <= rec.ndcg_at_10 <= 1 else "FAIL"))

    # 6. PerformanceBench unified runner
    bench = PerformanceBench("self-test")
    bench.run_qps(engine.search, iterations=30)
    bench.run_latency(engine.search, iterations=30)
    report = bench.generate_report()
    results.append(("Bench report has qps", "PASS" if "qps" in report else "FAIL"))
    results.append(("Bench report has latency", "PASS" if "latency" in report else "FAIL"))
    results.append(("Bench report has summary", "PASS" if "summary" in report else "FAIL"))

    # 7. Report serialization
    json_str = json.dumps(report, indent=2)
    results.append(("Report JSON serializable", "PASS" if len(json_str) > 0 else "FAIL"))

    passed = sum(1 for _, r in results if r == "PASS")
    total = len(results)
    print(f"[SELFTEST_RESULT] perf_bench: {passed}/{total} PASS")
    for name, result in results:
        print(f"  {name}: {result}")

    if passed == total:
        return "PASS"
    return "FAIL"


if __name__ == "__main__":
    self_test()
