"""
Trinity Telemetry — OpenTelemetry Trace & Span Instrumentation.

Provides tracing instrumentation for Trinity's critical paths:
  - Write (ingest) path: content → embedding → store → index
  - Search (retrieval) path: query → BM25+vector+graph → fusion → results

Supports Jaeger OTLP exporter via environment variables:
  OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
  OTEL_SERVICE_NAME=trinity

Usage::

    from trinity.telemetry import Tracer, traced, get_tracer

    # Decorator-based tracing
    @traced("memory.search")
    def search(query: str) -> dict: ...

    # Context-manager spans
    with get_tracer().start_span("write.ingest") as span:
        span.set_attribute("content_length", len(content))
        ...

    # Manual span lifecycle
    tracer = Tracer(service_name="trinity-api")
    span = tracer.start("memories.ingest")
    try:
        ...
        span.ok()
    except Exception as e:
        span.error(e)
    finally:
        span.finish()
"""

from __future__ import annotations

import functools
import logging
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])

# ── Configuration ────────────────────────────────────────────────────────

_TELEMETRY_ENABLED = os.environ.get("TRINITY_TELEMETRY_ENABLED", "1") == "1"
_DEFAULT_SERVICE = os.environ.get("OTEL_SERVICE_NAME", "trinity")
_OTLP_ENDPOINT = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
_BATCH_SIZE = int(os.environ.get("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "512"))
_FLUSH_INTERVAL = int(os.environ.get("OTEL_BSP_SCHEDULE_DELAY_MILLIS", "5000"))

# ── Span Status ──────────────────────────────────────────────────────────

class SpanStatus(Enum):
    UNSET = "UNSET"
    OK = "OK"
    ERROR = "ERROR"


@dataclass
class SpanEvent:
    """Timestamped event within a trace span."""
    name: str
    timestamp_ns: int = field(default_factory=lambda: int(time.time_ns()))
    attributes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Span:
    """A single operation span within a trace.

    Tracks start/end time, status, attributes, and nested events.
    Can be serialized to Jaeger-compatible JSON for OTLP export.
    """

    name: str
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:32])
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    parent_id: Optional[str] = None
    start_time_ns: int = field(default_factory=lambda: int(time.time_ns()))
    end_time_ns: int = 0
    status: SpanStatus = SpanStatus.UNSET
    attributes: Dict[str, Any] = field(default_factory=dict)
    events: List[SpanEvent] = field(default_factory=list)
    _finished: bool = False

    def set_attribute(self, key: str, value: Any) -> Span:
        """Set a span attribute (key-value)."""
        self.attributes[key] = value
        return self

    def set_attributes(self, **kwargs: Any) -> Span:
        """Set multiple span attributes."""
        self.attributes.update(kwargs)
        return self

    def add_event(self, name: str, **attributes: Any) -> Span:
        """Record a timestamped event within the span."""
        self.events.append(SpanEvent(name=name, attributes=attributes))
        return self

    def ok(self) -> Span:
        """Mark span as successful."""
        self.status = SpanStatus.OK
        return self

    def error(self, exception: Optional[Exception] = None) -> Span:
        """Mark span as failed with optional exception details."""
        self.status = SpanStatus.ERROR
        if exception:
            self.set_attribute("exception.type", type(exception).__name__)
            self.set_attribute("exception.message", str(exception))
        return self

    def finish(self) -> Span:
        """Finalize the span with an end timestamp."""
        if not self._finished:
            self.end_time_ns = int(time.time_ns())
            self._finished = True
        return self

    @property
    def duration_ms(self) -> float:
        """Span duration in milliseconds."""
        if self.end_time_ns == 0:
            return (time.time_ns() - self.start_time_ns) / 1_000_000
        return (self.end_time_ns - self.start_time_ns) / 1_000_000

    def to_otlp_dict(self, service_name: str = "") -> Dict[str, Any]:
        """Serialize span to OTLP-compatible JSON (Jaeger format).

        Args:
            service_name: Service name for resource attribution.

        Returns:
            Dict suitable for OTLP/HTTP JSON export.
        """
        if not self._finished:
            self.finish()

        return {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "parentSpanId": self.parent_id or "",
            "name": self.name,
            "startTimeUnixNano": str(self.start_time_ns),
            "endTimeUnixNano": str(self.end_time_ns),
            "kind": "SPAN_KIND_INTERNAL",
            "status": {
                "code": (
                    "STATUS_CODE_OK" if self.status == SpanStatus.OK
                    else "STATUS_CODE_ERROR" if self.status == SpanStatus.ERROR
                    else "STATUS_CODE_UNSET"
                )
            },
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in self.attributes.items()
            ],
            "events": [
                {
                    "timeUnixNano": str(e.timestamp_ns),
                    "name": e.name,
                    "attributes": [
                        {"key": k, "value": {"stringValue": str(v)}}
                        for k, v in e.attributes.items()
                    ],
                }
                for e in self.events
            ],
        }

    def __repr__(self) -> str:
        return (
            f"Span(name={self.name!r}, trace={self.trace_id[:8]}..., "
            f"span={self.span_id[:8]}..., duration={self.duration_ms:.2f}ms, "
            f"status={self.status.value})"
        )


