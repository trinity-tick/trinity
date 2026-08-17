#!/usr/bin/env python3
"""
Trinity REST API Server — health / metrics / diagnostics / dashboard routes.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, List

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from trinity.api.middleware import get_metrics
from trinity.api.server import app

from ._deps import (
    _live_aggregator as get_aggregator,
    _live_memory as get_memory,
    _mem_stats_cache,
    _MEM_STATS_TTL,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# The monolith shared this global with the lifespan (which sets it at
# startup). The lifespan now lives in _deps; server/__init__.py wraps it and
# syncs this module's copy at startup so /health reports real uptime.
_app_start_time: float = 0.0


@router.get("/health/self-test", tags=["Health"], summary="运行时全组件自检")
async def health_self_test():
    """执行全组件自检：HybridRouter / CapabilityRegistry / TaskManager / IdentityManager / Auditor。
    返回每个组件的pass/fail 状态与详细检查项。    """
    try:
        from trinity.self_test import run_all_self_tests
        results = run_all_self_tests()
        return results
    except Exception as e:
        return {
            "overall": "error",
            "summary": f"Self-test harness failed: {e}",
            "components": [],
        }


@router.get("/health", tags=["Health"], summary="健康检查")
async def health():
    """Health check with component status."""
    agg_ok = False
    sb_ok = False
    try:
        agg = get_aggregator()
        agg_ok = agg is not None and agg._pool is not None
        sb_ok = agg.second_brain_available
    except Exception:
        pass

    return {
        "status": "ok" if agg_ok else "degraded",
        "version": app.version,
        "uptime_seconds": round(time.time() - _app_start_time, 1),
        "components": {
            "aggregator": "healthy" if agg_ok else "unavailable",
            "api": "healthy",
            "second_brain": "available" if sb_ok else "unavailable",
        },
        "degradation": agg._degradation.statistics() if hasattr(agg, '_degradation') else {},
    }


@router.get("/metrics")
async def metrics():
    """Prometheus-format metrics endpoint (text/plain; exposition 0.0.4).

    Emits request counters/histograms from the metrics registry plus pool
    gauges/counters from the aggregator. /metrics itself is exempt from
    rate limiting and is not recorded in the request metrics (no scrape
    feedback loop). If the aggregator statistics cannot be obtained,
    trinity_memories_total falls back to 0 with an explanatory comment.
    """
    global _mem_stats_cache
    now = time.time()
    stats: Dict[str, Any] = {}
    mem_notes: List[str] = []
    if _mem_stats_cache["stats"] is not None and now - _mem_stats_cache["ts"] < _MEM_STATS_TTL:
        stats = _mem_stats_cache["stats"]
    else:
        try:
            stats = get_aggregator().statistics() or {}
        except Exception as exc:
            logger.warning("metrics: aggregator statistics unavailable: %s", exc)
            mem_notes.append(
                "# trinity_memories_total set to 0 (aggregator statistics unavailable)"
            )
        _mem_stats_cache = {"ts": now, "stats": stats}

    try:
        mem_total = int(stats.get("total_memories", 0) or 0)
    except (TypeError, ValueError):
        mem_total = 0
        mem_notes.append("# trinity_memories_total set to 0 (unparseable aggregator count)")

    lines = [
        "# HELP trinity_memories_total Total memories in shared pool",
        "# TYPE trinity_memories_total gauge",
        f"trinity_memories_total {mem_total}",
        *mem_notes,
        "# HELP trinity_ingested_total Total memories ingested",
        "# TYPE trinity_ingested_total counter",
        f"trinity_ingested_total {int(stats.get('total_ingested', 0) or 0)}",
        "# HELP trinity_merged_total Total merges by similarity",
        "# TYPE trinity_merged_total counter",
        f"trinity_merged_total {int(stats.get('total_merged', 0) or 0)}",
        "# HELP trinity_queries_total Total queries executed",
        "# TYPE trinity_queries_total counter",
        f"trinity_queries_total {int(stats.get('total_queries', 0) or 0)}",
    ]
    rendered = get_metrics().render().rstrip("\n")
    if rendered:
        lines.append(rendered)
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


@router.get("/diagnostics")
async def diagnostics():
    """Full system diagnostics (resilient)."""
    try:
        return get_memory().diagnostics()
    except Exception as e:
        # Fallback: return partial diagnostics from aggregator
        agg = get_aggregator()
        return {
            "status": "partial",
            "error": str(e),
            "aggregator": agg.statistics(),
            "trinity_version": "8.2.0",
        }




# ── Dashboard ──────────────────────────────────────────────────────────

async def dashboard():
    """Web dashboard."""
    html_path = Path(__file__).parent.parent / "static" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    html_path = Path(__file__).parent.parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Trinity Dashboard</h1><p>Static files not found.</p>")
