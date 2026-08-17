"""Tests for Trinity REST API rate limiting (M3-3) and Prometheus metrics (M3-4).

Covers:
  - Normal requests return 200.
  - POST /memories beyond the token-bucket burst returns 429 with
    {"error": "rate_limit_exceeded", "detail": ...}; read endpoints stay
    unlimited even when the bucket is empty.
  - /metrics exposes trinity_http_requests_total and
    trinity_rate_limit_denied_total (plus the duration histogram and
    trinity_memories_total) in Prometheus text format, without counting
    the /metrics scrape itself.
  - The TRINITY_RATE_LIMIT_ENABLED environment switch works (off -> no
    limiting; on -> limiting resumes), and TRINITY_RATE_LIMIT_RATE/BURST
    defaults are 60/120.

The engine/aggregator are stubbed so tests never touch PostgreSQL; the
module-level rate limiter is reset around every test for isolation.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient

from trinity.api import server
from trinity.api.middleware import (
    is_rate_limited_request,
    rate_limit_burst,
    rate_limit_enabled,
    rate_limit_rate,
)

# ── Stubs ────────────────────────────────────────────────────────────────

class _StubMemory:
    """Stand-in for server.get_memory() — no engine/DB access."""

    def ingest(self, **kwargs):
        return {"id": "stub-memory-id", "ok": True, "content": kwargs.get("content")}


class _StubAggregator:
    """Stand-in for server.get_aggregator() — fixed statistics, no pool."""

    def statistics(self):
        return {
            "total_memories": 7,
            "total_ingested": 12,
            "total_merged": 2,
            "total_queries": 5,
        }

    def _save(self):
        pass


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_server_deps(monkeypatch):
    """Stub engine/aggregator and reset the shared rate limiter per test."""
    monkeypatch.setattr(server, "get_memory", lambda: _StubMemory())
    monkeypatch.setattr(server, "get_aggregator", lambda: _StubAggregator())
    monkeypatch.setenv("TRINITY_RATE_LIMIT_ENABLED", "on")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_RATE", "60")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_BURST", "120")
    server.reconfigure_rate_limiter()
    yield
    # Restore canonical env + full bucket regardless of in-test overrides,
    # so later suites in the same process are never throttled.
    monkeypatch.setenv("TRINITY_RATE_LIMIT_ENABLED", "on")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_RATE", "60")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_BURST", "120")
    server.reconfigure_rate_limiter()


@pytest.fixture
def client():
    """Temporary TestClient instance (never touches the live :8001 service)."""
    with TestClient(server.app) as c:
        yield c


def _post_memory(client, content="probe"):
    """POST /memories with the RBAC-required agent header."""
    return client.post("/memories", json={"content": content}, headers={"X-Agent-ID": "test-agent"})


# ── Tests ────────────────────────────────────────────────────────────────

def test_normal_request_returns_200(client):
    """(a) Ordinary requests succeed."""
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 200


def test_post_memories_beyond_burst_returns_429(monkeypatch, client):
    """(b) More than `burst` consecutive POST /memories → 429 with the
    documented JSON body; read endpoints stay unlimited on an empty bucket."""
    monkeypatch.setenv("TRINITY_RATE_LIMIT_RATE", "1")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_BURST", "3")
    server.reconfigure_rate_limiter()

    statuses = [_post_memory(client).status_code for _ in range(5)]

    assert statuses[:3] == [200, 200, 200]
    assert statuses[3:] == [429, 429]

    last = _post_memory(client)
    assert last.status_code == 429
    body = last.json()
    assert body["error"] == "rate_limit_exceeded"
    assert "detail" in body and body["detail"]

    # Read endpoints are never rate-limited, even with an empty bucket.
    assert client.get("/health").status_code == 200

    # Denials are recorded in the metrics.
    text = client.get("/metrics").text
    assert 'trinity_rate_limit_denied_total{path="/memories"}' in text


def test_metrics_expose_http_and_rate_limit_metrics(monkeypatch, client):
    """(c) /metrics carries request counters, the denied counter, the
    duration histogram and the memory gauge in Prometheus text format."""
    monkeypatch.setenv("TRINITY_RATE_LIMIT_RATE", "1")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_BURST", "2")
    server.reconfigure_rate_limiter()

    client.get("/health")
    for _ in range(4):
        _post_memory(client)  # 2×200 then 2×429

    text = client.get("/metrics").text
    assert 'trinity_http_requests_total{method="GET",path="/health",status="200"}' in text
    assert 'trinity_http_requests_total{method="POST",path="/memories",status="200"}' in text
    assert 'trinity_http_requests_total{method="POST",path="/memories",status="429"}' in text
    assert 'trinity_rate_limit_denied_total{path="/memories"}' in text
    assert 'trinity_http_request_duration_seconds_bucket{le="0.005",method="POST",path="/memories"}' in text
    assert 'trinity_http_request_duration_seconds_bucket{le="+Inf",method="POST",path="/memories"}' in text
    assert 'trinity_http_request_duration_seconds_count{method="POST",path="/memories"}' in text
    assert "trinity_memories_total 7" in text

    # /metrics itself is never counted (no scrape feedback loop).
    assert 'path="/metrics"' not in text


def test_rate_limit_env_toggle_off_disables_limiting(monkeypatch, client):
    """(d) TRINITY_RATE_LIMIT_ENABLED=off disables limiting; back on resumes."""
    monkeypatch.setenv("TRINITY_RATE_LIMIT_RATE", "1")
    monkeypatch.setenv("TRINITY_RATE_LIMIT_BURST", "2")
    server.reconfigure_rate_limiter()

    # Kill switch off → unlimited writes.
    monkeypatch.setenv("TRINITY_RATE_LIMIT_ENABLED", "off")
    statuses = [_post_memory(client).status_code for _ in range(5)]
    assert statuses == [200] * 5

    # Back on → burst enforcement returns.
    monkeypatch.setenv("TRINITY_RATE_LIMIT_ENABLED", "on")
    server.reconfigure_rate_limiter()
    statuses = [_post_memory(client).status_code for _ in range(5)]
    assert statuses[:2] == [200, 200]
    assert statuses[2:] == [429, 429, 429]


def test_rate_limit_env_defaults_and_predicate(monkeypatch):
    """Defaults are enabled/60/120 and only write endpoints are limited."""
    monkeypatch.delenv("TRINITY_RATE_LIMIT_ENABLED", raising=False)
    monkeypatch.delenv("TRINITY_RATE_LIMIT_RATE", raising=False)
    monkeypatch.delenv("TRINITY_RATE_LIMIT_BURST", raising=False)
    assert rate_limit_enabled() is True
    assert rate_limit_rate() == 60
    assert rate_limit_burst() == 120

    # Limited: POST/PUT/DELETE on /memories, /memory/*, /agents/*.
    assert is_rate_limited_request("/memories", "POST")
    assert is_rate_limited_request("/memories/session", "POST")
    assert is_rate_limited_request("/memories/abc/touch", "POST")
    assert is_rate_limited_request("/agents/register", "POST")
    assert is_rate_limited_request("/agents/memory/write", "PUT")
    assert is_rate_limited_request("/agents/register", "DELETE")
    # Not limited: reads, other routes, /metrics,
    # and /memory/search/* (read-only search endpoints even though they POST).
    assert not is_rate_limited_request("/memory/search/hybrid", "POST")
    assert not is_rate_limited_request("/memory/search/cross-modal", "POST")
    assert not is_rate_limited_request("/memories", "GET")
    assert not is_rate_limited_request("/memories/abc", "HEAD")
    assert not is_rate_limited_request("/agents/memory/search", "GET")
    assert not is_rate_limited_request("/metrics", "POST")
    assert not is_rate_limited_request("/health", "POST")
    assert not is_rate_limited_request("/reason", "POST")
