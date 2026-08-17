"""
Trinity Benchmark Suite — MemArena-style public evaluation framework.

Version: v1.0.0
Paradigm: MemArena (arXiv:2509.21771, 2025)
Supported datasets: LoCoMo, LongMemEval, MemoryAgentBench, LoCoMo-R1
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "Trinity Team"
__paradigm__ = "MemArena"

from benchmarks.arena import ArenaRunner
from benchmarks.metrics import (
    MetricRegistry,
    RecallAtK,
    MRR,
    NDCG,
    Faithfulness,
    HallucinationRate,
    LatencyStats,
    MemoryCompressionRatio,
    RetrievalPrecision,
)
from benchmarks.datasets import DatasetLoader, LoCoMoDataset, LongMemEvalDataset, LoCoMoR1Dataset
from benchmarks.report import BenchmarkReport

__all__ = [
    "ArenaRunner",
    "MetricRegistry",
    "RecallAtK",
    "MRR",
    "NDCG",
    "Faithfulness",
    "HallucinationRate",
    "LatencyStats",
    "MemoryCompressionRatio",
    "RetrievalPrecision",
    "DatasetLoader",
    "LoCoMoDataset",
    "LongMemEvalDataset",
    "LoCoMoR1Dataset",
    "BenchmarkReport",
]
