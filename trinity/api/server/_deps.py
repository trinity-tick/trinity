#!/usr/bin/env python3
"""
Trinity REST API Server — shared runtime state, helpers and HTTP middleware.

Extracted from the former trinity/api/server.py monolith (v8.0.0+).

This module owns ALL module-level mutable state and the functions that use
it (so globals resolve in one place), plus the four @app.middleware("http")
handlers (defined here WITHOUT decorators; server/__init__.py registers them
on the app in the original order so the middleware stack is identical).

It must NOT import from trinity.api.server (no circular imports); the
_live_memory / _live_aggregator helpers resolve get_memory/get_aggregator
through the server package at CALL time so test monkeypatching of
trinity.api.server.get_memory / get_aggregator keeps working exactly
as it did on the monolith module globals.
"""

import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

from trinity import Trinity
from trinity.agents import MemoryAggregator, create_aggregator
from trinity.api.middleware import (
    get_metrics,
    is_rate_limited_request,
    metrics_dispatch,
    rate_limit_burst,
    rate_limit_enabled,
    rate_limit_rate,
)

try:
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import JSONResponse
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    FastAPI = object


logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Rate Limiter (simple token bucket)
# ═══════════════════════════════════════════════════════════════════════════
class TokenBucket:
    def __init__(self, rate: int = 60, burst: int = 120):
        self.rate = rate
        self.burst = burst
        self.tokens = float(burst)
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, n: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens >= n:
                self.tokens -= n
                return True
            return False

def _build_rate_limiter() -> TokenBucket:
    """Build a bucket from TRINITY_RATE_LIMIT_RATE / TRINITY_RATE_LIMIT_BURST."""
    return TokenBucket(rate=rate_limit_rate(), burst=rate_limit_burst())


_rate_limiter = _build_rate_limiter()


def reconfigure_rate_limiter() -> TokenBucket:
    """Re-read TRINITY_RATE_LIMIT_* env vars and rebuild the shared bucket.

    Resets the token count to a full burst. Used by tests for isolation and
    available for live reconfiguration without a restart.
    """
    global _rate_limiter
    _rate_limiter = _build_rate_limiter()
    return _rate_limiter


# ═══════════════════════════════════════════════════════════════════════════
# App Lifecycle
# ═══════════════════════════════════════════════════════════════════════════
_aggregator: Optional[MemoryAggregator] = None
_memory: Optional[Trinity] = None
_app_start_time: float = 0.0


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _app_start_time
    _app_start_time = time.time()
    _live_aggregator()  # pre-warm
    _startup_prewarm()  # 2026-08-15：启动期后台预热（BM25 构建 + 首次检索）
    yield
    # Shutdown: flush persistence
    global _aggregator
    if _aggregator is not None:
        _aggregator._save()


def _startup_prewarm() -> None:
    """启动期后台预热（2026-08-15 二轮压测修复）：把 ~2.7s 冷启动
    （BM25 后台构建 + embedding fit + jieba）从"首个请求"移到启动期。

    - get_memory() 触发 adapter 连接（jieba 后台预热）
    - 只触发 BM25 后台构建（_ensure_bm25_index），**不跑完整 search_hybrid**
      ——预热线程跑全链路检索会与首请求竞争写锁/GIL（实测首请求 16s）
    - 轮询等 _bm25_ready（上限 30s），不阻塞启动
    失败静默：即使预热失败，首个请求仍走惰性路径（行为与预热前一致）。
    """
    import threading as _th

    def _warm() -> None:
        try:
            mem = _live_memory()
            mem._ensure_bm25_index()  # 仅触发后台构建，返回即释放
            deadline = time.time() + 30
            while time.time() < deadline and not getattr(
                    mem, "_bm25_ready", False):
                time.sleep(0.2)
        except Exception:
            pass
        # 2026-09（EXECUTION 104.9）：嵌入引擎预热——向量通道冷启动 ~24s
        # （transformers import + tokenizer + ONNX session）移到启动期后台，
        # 首个向量查询不再卡 24s。TRINITY_PREWARM_EMBED=0 可关闭；失败静默
        # （惰性路径兜底，行为与预热前一致）。
        if os.environ.get("TRINITY_PREWARM_EMBED", "1") == "1":
            try:
                from trinity.core.client._helpers import _get_embedding_engine
                eng = _get_embedding_engine()
                if eng is not None:
                    eng.embed("warmup")
            except Exception:
                pass
        # 2026-09 (EXECUTION 123): jieba 词典预热——首个中文检索不再卡
        # 1.8s（词典构建是进程级一次性，从首请求移到启动期）。
        try:
            import jieba as _jb
            _jb.setLogLevel(60)
            _jb.cut("预热中文分词词典")
        except Exception:
            pass
        # 2026-09 (EXECUTION 124): reranker 预热——首查不再卡 2-10s
        # 2026-09-02（CE 修复后恢复默认开）：main() 已顺序 preload，此处后台加载 CE
        # 模型（缓存完整，~0.3s）；TRINITY_PREWARM_RERANK=0 可关。
        if os.environ.get("TRINITY_PREWARM_RERANK", "1") == "1":
            try:
                from trinity.vector_index.reranker import CrossEncoderReranker
                _rk = CrossEncoderReranker(model_name="chinese")
                _rk._load_model()  # 失败静默（降级链兜底）
            except Exception:
                pass

    _th.Thread(target=_warm, daemon=True, name="api-startup-prewarm").start()


