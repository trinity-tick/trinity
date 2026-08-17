# -*- coding: utf-8 -*-
"""
Trinity Memory — PerformanceBench Integration.

Provides a bridge between memory operations (search / ingest) and the
PerformanceBench framework from trinity.benchmark.perf.

Usage::

    from trinity.memory.bench_integration import MemoryBench

    bench = MemoryBench()
    report = bench.run_all()
    bench.save_report("output/memory_baseline.json")
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from trinity.benchmark.perf import PerformanceBench
from trinity.core.client import Trinity

logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────

_DEFAULT_QUERIES = [
    "user preferences",
    "dark mode settings",
    "project deadlines",
    "API documentation",
    "error handling",
    "database schema",
    "deployment steps",
    "security policies",
    "meeting notes",
    "code review",
    "performance optimization",
    "configuration options",
    "logging strategy",
    "cache invalidation",
    "retry mechanism",
    "authentication flow",
    "file management",
    "search indexing",
    "memory consolidation",
    "task scheduling",
]


class MemoryBench:
    """Unified memory benchmark integration.

    Wraps PerformanceBench with Trinity memory as the search backend,
    providing a convenient interface for baseline generation.

    Usage::

        bench = MemoryBench()
        report = bench.run_all()
        report = bench.run_all(iterations=200, concurrency=2)
    """

    def __init__(self, store_path: Optional[str] = None):
        self._trinity: Optional[Trinity] = None
        self._store_path = store_path
        self._bench = PerformanceBench(name="trinity-memory-baseline")
        self._queries = list(_DEFAULT_QUERIES)

    @property
    def trinity(self) -> Trinity:
        """Lazy-init Trinity instance."""
        if self._trinity is None:
            kwargs: Dict[str, Any] = {}
            if self._store_path:
                kwargs["store_path"] = self._store_path
            self._trinity = Trinity(**kwargs)
        return self._trinity

    def _search_wrapper(self, query: str) -> Dict[str, Any]:
        """Search wrapper compatible with PerformanceBench."""
        return self.trinity.search(query, top_k=10)

    def _recall_search_wrapper(self, query: str) -> Optional[List[str]]:
        """Recall-compatible search returning document IDs."""
        result = self.trinity.search(query, top_k=10)
        results = result.get("results", [])
        return [r.get("memory_id", "") for r in results if r.get("memory_id")]

    def seed_memories(self, count: int = 50) -> int:
        """Seed baseline memories for meaningful benchmark data.

        Args:
            count: Number of synthetic memories to ingest.

        Returns:
            Number of memories ingested.
        """
        topics = [
            "User prefers dark mode for all applications",
            "Project Alpha deadline is 2026-09-15",
            "API v2 endpoint requires authentication header",
            "Database uses PostgreSQL 16 with JSONB columns",
            "Error handling follows RFC 7807 Problem Details",
            "Deployment uses Docker Compose with 4 services",
            "All code reviews require 2 approvals before merge",
            "Search index uses BM25 + vector fusion ranking",
            "Memory TTL defaults to 90 days for general category",
            "Cache implements LRU eviction with 1024 entries",
            "Logging uses structured JSON format with trace IDs",
            "Authentication supports OAuth2 and API key methods",
            "File agent handles PDF, DOCX, XLSX, PPTX formats",
            "Recall@5 target is 0.92 for production deployment",
            "Latency P95 budget is 50ms for search operations",
            "Session timeout is 30 minutes of inactivity",
            "Audit log retained for 365 days",
            "Encryption uses AES-256-GCM with key rotation",
            "Multi-tenant isolation via tenant_id column",
            "Webhook retry uses exponential backoff 5 attempts",
        ]

        ingested = 0
        for i, topic in enumerate(topics[:count]):
            try:
                self.trinity.ingest(
                    content=f"[Baseline #{i+1:02d}] {topic}",
                    source_window="memory_bench",
                    importance=0.5 + (i % 10) * 0.05,
                    tags=["baseline", f"topic_{i % 5}"],
                    category="benchmark",
                    modality="text",
                )
                ingested += 1
            except Exception as exc:
                logger.warning("Seed memory %d failed: %s", i + 1, exc)

        logger.info("Seeded %d baseline memories", ingested)
        return ingested

    def run_all(
        self,
        iterations: int = 500,
        concurrency: int = 1,
        seed_count: int = 50,
    ) -> Dict[str, Any]:
        """Run all benchmarks and return a comprehensive report.

        Args:
            iterations: QPS/latency measurement iterations.
            concurrency: Concurrency level for QPS.
            seed_count: Baseline memories to seed before benchmarking.

        Returns:
            Full benchmark report dict.
        """
        # Ensure baseline data
        self.seed_memories(seed_count)

        # Run benchmarks via PerformanceBench
        report = self._bench.run_all(
            search_fn=self._search_wrapper,
            recall_search_fn=self._recall_search_wrapper,
            iterations=iterations,
            concurrency=concurrency,
        )

        # Inject memory-specific metadata
        stats = self.trinity.stats()
        report.setdefault("memory_stats", {
            "total": stats.get("total_memories", 0) if isinstance(stats, dict) else "N/A",
            "active": stats.get("active_memories", 0) if isinstance(stats, dict) else "N/A",
            "aged": stats.get("aged_memories", 0) if isinstance(stats, dict) else "N/A",
        })

        return report

    def save_report(self, path: str) -> str:
        """Save the latest benchmark report to a JSON file.

        Args:
            path: Output file path.

        Returns:
            Absolute path to saved report.
        """
        report = self._bench.generate_report()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        logger.info("Benchmark report saved to %s", path)
        return os.path.abspath(path)

    @property
    def bench(self) -> PerformanceBench:
        return self._bench


# ── Convenience function ──────────────────────────────────────────────────


def run_memory_baseline(
    iterations: int = 500,
    concurrency: int = 1,
    output_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Convenience function: run full memory baseline and optionally save.

    Args:
        iterations: Measurement iterations.
        concurrency: QPS concurrency.
        output_path: If given, save report to this path.

    Returns:
        Benchmark report.
    """
    bench = MemoryBench()
    report = bench.run_all(iterations=iterations, concurrency=concurrency)

    if output_path:
        bench.save_report(output_path)

    return report


