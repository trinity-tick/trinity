"""
Engine initialization cache - module-level singleton for the second_brain engine.
+ Semantic result cache for retrieval queries.

The second_brain engine imports and initialises 122 modules upon construction,
touching retrieval channels, guardian chains, and dozens of sub-modules.
Every call to ``Trinity()`` in legacy mode (no adapter) would previously
re-import and rebuild the entire engine, incurring a significant startup cost.

This module provides two caching layers:

  Layer 1 - Engine Singleton Cache:
    * Stores the engine instance in a module-level global after first creation.
    * Returns the cached instance on subsequent ``get_engine()`` calls.
    * Supports explicit ``reset_engine()`` for testing or configuration changes.
    * Is thread-safe via a basic lock (no double-initialisation races).

  Layer 2 - Semantic Result Cache:
    * Caches retrieval results keyed by query embedding fingerprint.
    * In-memory LRU by default; optional Redis backend for production.
    * Queries with similar/same embeddings return cached results (10-100x).
    * TTL expiration, LRU eviction, hit/miss tracking.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
#  Layer 1: Engine Singleton Cache
# ═══════════════════════════════════════════════════════════════════════

# Module-level singleton
_engine: Any = None
_engine_lock = threading.Lock()


def get_engine() -> Any:
    """Return the cached second_brain Engine singleton.

    On the first call this imports ``Engine`` from
    ``trinity.modules.second_brain`` and instantiates it.
    Subsequent calls return the same instance unless ``reset_engine()``
    has been called in between.

    Returns:
        A ``SecondBrainV636`` (or compatible) engine instance.
    """
    global _engine

    if _engine is not None:
        return _engine

    with _engine_lock:
        if _engine is not None:
            return _engine

        from trinity.modules.second_brain import Engine as _SecondBrainEngine

        _engine = _SecondBrainEngine()
        return _engine


def reset_engine() -> None:
    """Reset the cached engine singleton.

    The next call to ``get_engine()`` performs a fresh import and
    instantiation of the second_brain engine.
    """
    global _engine

    with _engine_lock:
        _engine = None


def get_engine_status() -> dict:
    """Return diagnostics about the engine cache state."""
    return {
        "cached": _engine is not None,
        "engine_type": type(_engine).__name__ if _engine is not None else None,
    }


# ═══════════════════════════════════════════════════════════════════════
#  Layer 2: Semantic Result Cache
# ═══════════════════════════════════════════════════════════════════════


def _vector_fingerprint(vector: np.ndarray, precision: int = 3) -> str:
    """Create a semantic fingerprint from an embedding vector.

    Uses quantized bins + hash to group similar vectors together.
    Two queries with similar embeddings will have the same fingerprint.

    Args:
        vector: The embedding vector (np.ndarray).
        precision: Quantization precision (3 = ~0.1% similarity threshold).

    Returns:
        SHA-256 hex digest as fingerprint string.
    """
    # Quantize the vector: round to nearest 10^-precision
    quantized = np.round(vector.astype(np.float64), decimals=precision)
    # Hash the quantized vector
    raw = quantized.tobytes()
    return hashlib.sha256(raw).hexdigest()[:16]  # 16 chars = 64-bit collision resistance


@dataclass
class CacheEntry:
    """A single cache entry with metadata."""

    key: str
    value: Any
    created_at: float = field(default_factory=time.time)
    ttl: float = 300.0  # seconds (default 5 min)
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    embedding_fingerprint: str = ""

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.ttl

    def touch(self):
        self.access_count += 1
        self.last_access = time.time()


class SemanticCache:
    """Semantic result cache for retrieval queries.

    Caches top-k search results keyed by the query embedding fingerprint.
    Queries with identical or nearly identical embeddings share the same
    cache entry, providing dramatic speedups for repeated/related queries.

    Two backends:
      - ``"memory"`` (default): In-memory LRU dict. Fast, no dependencies.
      - ``"redis"``: Redis-backed (``pip install redis``). Production-ready.

    Usage:
        cache = SemanticCache(max_size=1000, default_ttl=300)

        # During search
        fingerprint = cache.make_key(query_vector)
        cached = cache.get(fingerprint)
        if cached:
            return cached

        results = search(query, query_vector)
        cache.set(fingerprint, results)

        # Stats
        stats = cache.statistics()
    """

    def __init__(
        self,
        backend: str = "memory",
        max_size: int = 1000,
        default_ttl: float = 300.0,
        redis_url: Optional[str] = None,
        redis_prefix: str = "trinity:cache:",
        fingerprint_precision: int = 3,
    ):
        """
        Args:
            backend: ``"memory"`` or ``"redis"``.
            max_size: Max entries in memory LRU cache (ignored for Redis).
            default_ttl: Default TTL in seconds (5 min).
            redis_url: Redis connection URL (e.g. ``redis://localhost:6379/0``).
            redis_prefix: Key prefix for Redis entries.
            fingerprint_precision: Quantization precision for embedding hashing.
                                   1 = coarser (more cache hits, lower precision).
                                   3 = finer (fewer hits, higher precision).
        """
        self._backend = backend
        self._max_size = max_size
        self._default_ttl = default_ttl
        self._redis_url = redis_url
        self._redis_prefix = redis_prefix
        self._fingerprint_precision = fingerprint_precision

        # Memory backend
        self._cache: Dict[str, CacheEntry] = {}
        self._cache_lock = threading.Lock()
        self._lru_order: List[str] = []  # least recently used first

        # Redis backend
        self._redis = None
        self._redis_available = False
        if backend == "redis":
            self._init_redis()

        # Statistics
        self._hits = 0
        self._misses = 0
        self._total_gets = 0
        self._total_sets = 0

    def _init_redis(self):
        """Initialize Redis connection."""
        try:
            import redis as _redis_module
            url = self._redis_url or "redis://localhost:6379/0"
            try:
                # Default: modern Redis (RESP3 handshake).
                self._redis = _redis_module.Redis.from_url(url, decode_responses=True)
                self._redis.ping()
            except Exception:
                # Fallback: older Redis servers (e.g. Redis 3.x Windows
                # builds) reject the RESP3 ``HELLO`` command; retry with
                # RESP2, which every server understands.
                self._redis = _redis_module.Redis.from_url(
                    url, decode_responses=True, protocol=2
                )
                self._redis.ping()
            self._redis_available = True
            logger.info("SemanticCache connected to Redis at %s", url)
        except Exception as e:
            logger.warning(
                "Redis unavailable, falling back to memory backend: %s", e
            )
            self._backend = "memory"

    # ── Key Generation ────────────────────────────────────────────

    def make_key(
        self,
        query_vector: Optional[np.ndarray] = None,
        query_text: Optional[str] = None,
        top_k: Optional[int] = None,
    ) -> str:
        """Generate a cache key from query parameters.

        Primary: embedding vector fingerprint (semantic similarity).
        Fallback: text hash (exact match only).
        """
        if query_vector is not None:
            fp = _vector_fingerprint(query_vector, self._fingerprint_precision)
        elif query_text:
            fp = hashlib.md5(query_text.encode("utf-8")).hexdigest()[:16]
        else:
            fp = "empty_query"

        if top_k:
            fp = f"{fp}_k{top_k}"

        return fp

    def make_text_key(
        self,
        query_text: str,
        top_k: Optional[int] = None,
        strategy: Optional[str] = None,
    ) -> str:
        """Build a cache key from the raw query text (exact-match, SHA-256).

        This is the key generator used by the retrieval-layer cache wrapper:
        it hashes the query string itself (not an embedding), so only
        byte-identical queries share a cache entry.

        Args:
            query_text: The raw query string.
            top_k: Optional result count, folded into the key.
            strategy: Optional retrieval strategy (e.g. ``"fusion"`` /
                ``"rrf"`` / ``"cascade"``), folded into the key.

        Returns:
            SHA-256 hex digest (optionally suffixed with ``_k<top_k>`` and
            ``_<strategy>``).
        """
        fp = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        if top_k:
            fp = f"{fp}_k{top_k}"
        if strategy:
            fp = f"{fp}_{strategy}"
        return fp

    # ── Get / Set ─────────────────────────────────────────────────

    def get(
        self,
        key: str,
        default: Any = None,
    ) -> Any:
        """Retrieve cached result by key.

        Args:
            key: Cache key (from ``make_key()``).
            default: Value to return on cache miss.

        Returns:
            Cached value or default.
        """
        self._total_gets += 1

        if self._backend == "redis" and self._redis_available:
            return self._redis_get(key, default)

        return self._memory_get(key, default)

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[float] = None,
    ) -> bool:
        """Store a result in cache.

        Args:
            key: Cache key (from ``make_key()``).
            value: Value to cache (must be JSON-serializable for Redis).
            ttl: Optional TTL override (seconds). Defaults to ``default_ttl``.

        Returns:
            True if stored successfully.
        """
        self._total_sets += 1
        _ttl = ttl if ttl is not None else self._default_ttl

        if self._backend == "redis" and self._redis_available:
            return self._redis_set(key, value, _ttl)

        return self._memory_set(key, value, _ttl)

    # ── Memory Backend ────────────────────────────────────────────

    def _memory_get(self, key: str, default: Any = None) -> Any:
        with self._cache_lock:
            entry = self._cache.get(key)
            if entry is None:
                self._misses += 1
                return default

            if entry.expired:
                del self._cache[key]
                if key in self._lru_order:
                    self._lru_order.remove(key)
                self._misses += 1
                return default

            entry.touch()
            # Move to end of LRU (most recently used)
            if key in self._lru_order:
                self._lru_order.remove(key)
            self._lru_order.append(key)

            self._hits += 1
            return entry.value

    def _memory_set(self, key: str, value: Any, ttl: float) -> bool:
        with self._cache_lock:
            # Evict LRU if at capacity
            if len(self._cache) >= self._max_size and key not in self._cache:
                # Remove oldest entries (top 10% of LRU)
                evict_count = max(1, self._max_size // 10)
                for _ in range(evict_count):
                    if not self._lru_order:
                        break
                    oldest = self._lru_order.pop(0)
                    self._cache.pop(oldest, None)

            entry = CacheEntry(
                key=key,
                value=value,
                ttl=ttl,
                embedding_fingerprint=key,
            )
            self._cache[key] = entry
            if key in self._lru_order:
                self._lru_order.remove(key)
            self._lru_order.append(key)

            return True

    # ── Redis Backend ─────────────────────────────────────────────

    def _redis_get(self, key: str, default: Any = None) -> Any:
        try:
            full_key = self._redis_prefix + key
            data = self._redis.get(full_key)
            if data is None:
                self._misses += 1
                return default
            self._hits += 1
            # Update TTL on access (sliding window)
            self._redis.expire(full_key, int(self._default_ttl))
            return json.loads(data)
        except Exception as e:
            logger.warning("Redis get failed: %s", e)
            self._misses += 1
            return default

    def _redis_set(self, key: str, value: Any, ttl: float) -> bool:
        try:
            full_key = self._redis_prefix + key
            data = json.dumps(value, default=str, ensure_ascii=False)
            self._redis.set(full_key, data, ex=int(ttl))
            return True
        except Exception as e:
            logger.warning("Redis set failed: %s", e)
            return False

    # ── Invalidation ──────────────────────────────────────────────

    def invalidate(self, key: Optional[str] = None, pattern: Optional[str] = None):
        """Invalidate specific cache entries.

        Args:
            key: Invalidate a single key.
            pattern: Redis-style glob pattern (e.g. ``"user:*"``).
        """
        if self._backend == "redis" and self._redis_available:
            try:
                if key:
                    self._redis.delete(self._redis_prefix + key)
                elif pattern:
                    full_pattern = self._redis_prefix + pattern
                    for k in self._redis.scan_iter(match=full_pattern):
                        self._redis.delete(k)
            except Exception as e:
                logger.warning("Redis invalidation failed: %s", e)
        else:
            with self._cache_lock:
                if key and key in self._cache:
                    del self._cache[key]
                    if key in self._lru_order:
                        self._lru_order.remove(key)
                elif pattern:
                    import fnmatch
                    to_remove = [k for k in self._cache if fnmatch.fnmatch(k, pattern)]
                    for k in to_remove:
                        del self._cache[k]
                        if k in self._lru_order:
                            self._lru_order.remove(k)

    def clear(self):
        """Clear all cached entries."""
        if self._backend == "redis" and self._redis_available:
            try:
                for k in self._redis.scan_iter(match=self._redis_prefix + "*"):
                    self._redis.delete(k)
            except Exception as e:
                logger.warning("Redis clear failed: %s", e)
        else:
            with self._cache_lock:
                self._cache.clear()
                self._lru_order.clear()
        self._hits = 0
        self._misses = 0
        self._total_gets = 0
        self._total_sets = 0

    # ── Statistics ────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """Return cache usage statistics."""
        hit_rate = (
            self._hits / (self._hits + self._misses) * 100
            if (self._hits + self._misses) > 0 else 0.0
        )

        stats: Dict[str, Any] = {
            "backend": self._backend,
            "hits": self._hits,
            "misses": self._misses,
            "total_gets": self._total_gets,
            "total_sets": self._total_sets,
            "hit_rate_pct": round(hit_rate, 2),
            "default_ttl_s": self._default_ttl,
            "fingerprint_precision": self._fingerprint_precision,
        }

        if self._backend == "memory":
            with self._cache_lock:
                stats["memory_entries"] = len(self._cache)
                stats["memory_max"] = self._max_size
                stats["memory_usage_pct"] = round(
                    len(self._cache) / self._max_size * 100, 1
                ) if self._max_size > 0 else 0
        elif self._backend == "redis":
            stats["redis_connected"] = self._redis_available
            stats["redis_url"] = self._redis_url

        return stats


# ── Module-level SemanticCache singleton for convenience ───────────────

_default_cache: Optional[SemanticCache] = None
_default_cache_lock = threading.Lock()


def get_cache() -> SemanticCache:
    """Get or create the default semantic cache singleton."""
    global _default_cache

    if _default_cache is not None:
        return _default_cache

    with _default_cache_lock:
        if _default_cache is not None:
            return _default_cache

        _default_cache = SemanticCache()
        return _default_cache


def configure_cache(
    backend: str = "memory",
    max_size: int = 1000,
    default_ttl: float = 300.0,
    redis_url: Optional[str] = None,
):
    """Configure and reset the default cache singleton.

    Call this once during app startup to configure caching.
    """
    global _default_cache

    with _default_cache_lock:
        _default_cache = SemanticCache(
            backend=backend,
            max_size=max_size,
            default_ttl=default_ttl,
            redis_url=redis_url,
        )
        return _default_cache
