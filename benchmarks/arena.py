"""
benchmarks.arena — MemArena-style benchmark runner.

MemArena (arXiv:2509.21771): structured arena where multiple memory
systems compete on the same datasets, producing side-by-side comparison reports.

Pipeline: load_dataset → feed_to_systems → probe_questions → collect_metrics
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from benchmarks.datasets import DatasetLoader, DatasetSample
from benchmarks.metrics import MetricRegistry, MetricResult
from benchmarks.report import BenchmarkReport


# ── Memory System Protocol ──────────────────────────────────────────────────

class MemorySystem(Protocol):
    """Protocol that any memory system must implement to enter the arena."""

    name: str

    def ingest(self, conversation: list[dict[str, str]]) -> None:
        """Feed conversation history into the memory system."""
        ...

    def retrieve(self, query: str, top_k: int = 10) -> list[str]:
        """Retrieve relevant memory chunks for a query."""
        ...

    def generate(self, query: str, context: list[str]) -> str:
        """Generate answer given query and retrieved context."""
        ...

    def stats(self) -> dict[str, Any]:
        """Return runtime statistics (memory usage, latency, etc.)."""
        ...


# ── Types ────────────────────────────────────────────────────────────────────

@dataclass
class SystemRunResult:
    """Per-system performance on a single sample."""

    system_name: str
    sample_id: str
    answer: str
    metrics: list[MetricResult]
    retrieval_context: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArenaResult:
    """Aggregated results for all systems across all samples."""

    dataset_name: str
    num_samples: int
    system_results: dict[str, list[SystemRunResult]] = field(default_factory=dict)
    aggregated: dict[str, list[MetricResult]] = field(default_factory=dict)
    elapsed_seconds: float = 0.0


# ── Arena Runner ─────────────────────────────────────────────────────────────

class ArenaRunner:
    """MemArena-style orchestrator: dataset → systems → metrics → report.

    Usage:
        runner = ArenaRunner(dataset, systems=[trinity, mem0])
        result = runner.run()
        runner.report(result, output_dir="reports/")
    """

    def __init__(
        self,
        dataset: DatasetLoader,
        systems: list[MemorySystem],
        metrics: list[str] | None = None,
        verbose: bool = False,
    ) -> None:
        self.dataset = dataset
        self.systems = systems
        self.metric_names = metrics or MetricRegistry.list_all()
        self.verbose = verbose
        self._results: ArenaResult | None = None

    def run(self) -> ArenaResult:
        """Execute the full benchmark pipeline."""
        t0 = time.perf_counter()

        samples = self.dataset.load()
        if self.verbose:
            print(f"[Arena] Loaded {len(samples)} samples from {self.dataset.name}")

        system_results: dict[str, list[SystemRunResult]] = {s.name: [] for s in self.systems}
        aggregated: dict[str, list[MetricResult]] = {s.name: [] for s in self.systems}

        for idx, sample in enumerate(samples):
            for system in self.systems:
                srr = self._run_sample(system, sample, idx)
                system_results[system.name].append(srr)
                aggregated[system.name].extend(srr.metrics)

            if self.verbose and (idx + 1) % 10 == 0:
                print(f"[Arena] Progress: {idx + 1}/{len(samples)}")

        elapsed = time.perf_counter() - t0
        self._results = ArenaResult(
            dataset_name=self.dataset.name,
            num_samples=len(samples),
            system_results=system_results,
            aggregated=aggregated,
            elapsed_seconds=elapsed,
        )

        if self.verbose:
            print(f"[Arena] Done. {len(samples)} samples × {len(self.systems)} systems in {elapsed:.1f}s")

        return self._results

    def _run_sample(self, system: MemorySystem, sample: DatasetSample, idx: int) -> SystemRunResult:
        """Feed conversation → probe question → collect metrics."""
        # Phase 1: Ingest conversation
        t1 = time.perf_counter()
        system.ingest(sample.conversation)
        ingest_latency = (time.perf_counter() - t1) * 1000

        # Phase 2: Retrieve
        t2 = time.perf_counter()
        context = system.retrieve(sample.question, top_k=10)
        retrieve_latency = (time.perf_counter() - t2) * 1000

        # Phase 3: Generate answer
        t3 = time.perf_counter()
        answer = system.generate(sample.question, context)
        gen_latency = (time.perf_counter() - t3) * 1000

        total_latency = ingest_latency + retrieve_latency + gen_latency

        # Phase 4: Compute metrics
        metrics: list[MetricResult] = []
        for mname in self.metric_names:
            metric = MetricRegistry.get(mname)
            if metric is None:
                continue
            if isinstance(metric, type):
                metric = metric()

            kwargs: dict[str, Any] = {}
            if hasattr(metric, "name"):
                mn = metric.name
            else:
                mn = mname

            if mn in ("Latency", "LatencyStats"):
                kwargs["latencies"] = [ingest_latency, retrieve_latency, gen_latency]
            elif mn in ("Faithfulness", "HallucinationRate"):
                kwargs["grounded_claims"] = len(set(context) & set(sample.evidence_spans))
                kwargs["total_claims"] = max(len(sample.evidence_spans), 1)
            elif mn == "MemoryCompressionRatio":
                raw = sum(len(str(t).encode()) for t in sample.conversation)
                stats = system.stats()
                kwargs["raw_context_bytes"] = raw
                kwargs["stored_memory_bytes"] = stats.get("stored_bytes", max(raw // 10, 1))

            try:
                result = metric.compute(context, sample.evidence_spans, **kwargs)
                metrics.append(result)
            except Exception:
                metrics.append(MetricResult(name=mn, value=0.0, metadata={"error": "compute_failed"}))

        return SystemRunResult(
            system_name=system.name,
            sample_id=sample.sample_id,
            answer=answer,
            metrics=metrics,
            retrieval_context=context,
            latency_ms=total_latency,
            extra={"ingest_ms": ingest_latency, "retrieve_ms": retrieve_latency, "gen_ms": gen_latency},
        )

    def report(self, output_dir: str | Path) -> Path:
        """Generate a full benchmark report."""
        if self._results is None:
            raise RuntimeError("Call .run() before .report()")
        reporter = BenchmarkReport(self._results)
        return reporter.generate(Path(output_dir))


# ── Built-in Mock System for self-test ───────────────────────────────────────

class MockMemorySystem:
    """Minimal mock memory system for self-testing the arena."""

    name = "MockSystem"

    def __init__(self, capacity: int = 1000) -> None:
        self._store: list[str] = []
        self.capacity = capacity
        self._stored_bytes: int = 0

    def ingest(self, conversation: list[dict[str, str]]) -> None:
        for turn in conversation:
            text = turn.get("content", "")
            self._store.append(text)
            self._stored_bytes += len(text.encode())
            if len(self._store) > self.capacity:
                self._store = self._store[-self.capacity :]

    def retrieve(self, query: str, top_k: int = 10) -> list[str]:
        # Naive keyword overlap scoring
        q_words = set(query.lower().split())
        scored: list[tuple[float, str]] = []
        for chunk in self._store:
            c_words = set(chunk.lower().split())
            overlap = len(q_words & c_words) / max(len(q_words), 1)
            scored.append((overlap, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[:top_k]]

    def generate(self, query: str, context: list[str]) -> str:
        return "\n".join(context[:3]) if context else "No context available."

    def stats(self) -> dict[str, Any]:
        return {"stored_bytes": self._stored_bytes, "num_chunks": len(self._store)}
