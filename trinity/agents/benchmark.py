# -*- coding: utf-8 -*-
"""
Trinity v7.1.0: Memory Benchmark Suite.
LongMemEval / MemSyco compatible evaluation pipeline.
"""

import time
import random
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    """Single benchmark run result."""
    name: str
    total_operations: int
    success_count: int
    success_rate: float
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    memory_usage_mb: float
    details: Optional[Dict[str, Any]] = None


class MemoryBenchmark:
    """Benchmark suite for Trinity memory operations."""

    # Standard LongMemEval-style test queries
    LONGMEMEVAL_QUERIES = [
        "What are the user's preferences for code formatting?",
        "Summarize the key decisions from the last team meeting.",
        "What topics has the user been researching recently?",
        "Recall the error encountered during the last deployment.",
        "List all configuration changes made this week.",
        "What is the user's preferred communication style?",
        "Summarize the architecture decision from the design review.",
        "What security concerns were raised in recent discussions?",
    ]

    def __init__(self, aggregator):
        self._aggregator = aggregator

    def run_ingest_benchmark(self, num_records: int = 100) -> BenchmarkResult:
        """Benchmark memory ingestion throughput."""
        latencies = []
        success = 0

        for i in range(num_records):
            start = time.time()
            try:
                self._aggregator.ingest(
                    content=(
                        f"Benchmark test record #{i}: This is a sample memory entry for "
                        f"performance testing. It contains typical agent interaction data "
                        f"including tool calls, user preferences, and task outcomes. "
                        f"Topic: benchmark_test_{i % 5}."
                    ),
                    source_agent="benchmark_agent",
                    metadata={"benchmark": True, "batch": i // 25, "topic": f"test_{i % 5}"},
                )
                success += 1
            except Exception:
                pass
            latencies.append((time.time() - start) * 1000)

        return self._build_result("ingest", success, num_records, latencies)

    def run_query_benchmark(self, num_queries: int = 50) -> BenchmarkResult:
        """Benchmark hybrid query performance."""
        latencies = []
        success = 0
        queries = random.choices(self.LONGMEMEVAL_QUERIES, k=num_queries)

        for q in queries:
            start = time.time()
            try:
                results = self._aggregator.query(
                    filters={"query_text": q}, mode="hybrid", limit=10
                )
                if results:
                    success += 1
            except Exception:
                pass
            latencies.append((time.time() - start) * 1000)

        return self._build_result("query", success, num_queries, latencies)

    def run_retrieval_benchmark(self, num_queries: int = 50) -> BenchmarkResult:
        """Benchmark retrieval precision (Recall@K metric)."""
        # Ingest known test records
        known_ids = []
        for i in range(10):
            mid = self._aggregator.ingest(
                content=(
                    f"UNIQUE_TEST_MARKER_{i}: The capital of test-country-{i} is "
                    f"test-city-{i}. Population is {100000 + i * 50000}."
                ),
                source_agent="benchmark_agent",
                metadata={"benchmark_test": True, "test_index": i},
            )
            known_ids.append(mid)

        recall_hits = 0
        latencies = []

        for i, mid in enumerate(known_ids):
            start = time.time()
            try:
                results = self._aggregator.query(
                    filters={"query_text": f"capital of test-country-{i}"},
                    mode="hybrid",
                    limit=5,
                )
                # Check if known record is in results
                if any(r.memory_id == mid for r in results):
                    recall_hits += 1
            except Exception:
                pass
            latencies.append((time.time() - start) * 1000)

        result = self._build_result("retrieval", recall_hits, len(known_ids), latencies)
        result.details = {
            "recall_at_k": recall_hits / max(len(known_ids), 1),
            "total_known": len(known_ids),
        }
        return result

    def run_full_suite(self) -> List[BenchmarkResult]:
        """Run complete benchmark suite."""
        logger.info("Starting Trinity benchmark suite...")
        results = []

        results.append(self.run_ingest_benchmark(100))
        logger.info(
            "  ingest: %.2f%% success, %.2f ms avg",
            results[-1].success_rate * 100,
            results[-1].avg_latency_ms,
        )

        results.append(self.run_query_benchmark(50))
        logger.info(
            "  query:  %.2f%% success, %.2f ms avg",
            results[-1].success_rate * 100,
            results[-1].avg_latency_ms,
        )

        results.append(self.run_retrieval_benchmark(50))
        logger.info(
            "  retrieval: %.2f%% recall@K, %.2f ms avg",
            results[-1].details.get("recall_at_k", 0) * 100,
            results[-1].avg_latency_ms,
        )

        return results

    def _build_result(self, name, success, total, latencies) -> BenchmarkResult:
        sorted_lats = sorted(latencies)
        return BenchmarkResult(
            name=name,
            total_operations=total,
            success_count=success,
            success_rate=success / max(total, 1),
            avg_latency_ms=round(sum(latencies) / max(len(latencies), 1), 2),
            p50_latency_ms=(
                round(sorted_lats[len(sorted_lats) // 2], 2) if sorted_lats else 0
            ),
            p95_latency_ms=(
                round(sorted_lats[int(len(sorted_lats) * 0.95)], 2)
                if sorted_lats
                else 0
            ),
            memory_usage_mb=0.0,  # Would need psutil
        )
