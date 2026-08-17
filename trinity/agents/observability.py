# -*- coding: utf-8 -*-
"""
Trinity v7.1.0: Observability & Tracing Module.
Request tracing, latency metrics, memory usage tracking, health aggregation.
"""

import time
import threading
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from collections import defaultdict, deque
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class TraceSpan:
    """A single operation trace span."""
    operation: str
    start_time: float
    end_time: float = 0.0
    status: str = "running"     # running, success, error
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


class RequestTracer:
    """Per-request tracer with span-based timing."""

    def __init__(self, max_spans: int = 100):
        self._spans: List[TraceSpan] = []
        self._active: Dict[str, TraceSpan] = {}
        self._max_spans = max_spans

    def start_span(self, operation: str, **meta) -> TraceSpan:
        span = TraceSpan(operation=operation, start_time=time.time(), metadata=meta)
        self._active[operation] = span
        return span

    def end_span(self, operation: str, status: str = "success"):
        if operation not in self._active:
            return
        span = self._active.pop(operation)
        span.end_time = time.time()
        span.latency_ms = (span.end_time - span.start_time) * 1000
        span.status = status
        if len(self._spans) >= self._max_spans:
            self._spans.pop(0)
        self._spans.append(span)

    def summary(self) -> dict:
        total = len(self._spans)
        if total == 0:
            return {"total_spans": 0}
        latencies = [s.latency_ms for s in self._spans if s.latency_ms > 0]
        errors = sum(1 for s in self._spans if s.status == "error")
        return {
            "total_spans": total,
            "errors": errors,
            "error_rate": round(errors / total, 4) if total else 0,
            "latency_p50_ms": round(sorted(latencies)[len(latencies) // 2], 2) if latencies else 0,
            "latency_p95_ms": round(sorted(latencies)[int(len(latencies) * 0.95)], 2) if latencies else 0,
            "latency_avg_ms": round(sum(latencies) / len(latencies), 2) if latencies else 0,
        }


class ObservabilityManager:
    """Central observability manager for Trinity."""

    def __init__(self, max_history: int = 1000):
        self._lock = threading.Lock()
        # Request metrics
        self._request_count: int = 0
        self._error_count: int = 0
        self._total_latency_ms: float = 0.0
        self._recent_latencies: deque = deque(maxlen=max_history)
        # Operation metrics
        self._op_counts: Dict[str, int] = defaultdict(int)
        self._op_latencies: Dict[str, deque] = defaultdict(lambda: deque(maxlen=500))
        # Memory metrics
        self._memory_ops: Dict[str, int] = defaultdict(int)  # ingest/query/cleanup counts
        # Start time
        self._start_time = time.time()
        self._last_health_check: float = time.time()
        self._health_status: str = "healthy"

    def record_request(self, operation: str, latency_ms: float, status: str = "success", **meta):
        with self._lock:
            self._request_count += 1
            if status == "error":
                self._error_count += 1
            self._total_latency_ms += latency_ms
            self._recent_latencies.append(latency_ms)
            self._op_counts[operation] += 1
            self._op_latencies[operation].append(latency_ms)

    def record_memory_op(self, op_type: str):
        """Record memory operation: ingest, query, cleanup, merge, etc."""
        with self._lock:
            self._memory_ops[op_type] += 1

    def set_health(self, status: str):
        with self._lock:
            self._health_status = status
            self._last_health_check = time.time()

    def dashboard(self) -> dict:
        """Aggregated dashboard for /dashboard endpoint."""
        with self._lock:
            uptime_seconds = time.time() - self._start_time

            # Latency distributions
            op_latency_breakdown = {}
            for op, lats in self._op_latencies.items():
                if lats:
                    sorted_lats = sorted(lats)
                    op_latency_breakdown[op] = {
                        "count": self._op_counts.get(op, 0),
                        "p50_ms": round(sorted_lats[len(sorted_lats) // 2], 2),
                        "p95_ms": (
                            round(sorted_lats[int(len(sorted_lats) * 0.95)], 2)
                            if len(sorted_lats) >= 20
                            else None
                        ),
                        "avg_ms": round(sum(lats) / len(lats), 2),
                    }

            return {
                "uptime_seconds": round(uptime_seconds, 0),
                "uptime_human": f"{int(uptime_seconds // 3600)}h {int((uptime_seconds % 3600) // 60)}m",
                "health": self._health_status,
                "last_health_check": datetime.fromtimestamp(self._last_health_check).isoformat(),
                "requests": {
                    "total": self._request_count,
                    "errors": self._error_count,
                    "error_rate": round(self._error_count / max(self._request_count, 1), 4),
                    "avg_latency_ms": round(
                        self._total_latency_ms / max(self._request_count, 1), 2
                    ),
                },
                "operations": op_latency_breakdown,
                "memory_ops": dict(self._memory_ops),
            }