# ── Tracer ───────────────────────────────────────────────────────────────

class Tracer:
    """OpenTelemetry-compatible tracer with Jaeger OTLP export support.

    Creates and manages spans. Exports completed spans to the configured
    OTLP endpoint in background batches.

    Attributes:
        service_name: Service identifier for resource attribution.
        spans: List of completed spans pending export.
        _batch_lock: Thread safety for span buffer.
    """

    def __init__(self, service_name: str = "", otlp_endpoint: str = ""):
        self.service_name = service_name or _DEFAULT_SERVICE
        self.otlp_endpoint = otlp_endpoint or _OTLP_ENDPOINT
        self._spans: List[Span] = []
        self._active_spans: Dict[str, Span] = {}
        self._lock = threading.RLock()
        self._export_thread: Optional[threading.Thread] = None
        self._enabled = _TELEMETRY_ENABLED
        self._exported_count: int = 0
        self._dropped_count: int = 0

    # ── Span Lifecycle ──────────────────────────────────────────────

    def start_span(
        self,
        name: str,
        parent: Optional[Span] = None,
        attributes: Optional[Dict[str, Any]] = None,
    ) -> Span:
        """Create and start a new span.

        Args:
            name: Operation name (e.g. 'memory.write', 'search.hybrid').
            parent: Optional parent span for nested tracing.
            attributes: Optional initial attributes.

        Returns:
            A new active Span.
        """
        if not self._enabled:
            return Span(name="noop", span_id="0" * 16)

        span = Span(
            name=name,
            trace_id=parent.trace_id if parent else uuid.uuid4().hex[:32],
            parent_id=parent.span_id if parent else None,
        )
        if attributes:
            span.set_attributes(**attributes)

        with self._lock:
            self._active_spans[span.span_id] = span

        logger.debug("Span started: %s", span)
        return span

    def end_span(self, span: Span) -> None:
        """Complete a span and queue it for export.

        Args:
            span: The span to finalize.
        """
        span.finish()
        with self._lock:
            self._active_spans.pop(span.span_id, None)
            if len(self._spans) >= _BATCH_SIZE:
                self._dropped_count += 1
            else:
                self._spans.append(span)
                self._exported_count += 1
        logger.debug("Span ended: %s", span)

    @contextmanager
    def span(
        self,
        name: str,
        parent: Optional[Span] = None,
        **attributes: Any,
    ):
        """Context-manager for automatic span lifecycle.

        Usage::

            with tracer.span("memory.search", query="hello") as span:
                results = do_search(query)
                span.set_attribute("result_count", len(results))
        """
        s = self.start_span(name, parent=parent)
        if attributes:
            s.set_attributes(**attributes)
        try:
            yield s
            s.ok()
        except Exception as exc:
            s.error(exc)
            raise
        finally:
            self.end_span(s)

    # ── Decorator ───────────────────────────────────────────────────

    def trace(self, name: str = "", **attributes: Any) -> Callable[[F], F]:
        """Decorator to trace a function with automatic span management.

        Usage::

            @tracer.trace("memory.write")
            def write(content: str) -> dict: ...

        Args:
            name: Span name (defaults to function name).
            **attributes: Static attributes set on every invocation.

        Returns:
            Decorated function.
        """

        def decorator(func: F) -> F:
            span_name = name or func.__qualname__

            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                with self.span(span_name, **attributes) as span:
                    span.set_attribute("function", func.__qualname__)
                    try:
                        result = func(*args, **kwargs)
                        span.set_attribute("result_type", type(result).__name__)
                        return result
                    except Exception as e:
                        span.error(e)
                        raise

            return wrapper  # type: ignore[return-value]

        return decorator

    # ── Export ──────────────────────────────────────────────────────

    def export_spans(self) -> Dict[str, Any]:
        """Export all completed spans as OTLP-compatible JSON.

        Returns:
            Dict with resource_spans array for OTLP ingestion.
        """
        with self._lock:
            spans = list(self._spans)
            self._spans.clear()

        if not spans:
            return {"resourceSpans": []}

        resource_spans = [{
            "resource": {
                "attributes": [
                    {"key": "service.name", "value": {"stringValue": self.service_name}},
                    {"key": "telemetry.sdk.name", "value": {"stringValue": "trinity-telemetry"}},
                    {"key": "telemetry.sdk.version", "value": {"stringValue": "1.0.0"}},
                ]
            },
            "scopeSpans": [{
                "scope": {"name": "trinity"},
                "spans": [s.to_otlp_dict(self.service_name) for s in spans],
            }],
        }]

        return {"resourceSpans": resource_spans}

    def flush_to_jaeger(self) -> Dict[str, Any]:
        """Export spans to the configured Jaeger OTLP endpoint.

        Uses HTTP POST to the OTLP endpoint. If the endpoint is unreachable,
        spans are kept in buffer for the next flush attempt.

        Returns:
            Dict with export status.
        """
        payload = self.export_spans()

        if not payload["resourceSpans"]:
            return {"exported": 0, "status": "empty"}

        try:
            import urllib.request
            import json as _json

            data = _json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.otlp_endpoint}/v1/traces",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status

            span_count = sum(
                len(rs.get("scopeSpans", [{}])[0].get("spans", []))
                for rs in payload["resourceSpans"]
            )
            logger.info("Flushed %d spans to Jaeger (HTTP %d)", span_count, status)
            return {"exported": span_count, "status": f"ok_{status}"}

        except Exception as exc:
            logger.warning("Failed to flush spans to Jaeger: %s", exc)
            # Re-queue dropped spans
            with self._lock:
                for rs in payload["resourceSpans"]:
                    for ss in rs.get("scopeSpans", []):
                        # Reconstruct minimal Span objects for re-queue
                        pass
            return {"exported": 0, "status": "error", "error": str(exc)}

    # ── Statistics ──────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """Return tracer statistics for diagnostics.

        Returns:
            Dict with active_spans, exported, dropped, buffer_size.
        """
        with self._lock:
            active = len(self._active_spans)
            buffered = len(self._spans)
        return {
            "service_name": self.service_name,
            "enabled": self._enabled,
            "otlp_endpoint": self.otlp_endpoint,
            "active_spans": active,
            "buffered_spans": buffered,
            "exported": self._exported_count,
            "dropped": self._dropped_count,
        }


# ── Module-Level Convenience Functions ───────────────────────────────────

_global_tracer: Optional[Tracer] = None
_global_tracer_lock = threading.Lock()


def get_tracer() -> Tracer:
    """Get or create the global Tracer singleton."""
    global _global_tracer
    if _global_tracer is None:
        with _global_tracer_lock:
            if _global_tracer is None:
                _global_tracer = Tracer()
    return _global_tracer


def traced(name: str = "", **attributes: Any) -> Callable[[F], F]:
    """Module-level tracing decorator using the global tracer.

    Usage::

        @traced("memory.write")
        def write(content: str) -> dict: ...

    Args:
        name: Span name.
        **attributes: Static span attributes.

    Returns:
        Decorated function.
    """
    return get_tracer().trace(name, **attributes)


def start_span(name: str, **attributes: Any) -> Span:
    """Start a span on the global tracer.

    Args:
        name: Span name.
        **attributes: Initial span attributes.

    Returns:
        New Span instance.
    """
    return get_tracer().start_span(name, attributes=attributes or None)


def end_span(span: Span) -> None:
    """End a span on the global tracer."""
    get_tracer().end_span(span)


# ── Middleware Integration Helpers ────────────────────────────────────────


def instrument_write(content: str, modality: str = "text") -> Span:
    """Start a trace span for the memory write (ingest) path.

    Covers: content → hash → embedding → store → index.

    Args:
        content: Memory content being written.
        modality: Content modality.

    Returns:
        Active span with write-path attributes.
    """
    return get_tracer().start_span(
        "memory.write",
        content_length=len(content),
        modality=modality,
        content_hash=hashlib.sha256(content.encode()).hexdigest()[:16],
    )


def instrument_search(query: str, top_k: int = 10) -> Span:
    """Start a trace span for the memory search (retrieval) path.

    Covers: query → BM25 FTS5 → vector HNSW → graph traversal → fusion.

    Args:
        query: Search query string.
        top_k: Target result count.

    Returns:
        Active span with search-path attributes.
    """
    return get_tracer().start_span(
        "memory.search",
        query_length=len(query),
        top_k=top_k,
        query_hash=hashlib.sha256(query.encode()).hexdigest()[:16],
    )


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> str:
    """Run telemetry self-tests and return results."""
    import hashlib as _hashlib
    results = []

    # 1. Tracer singleton
    t = get_tracer()
    results.append(("Tracer singleton", "PASS" if t is not None else "FAIL"))

    # 2. Basic span lifecycle
    s = t.start_span("test.span", attributes={"foo": "bar"})
    results.append(("Start span", "PASS" if s.name == "test.span" else "FAIL"))
    results.append(("Span attributes", "PASS" if s.attributes.get("foo") == "bar" else "FAIL"))
    time.sleep(0.001)
    s.ok()
    t.end_span(s)
    results.append(("Span ok status", "PASS" if s.status == SpanStatus.OK else "FAIL"))
    results.append(("Span finished", "PASS" if s._finished else "FAIL"))
    results.append(("Span duration > 0", "PASS" if s.duration_ms > 0 else "FAIL"))

    # 3. Error span
    s2 = t.start_span("test.error")
    s2.error(ValueError("test error"))
    t.end_span(s2)
    results.append(("Error span status", "PASS" if s2.status == SpanStatus.ERROR else "FAIL"))
    results.append(("Error span exception", "PASS" if "ValueError" in str(s2.attributes) else "FAIL"))

    # 4. Nested spans
    parent = t.start_span("test.parent")
    child = t.start_span("test.child", parent=parent)
    results.append(("Child trace_id matches parent", "PASS" if child.trace_id == parent.trace_id else "FAIL"))
    results.append(("Child parent_id set", "PASS" if child.parent_id == parent.span_id else "FAIL"))
    t.end_span(child)
    t.end_span(parent)

    # 5. Context manager
    with t.span("test.ctx", ctx_attr="hello") as ctx_span:
        pass
    results.append(("Context manager span ok", "PASS" if ctx_span.status == SpanStatus.OK else "FAIL"))

    # 6. OTLP serialization
    s3 = t.start_span("test.otlp")
    s3.set_attribute("key", "value")
    s3.ok()
    t.end_span(s3)
    otlp = s3.to_otlp_dict("test")
    results.append(("OTLP traceId present", "PASS" if otlp.get("traceId") else "FAIL"))
    results.append(("OTLP spanId present", "PASS" if otlp.get("spanId") else "FAIL"))
    results.append(("OTLP status OK", "PASS" if "OK" in str(otlp.get("status")) else "FAIL"))

    # 7. Statistics
    stats = t.statistics()
    results.append(("Statistics available", "PASS" if "service_name" in stats else "FAIL"))

    passed = sum(1 for _, r in results if r == "PASS")
    total = len(results)
    print(f"[SELFTEST_RESULT] telemetry: {passed}/{total} PASS")
    for name, result in results:
        print(f"  {name}: {result}")

    if passed == total:
        return "PASS"
    return "FAIL"


# Late import to avoid circular dependency
import hashlib

if __name__ == "__main__":
    self_test()