def get_aggregator() -> MemoryAggregator:
    global _aggregator
    if _aggregator is None:
        _aggregator = create_aggregator(persist=True)
    return _aggregator


def get_memory() -> Trinity:
    global _memory
    if _memory is None:
        _memory = Trinity()
    return _memory


def _live_memory() -> Trinity:
    """Resolve get_memory() through the server package at call time.

    The monolith resolved get_memory as a module global, so
    monkeypatch.setattr(server, "get_memory", stub) was honored by every
    endpoint and by the lifespan. After the package split the endpoints and
    lifespan live in other modules; routing the lookup through the package
    attribute reproduces the same semantics (patched when patched, real
    otherwise).
    """
    from trinity.api.server import get_memory as _gm
    return _gm()


def _live_aggregator() -> MemoryAggregator:
    """Resolve get_aggregator() through the server package at call time
    (same rationale as _live_memory)."""
    from trinity.api.server import get_aggregator as _ga
    return _ga()


# TTL-cached aggregator statistics for /metrics (avoids rebuilding the
# pool distribution on every scrape). Refreshed at most once per TTL.
_mem_stats_cache: Dict[str, Any] = {"ts": 0.0, "stats": None}
_MEM_STATS_TTL = 5.0


# Static files directory (package moved server.py -> server/: one level up)
_static_dir = Path(__file__).parent.parent / "static"


# GraphQL schema (imported defensively; server/__init__.py mounts the router)
try:
    from trinity.api.graphql_schema import schema as _trinity_graphql_schema
except Exception:
    _trinity_graphql_schema = None


# ═══════════════════════════════════════════════════════════════════════════
# HTTP middleware handlers (bodies identical to the monolith; the
# @app.middleware("http") decorators were moved to server/__init__.py which
# registers them in the original order: global_error, rate_limit,
# request_logging, metrics — metrics last = outermost).
# ═══════════════════════════════════════════════════════════════════════════
async def global_error_handler(request: Request, call_next):
    """Catch-all error middleware —returns structured error JSON."""
    try:
        return await call_next(request)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "detail": str(exc),
                "path": request.url.path,
            },
        )


async def rate_limit_middleware(request: Request, call_next):
    """Token-bucket rate limiting on /memories, /memory/* and /agents/* write endpoints.

    - Only POST/PUT/DELETE are limited; read endpoints (GET/HEAD) pass through.
    - /metrics is exempt (no rate limiting, no counting loop).
    - Config via env: TRINITY_RATE_LIMIT_ENABLED (default on),
      TRINITY_RATE_LIMIT_RATE (default 60/s), TRINITY_RATE_LIMIT_BURST (default 120).
    - Denials return 429 {"error": "rate_limit_exceeded", "detail": ...} and are
      counted in trinity_rate_limit_denied_total{path}.
    """
    path = request.url.path
    if (
        rate_limit_enabled()
        and is_rate_limited_request(path, request.method)
        and not _rate_limiter.consume()
    ):
        get_metrics().inc(
            "trinity_rate_limit_denied_total",
            {"path": path},
        )
        return JSONResponse(
            status_code=429,
            content={
                "error": "rate_limit_exceeded",
                "detail": (
                    f"Too many write requests (limit {_rate_limiter.rate}/s, "
                    f"burst {_rate_limiter.burst}); retry later"
                ),
            },
        )
    return await call_next(request)


async def request_logging_middleware(request: Request, call_next):
    """Structured request logging + OpenTelemetry-compatible trace span."""
    from trinity.telemetry import get_tracer

    tracer = get_tracer()
    span = tracer.start_span("api.request", attributes={"method": request.method, "path": request.url.path})
    start = time.time()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception as exc:
        span.error(exc)
        raise
    finally:
        elapsed = (time.time() - start) * 1000
        print(f'[api] {request.method} {request.url.path} →{status} ({elapsed:.1f}ms)')
        span.set_attribute("status", status)
        span.set_attribute("elapsed_ms", round(elapsed, 1))
        span.ok()
        span.finish()
        tracer.end_span(span)


# Metrics middleware is registered last so it is the OUTERMOST layer:
# it wraps the whole chain and therefore also records rate-limit 429
# responses and the full end-to-end request duration.

async def metrics_middleware(request: Request, call_next):
    """Prometheus request metrics — /metrics itself is skipped (no scrape loop)."""
    return await metrics_dispatch(request, call_next)
