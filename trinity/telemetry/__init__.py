"""
Trinity Telemetry package.

Provides OpenTelemetry-compatible tracing instrumentation for
write (ingest) and search (retrieval) critical paths.

Exports spans to Jaeger via OTLP/HTTP.

Usage::

    from trinity.telemetry import get_tracer, traced, start_span

    tracer = get_tracer()

    @traced("memory.write")
    def write(content: str) -> dict: ...

    with tracer.span("search.hybrid", query="hello"):
        results = engine.search("hello")

    tracer.flush_to_jaeger()
"""

from trinity.telemetry.tracer import (
    Span,
    SpanEvent,
    SpanStatus,
    Tracer,
    end_span,
    get_tracer,
    instrument_search,
    instrument_write,
    start_span,
    traced,
)

__all__ = [
    "Span",
    "SpanEvent",
    "SpanStatus",
    "Tracer",
    "end_span",
    "get_tracer",
    "instrument_search",
    "instrument_write",
    "start_span",
    "traced",
]

__version__ = "1.0.0"
