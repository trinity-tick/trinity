"""Tests for SemanticCache backends and the retrieval cache wrapper (M3-2).

Covers:
  (a) SemanticCache redis backend set/get round-trip (skipped when the
      local Redis at 127.0.0.1:6379 is unreachable).
  (b) Memory backend LRU eviction and TTL expiry.
  (c) Retrieval wrapper: identical queries hit the cache on the second call.
  (d) TRINITY_CACHE_BACKEND=off keeps the original (uncached) behaviour.
"""

import os
import sys
import time

import pytest

# Ensure the project root is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.core.cache import SemanticCache
from trinity.retrieval.hybrid_retriever import (
    HybridRetriever,
    _get_configured_cache,
)

REDIS_URL = os.environ.get("TRINITY_REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_PREFIX = "trinity:test:cache:"


def _redis_reachable() -> bool:
    try:
        import redis
        # Local server is an old Redis 3.x Windows build: RESP2 required.
        client = redis.Redis.from_url(
            REDIS_URL, socket_connect_timeout=1, protocol=2
        )
        client.ping()
        return True
    except Exception:
        return False


REDIS_REACHABLE = _redis_reachable()


# ── Fixtures / helpers ─────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_cache_module():
    """Reset the module-level configured cache before/after each test."""
    import trinity.retrieval.hybrid_retriever as hr
    hr._cache_instance = None
    hr._cache_instance_config = None
    yield
    hr._cache_instance = None
    hr._cache_instance_config = None


def make_retriever(calls=None):
    """Build a HybridRetriever over lightweight mock sources.

    ``calls["vector"]`` counts how many times the vector source actually
    ran, so tests can prove the second identical query was cached.
    """
    if calls is None:
        calls = {"vector": 0}

    class _BM25:
        def search(self, query, top_k=10):
            return [("m_1", 0.9), ("m_4", 0.5)]

    class _Graph:
        def search_by_entity(self, query, top_k=10):
            return [{"memory_id": "m_2", "content": "entity hit", "graph_score": 0.8}]

        def get_neighbors(self, memory_id):
            return []

    def _vector_fn(query, top_k=10):
        calls["vector"] += 1
        return [{"memory_id": "m_1", "content": "vector hit", "score": 0.95}]

    retriever = HybridRetriever(
        bm25_index=_BM25(),
        graph_retriever=_Graph(),
        search_fn=_vector_fn,
    )
    return retriever, calls


# ── Key generation ─────────────────────────────────────────────────────

def test_make_text_key_sha256():
    """Text keys are stable SHA-256 digests, parameter-scoped."""
    cache = SemanticCache(backend="memory")

    k1 = cache.make_text_key("hello world")
    assert k1 == cache.make_text_key("hello world")
    assert len(k1) == 64  # SHA-256 hex digest

    k2 = cache.make_text_key("hello world", top_k=5, strategy="rrf")
    assert k2 == f"{k1}_k5_rrf"

    # Different queries must not collide.
    assert cache.make_text_key("hello world") != cache.make_text_key("hello world!")


# ── (a) Redis backend round-trip ───────────────────────────────────────

@pytest.mark.skipif(
    not REDIS_REACHABLE,
    reason=f"Redis at {REDIS_URL} not reachable; skipping redis backend test",
)
def test_redis_backend_set_get_roundtrip():
    """SemanticCache(backend='redis') set/get round-trips JSON payloads."""
    cache = SemanticCache(
        backend="redis",
        redis_url=REDIS_URL,
        redis_prefix=REDIS_PREFIX,
    )
    cache.clear()
    try:
        key = cache.make_text_key("capital of france", top_k=5, strategy="fusion")
        payload = {
            "results": [{"memory_id": "m1", "content": "Paris", "hybrid_score": 0.9}],
            "strategy": "fusion",
            "query": "capital of france",
            "breakdown": {"vector": 1, "bm25": 0, "unique_fused": 1},
        }

        assert cache.set(key, payload, ttl=60) is True
        assert cache.get(key) == payload

        stats = cache.statistics()
        assert stats["redis_connected"] is True
        assert stats["hits"] == 1
        assert stats["misses"] == 0

        # Unknown key is a miss.
        assert cache.get("no_such_key") is None
        assert cache.statistics()["misses"] == 1
    finally:
        cache.clear()


# ── (b) Memory backend LRU / TTL ───────────────────────────────────────

def test_memory_backend_lru_eviction():
    """At capacity, the least recently used entry is evicted."""
    cache = SemanticCache(backend="memory", max_size=5)
    for i in range(5):
        cache.set(f"k{i}", {"n": i})

    # Touch k0 → it becomes the most recently used.
    assert cache.get("k0") == {"n": 0}

    # Inserting k5 evicts the LRU entry (k1), not k0.
    cache.set("k5", {"n": 5})
    assert cache.get("k0") == {"n": 0}
    assert cache.get("k1") is None
    assert cache.get("k5") == {"n": 5}
    assert cache.statistics()["memory_entries"] == 5


def test_memory_backend_ttl_expiry():
    """Entries expire after their TTL elapses."""
    cache = SemanticCache(backend="memory", default_ttl=0.2)
    cache.set("tk", {"v": 1}, ttl=0.2)
    assert cache.get("tk") == {"v": 1}

    time.sleep(0.3)
    assert cache.get("tk") is None
    assert cache.statistics()["misses"] == 1


# ── (c) Retrieval wrapper: cache hit on second identical query ─────────

def test_hybrid_search_cache_hit(monkeypatch):
    """Same query twice: second call served from cache (memory backend)."""
    monkeypatch.setenv("TRINITY_CACHE_BACKEND", "memory")
    monkeypatch.setenv("TRINITY_CACHE_TTL", "300")

    retriever, calls = make_retriever()

    res1 = retriever.search("backup entity", top_k=10, strategy="fusion")
    res2 = retriever.search("backup entity", top_k=10, strategy="fusion")

    assert calls["vector"] == 1  # vector source ran exactly once
    assert res2 == res1

    cache = _get_configured_cache()
    assert cache is not None
    stats = cache.statistics()
    assert stats["misses"] == 1
    assert stats["hits"] == 1

    # A different query misses again and hits the sources.
    retriever.search("another query entirely", top_k=10)
    assert calls["vector"] == 2
    assert cache.statistics()["misses"] == 2


@pytest.mark.skipif(
    not REDIS_REACHABLE,
    reason=f"Redis at {REDIS_URL} not reachable; skipping redis wrapper test",
)
def test_hybrid_search_redis_cache_hit(monkeypatch):
    """Same query twice through the Redis backend (JSON round-trip)."""
    monkeypatch.setenv("TRINITY_CACHE_BACKEND", "redis")
    monkeypatch.setenv("TRINITY_REDIS_URL", REDIS_URL)
    monkeypatch.setenv("TRINITY_CACHE_TTL", "300")

    retriever, calls = make_retriever()
    cache = _get_configured_cache()
    assert cache is not None
    cache.clear()
    try:
        res1 = retriever.search("redis cached query", top_k=10)
        res2 = retriever.search("redis cached query", top_k=10)
        assert calls["vector"] == 1
        assert res2 == res1
        stats = cache.statistics()
        assert stats["hits"] >= 1
        assert stats["misses"] == 1
    finally:
        cache.clear()


# ── (d) TRINITY_CACHE_BACKEND=off keeps original behaviour ─────────────

def test_cache_off_passthrough(monkeypatch):
    """off: no caching, sources run every time, results identical."""
    monkeypatch.setenv("TRINITY_CACHE_BACKEND", "off")

    retriever, calls = make_retriever()

    r1 = retriever.search("query x", top_k=10)
    r2 = retriever.search("query x", top_k=10)

    assert calls["vector"] == 2  # uncached: both calls reached the sources
    assert r1 == r2
    assert _get_configured_cache() is None


def test_cache_default_memory(monkeypatch):
    """With the env var unset, caching defaults to the memory backend
    (2026-08-24, COMPARISON_VS_2026_SOTA_R7 P0-2: semantic cache is
    industry-standard latency reduction; memory backend is dependency-free)."""
    monkeypatch.delenv("TRINITY_CACHE_BACKEND", raising=False)
    monkeypatch.delenv("TRINITY_CACHE_TTL", raising=False)
    cache = _get_configured_cache()
    assert cache is not None
    assert cache.statistics()["backend"] == "memory"