# ── Self-Test ────────────────────────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module self-test."""
    results: Dict[str, Any] = {"module": "trinity.memory.bench_integration", "tests": {}}

    # Test 1: Instantiation
    try:
        bench = MemoryBench()
        assert bench is not None
        results["tests"]["instantiation"] = "PASS"
    except Exception as e:
        results["tests"]["instantiation"] = f"FAIL: {e}"
        return results

    # Test 2: Trinity lazy init
    try:
        trinity = bench.trinity
        assert trinity is not None
        results["tests"]["lazy_init"] = "PASS"
    except Exception as e:
        results["tests"]["lazy_init"] = f"FAIL: {e}"

    # Test 3: Seed memories
    try:
        count = bench.seed_memories(count=10)
        assert count > 0
        results["tests"]["seed_memories"] = f"PASS (seeded={count})"
    except Exception as e:
        results["tests"]["seed_memories"] = f"FAIL: {e}"

    # Test 4: Search wrapper
    try:
        result = bench._search_wrapper("user preferences")
        assert isinstance(result, dict)
        assert "results" in result
        results["tests"]["search_wrapper"] = "PASS"
    except Exception as e:
        results["tests"]["search_wrapper"] = f"FAIL: {e}"

    # Test 5: Recall wrapper
    try:
        ids = bench._recall_search_wrapper("dark mode")
        assert isinstance(ids, list)
        results["tests"]["recall_wrapper"] = "PASS"
    except Exception as e:
        results["tests"]["recall_wrapper"] = f"FAIL: {e}"

    # Test 6: Run all (light)
    try:
        report = bench.run_all(iterations=20, concurrency=1, seed_count=5)
        assert isinstance(report, dict)
        assert "qps" in report or "benchmark" in report
        results["tests"]["run_all_light"] = "PASS"
    except Exception as e:
        results["tests"]["run_all_light"] = f"FAIL: {e}"

    # Summary
    passed = sum(1 for v in results["tests"].values() if "PASS" in str(v))
    total = len(results["tests"])
    results["summary"] = f"{passed}/{total} PASS"

    return results


if __name__ == "__main__":
    import sys
    result = self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if all("PASS" in str(v) for v in result["tests"].values()) else 1)
