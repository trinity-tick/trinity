"""
benchmarks.metrics — Unified metric system for memory system evaluation.

A modular metric framework with compute() / aggregate() semantics.
Each metric is a standalone callable registered in MetricRegistry.
"""

from __future__ import annotations

import math
import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


# ── Base ────────────────────────────────────────────────────────────────────

@dataclass
class MetricResult:
    """Single-run metric output."""

    name: str
    value: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"{self.name}={self.value:.4f}"


class BaseMetric(ABC):
    """Abstract metric with compute() → MetricResult interface."""

    name: str = "base"

    @abstractmethod
    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        """Compute the metric for a single test instance or batch."""
        ...

    @staticmethod
    def aggregate(results: list[MetricResult]) -> MetricResult:
        """Aggregate multiple MetricResult into a single summary."""
        if not results:
            return MetricResult(name="empty", value=0.0)
        values = [r.value for r in results]
        agg = MetricResult(
            name=results[0].name,
            value=statistics.mean(values),
            metadata={"count": len(values), "stdev": statistics.stdev(values) if len(values) > 1 else 0.0},
        )
        return agg


# ── Core Metrics ────────────────────────────────────────────────────────────

class RecallAtK(BaseMetric):
    """Recall@K — fraction of relevant items retrieved in top-K."""

    name = "Recall@K"

    def __init__(self, k: int = 10) -> None:
        self.k = k

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        pred_set = set(predictions[: self.k])
        ref_set = set(references)
        if not ref_set:
            return MetricResult(name=self.name, value=1.0)
        value = len(pred_set & ref_set) / len(ref_set)
        return MetricResult(name=self.name, value=value, metadata={"k": self.k})


class MRR(BaseMetric):
    """Mean Reciprocal Rank — 1 / rank of first relevant item."""

    name = "MRR"

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        ref_set = set(references)
        for idx, pred in enumerate(predictions, start=1):
            if pred in ref_set:
                return MetricResult(name=self.name, value=1.0 / idx, metadata={"rank": idx})
        return MetricResult(name=self.name, value=0.0, metadata={"rank": -1})


class NDCG(BaseMetric):
    """Normalized Discounted Cumulative Gain at K."""

    name = "NDCG"

    def __init__(self, k: int = 10) -> None:
        self.k = k

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        ref_set = set(references)
        dcg = 0.0
        for idx, pred in enumerate(predictions[: self.k], start=1):
            if pred in ref_set:
                dcg += 1.0 / math.log2(idx + 1)
        ideal_len = min(len(ref_set), self.k)
        idcg = sum(1.0 / math.log2(i + 2) for i in range(ideal_len))
        value = dcg / idcg if idcg > 0 else 0.0
        return MetricResult(name=self.name, value=value, metadata={"k": self.k, "dcg": dcg, "idcg": idcg})


class Faithfulness(BaseMetric):
    """Faithfulness — ratio of output claims grounded in context."""

    name = "Faithfulness"

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        grounded_count: int = kwargs.get("grounded_claims", 0)
        total_claims: int = kwargs.get("total_claims", 0)
        if total_claims <= 0:
            return MetricResult(name=self.name, value=1.0)
        value = grounded_count / total_claims
        return MetricResult(name=self.name, value=value, metadata={"grounded": grounded_count, "total": total_claims})


class HallucinationRate(BaseMetric):
    """HallucinationRate — fraction of output claims not grounded in context."""

    name = "HallucinationRate"

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        grounded_count: int = kwargs.get("grounded_claims", 0)
        total_claims: int = kwargs.get("total_claims", 0)
        if total_claims <= 0:
            return MetricResult(name=self.name, value=0.0)
        value = 1.0 - grounded_count / total_claims
        return MetricResult(name=self.name, value=value, metadata={"grounded": grounded_count, "total": total_claims})


class LatencyStats(BaseMetric):
    """Latency P50 / P95 / P99 in milliseconds."""

    name = "Latency"

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        latencies: list[float] = kwargs.get("latencies", [])
        if not latencies:
            return MetricResult(name=self.name, value=0.0)
        arr = np.array(latencies)
        p50 = float(np.percentile(arr, 50))
        p95 = float(np.percentile(arr, 95))
        p99 = float(np.percentile(arr, 99))
        return MetricResult(
            name=self.name,
            value=p50,
            metadata={"p50_ms": p50, "p95_ms": p95, "p99_ms": p99, "mean_ms": float(arr.mean()), "samples": len(latencies)},
        )


class MemoryCompressionRatio(BaseMetric):
    """MemoryCompressionRatio — raw_context_bytes / stored_memory_bytes."""

    name = "MemoryCompressionRatio"

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        raw_bytes: int = kwargs.get("raw_context_bytes", 0)
        stored_bytes: int = kwargs.get("stored_memory_bytes", 1)
        value = raw_bytes / max(stored_bytes, 1)
        return MetricResult(name=self.name, value=value, metadata={"raw_bytes": raw_bytes, "stored_bytes": stored_bytes})


class RetrievalPrecision(BaseMetric):
    """RetrievalPrecision — fraction of retrieved items that are relevant."""

    name = "RetrievalPrecision"

    def __init__(self, k: int = 10) -> None:
        self.k = k

    def compute(self, predictions: list[Any], references: list[Any], **kwargs: Any) -> MetricResult:
        pred_set = set(predictions[: self.k])
        ref_set = set(references)
        if not pred_set:
            return MetricResult(name=self.name, value=0.0)
        value = len(pred_set & ref_set) / len(pred_set)
        return MetricResult(name=self.name, value=value, metadata={"k": self.k})


# ── Registry ────────────────────────────────────────────────────────────────

class MetricRegistry:
    """Central registry for all benchmark metrics."""

    _metrics: dict[str, BaseMetric] = {}

    @classmethod
    def register(cls, metric: BaseMetric) -> None:
        cls._metrics[metric.name] = metric
        cls._metrics[type(metric).__name__] = metric  # also register by class name

    @classmethod
    def get(cls, name: str) -> BaseMetric | None:
        return cls._metrics.get(name)

    @classmethod
    def list_all(cls) -> list[str]:
        return sorted(cls._metrics.keys())

    @classmethod
    def compute_all(
        cls, predictions: list[Any], references: list[Any], **kwargs: Any
    ) -> list[MetricResult]:
        seen: set[int] = set()
        results: list[MetricResult] = []
        for m in cls._metrics.values():
            if id(m) not in seen:
                seen.add(id(m))
                results.append(m.compute(predictions, references, **kwargs))
        return results


# Auto-register default instances
_DEFAULTS: list[BaseMetric] = [
    RecallAtK(k=5),
    RecallAtK(k=10),
    RecallAtK(k=20),
    MRR(),
    NDCG(k=5),
    NDCG(k=10),
    NDCG(k=20),
    Faithfulness(),
    HallucinationRate(),
    LatencyStats(),
    MemoryCompressionRatio(),
    RetrievalPrecision(k=5),
    RetrievalPrecision(k=10),
    RetrievalPrecision(k=20),
]
for _m in _DEFAULTS:
    MetricRegistry.register(_m)
