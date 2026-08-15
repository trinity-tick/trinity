"""
Trinity API middleware helpers (M3-3 / M3-4)
=============================================
Self-contained helpers for the REST API server:

  - Rate-limit configuration from ``TRINITY_RATE_LIMIT_*`` environment
    variables and the path/method predicate used by the rate-limit
    middleware (which lives in ``trinity.api.server``).
  - A lightweight, dependency-free Prometheus metrics registry
    (counters + histogram) rendered in Prometheus text exposition
    format, plus the ``metrics_dispatch`` middleware hook that records
    per-request counters/durations.

No third-party metric library is required (``prometheus_client`` is not
guaranteed to be installed); the registry is thread-safe and renders
``text/plain; version=0.0.4`` compatible output.

Metrics emitted:
  - ``trinity_http_requests_total{method,path,status}``        (counter)
  - ``trinity_http_request_duration_seconds{method,path}``     (histogram)
  - ``trinity_rate_limit_denied_total{path}``                  (counter)

``/metrics`` itself is exempt from recording (and from rate limiting) to
avoid a scrape feedback loop.
"""

import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

# ── Rate-limit configuration (environment) ───────────────────────────────

RATE_LIMITED_PREFIXES: Tuple[str, ...] = ("/memories", "/memory/", "/agents/")
RATE_LIMITED_METHODS: Tuple[str, ...] = ("POST", "PUT", "DELETE")


def _env_int(name: str, default: int) -> int:
    """Read a positive-int env var, falling back to `default` on junk input."""
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def rate_limit_enabled() -> bool:
    """``TRINITY_RATE_LIMIT_ENABLED`` — default ``on``.

    Accepts ``on/off/1/0/true/false/yes/no`` (case-insensitive).
    Read per request so the kill-switch takes effect without a restart.
    """
    raw = os.environ.get("TRINITY_RATE_LIMIT_ENABLED", "on").strip().lower()
    return raw in ("1", "true", "yes", "on")


def rate_limit_rate() -> int:
    """``TRINITY_RATE_LIMIT_RATE`` — default 60 (tokens per second)."""
    return _env_int("TRINITY_RATE_LIMIT_RATE", 60)


def rate_limit_burst() -> int:
    """``TRINITY_RATE_LIMIT_BURST`` — default 120 (max bucket tokens)."""
    return _env_int("TRINITY_RATE_LIMIT_BURST", 120)


def is_rate_limited_request(path: str, method: str) -> bool:
    """Decide whether a request consumes a rate-limit token.

    Rules:
      - Only POST/PUT/DELETE on ``/memories``, ``/memory/*`` and
        ``/agents/*`` are limited (write endpoints).
      - Read endpoints (GET/HEAD) are never limited.
      - ``/metrics`` is always exempt (no rate limiting, no counting loop).
      - ``/memory/search/*`` are read-only search endpoints even though they
        use POST (the body carries the query), so they are exempt
        (2026-08-15 stress fix: 8-thread concurrent search was 429-throttled).
    """
    if method not in RATE_LIMITED_METHODS:
        return False
    if path == "/metrics" or path.startswith("/metrics/"):
        return False
    if path.startswith("/memory/search/"):
        return False
    return path.startswith(RATE_LIMITED_PREFIXES)


# ── Prometheus text-format metrics registry (self-implemented) ──────────

HISTOGRAM_BUCKETS: Tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0,
)

_METRIC_HELP = {
    "trinity_http_requests_total": "Total HTTP requests served",
    "trinity_http_request_duration_seconds": "HTTP request duration in seconds",
    "trinity_rate_limit_denied_total": "Total requests denied by rate limiting",
}


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _fmt_labels(items: Tuple[Tuple[str, str], ...], extra: Optional[Dict[str, str]] = None) -> str:
    """Render Prometheus label set (sorted keys) as ``{k="v",...}`` or ``""``."""
    label_map: Dict[str, str] = dict(items)
    if extra:
        label_map.update(extra)
    if not label_map:
        return ""
    parts = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(label_map.items()))
    return "{" + parts + "}"


