"""MemoryAggregator - benchmark / diagnostics mixin (split from aggregator.py).
"""

from __future__ import annotations

import json
import logging
import math
import os
import pickle
import threading
import time
from collections import Counter, deque
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple, Union

# ── v7.1.0: Observability & Tracing ──
from trinity.agents.observability import ObservabilityManager, RequestTracer

import numpy as np

from trinity.agents.dimensions import (
    DEFAULT_CONFIDENCE,
    CONFIDENCE_BOOST_PER_AGENT,
    MAX_CONFIDENCE,
    TOPIC_MAX_TOPICS,
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
)

from ._constants import logger


class _DiagnosticsMixin:

    def run_benchmark(self) -> List[dict]:
        """Run the full benchmark suite and return results as dicts."""
        from trinity.agents.benchmark import MemoryBenchmark
        bench = MemoryBenchmark(self)
        results = bench.run_full_suite()
        return [
            {
                "name": r.name,
                "success_rate": r.success_rate,
                "avg_latency_ms": r.avg_latency_ms,
                "p50_ms": r.p50_latency_ms,
                "p95_ms": r.p95_latency_ms,
                "details": r.details,
            }
            for r in results
        ]