def _fmt_number(value: float) -> str:
    """Format a sample value without float noise (``1`` not ``1.0``)."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.9g}"


class MetricsRegistry:
    """Thread-safe counters + histogram in Prometheus text exposition format."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (name, label_items) -> value
        self._counters: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], float] = {}
        # (name, label_items) -> {"buckets": [...], "sum": float, "count": int}
        self._histograms: Dict[
            Tuple[str, Tuple[Tuple[str, str], ...]],
            Dict[str, Any],
        ] = {}

    # ── recording ────────────────────────────────────────────────────────

    def inc(self, name: str, labels: Optional[Dict[str, str]] = None, amount: float = 1.0) -> None:
        """Increment a counter identified by `name` + `labels`."""
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + amount

    def observe(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        """Record one histogram observation with `name` + `labels`."""
        key = (name, tuple(sorted((labels or {}).items())))
        with self._lock:
            entry = self._histograms.get(key)
            if entry is None:
                entry = {"buckets": [0] * len(HISTOGRAM_BUCKETS), "sum": 0.0, "count": 0}
                self._histograms[key] = entry
            for i, bound in enumerate(HISTOGRAM_BUCKETS):
                if value <= bound:
                    entry["buckets"][i] += 1
            entry["sum"] += value
            entry["count"] += 1

    # ── rendering ────────────────────────────────────────────────────────

    def render(self) -> str:
        """Render all registered series in Prometheus text format (0.0.4)."""
        lines: List[str] = []
        with self._lock:
            # Counters — group by name so HELP/TYPE appear once per metric.
            by_name: Dict[str, List[Tuple[Tuple[Tuple[str, str], ...], float]]] = {}
            for (name, label_items), value in self._counters.items():
                by_name.setdefault(name, []).append((label_items, value))
            for name in sorted(by_name):
                lines.append(f"# HELP {name} {_METRIC_HELP.get(name, '')}")
                lines.append(f"# TYPE {name} counter")
                for label_items, value in sorted(by_name[name]):
                    lines.append(f"{name}{_fmt_labels(label_items)} {_fmt_number(value)}")

            # Histograms — le buckets + sum/count.
            hist_by_name: Dict[
                str, List[Tuple[Tuple[Tuple[str, str], ...], Dict[str, Any]]]
            ] = {}
            for (name, label_items), entry in self._histograms.items():
                hist_by_name.setdefault(name, []).append((label_items, entry))
            for name in sorted(hist_by_name):
                lines.append(f"# HELP {name} {_METRIC_HELP.get(name, '')}")
                lines.append(f"# TYPE {name} histogram")
                for label_items, entry in sorted(hist_by_name[name]):
                    for i, bound in enumerate(HISTOGRAM_BUCKETS):
                        lines.append(
                            f"{name}_bucket{_fmt_labels(label_items, {'le': f'{bound:g}'})} "
                            f"{entry['buckets'][i]}"
                        )
                    lines.append(
                        f"{name}_bucket{_fmt_labels(label_items, {'le': '+Inf'})} {entry['count']}"
                    )
                    lines.append(f"{name}_sum{_fmt_labels(label_items)} {_fmt_number(entry['sum'])}")
                    lines.append(f"{name}_count{_fmt_labels(label_items)} {entry['count']}")
        return "\n".join(lines) + "\n"


# Module-level registry singleton shared by the app.
_metrics_registry = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    """Return the module-level metrics registry singleton."""
    return _metrics_registry


# ── HTTP metrics middleware hook ─────────────────────────────────────────

async def metrics_dispatch(request: Request, call_next):
    """Record request counter + duration histogram.

    Exempts ``/metrics`` itself so scraping does not feed back into the
    metrics (no counting loop). Rate-limit 429 responses produced by inner
    middleware are recorded with their real status code.
    """
    path = request.url.path
    if path == "/metrics" or path.startswith("/metrics/"):
        return await call_next(request)

    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        status = 500
        raise
    finally:
        elapsed = time.perf_counter() - start
        get_metrics().inc(
            "trinity_http_requests_total",
            {"method": request.method, "path": path, "status": str(status)},
        )
        get_metrics().observe(
            "trinity_http_request_duration_seconds",
            elapsed,
            {"method": request.method, "path": path},
        )
