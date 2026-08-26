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
    """Health check with component status.

    2026-08-24（R9 P0-1）：引擎（engine/adapter）状态纳入健康判定——
    此前仅看聚合池，API 在引擎 connect 失败（如 SQLite 写锁）时仍自报
    "ok"（健康假象）。现在 engine 不可用时 status=degraded 且报错误。
    """
    agg_ok = False
    sb_ok = False
    try:
        agg = get_aggregator()
        agg_ok = agg is not None and agg._pool is not None
        sb_ok = agg.second_brain_available
    except Exception:
        pass

    # ── engine 状态（R9 P0-1）：adapter 缺失或初始化失败 → degraded ──
    engine_ok = False
    engine_error: str = ""
    try:
        mem = get_memory()
        engine_ok = mem is not None and getattr(mem, "_adapter", None) is not None
        if not engine_ok:
            engine_error = str(getattr(mem, "_engine_error", "") or "no adapter")
    except Exception as exc:
        engine_error = f"{type(exc).__name__}: {exc}"

    healthy = agg_ok and engine_ok
    return {
        "status": "ok" if healthy else "degraded",
        "version": app.version,
        "uptime_seconds": round(time.time() - _app_start_time, 1),
        "components": {
            "aggregator": "healthy" if agg_ok else "unavailable",
            "api": "healthy",
            "second_brain": "available" if sb_ok else "unavailable",
            "engine": "healthy" if engine_ok else "degraded",
        },
        "engine_error": engine_error or None,
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

    # ── 2026-08-24（R9 后续 P1-③）：记忆可观测指标（对齐 2026 共识——
    # R@k 已失效，需命中率/写放大/成本运行指标）──────────────────
    try:
        # statistics() 已合并 _stats（queries_by_source/total_queries/last_query_at）
        qbs = stats.get("queries_by_source") or {}
        for src, cnt in sorted(qbs.items()):
            src_sanitized = src.replace(":", "_").replace("/", "_").replace("-", "_")
            lines.append(
                f'# HELP trinity_queries_by_source_total Queries per source: {src}'
            )
            lines.append("# TYPE trinity_queries_by_source_total counter")
            lines.append(f'trinity_queries_by_source_total{{source="{src_sanitized}"}} {int(cnt)}')

        # 写放大系数 = ingested / max(merged,1)（越高说明合并越少、写入越原始）
        ingested = int(stats.get("total_ingested", 0) or 0)
        merged = int(stats.get("total_merged", 0) or 0)
        write_amp = round(ingested / max(merged, 1), 3) if merged > 0 else 0.0
        lines.append("# HELP trinity_write_amplification Ingested-to-merged ratio (higher = less consolidation)")
        lines.append("# TYPE trinity_write_amplification gauge")
        lines.append(f"trinity_write_amplification {write_amp}")

        # 上次查询时间（0 = 从未查询——利用率监控）
        last_q = float(stats.get("last_query_at") or 0)
        lines.append("# HELP trinity_last_query_ts Unix ts of last pool query (0 = never)")
        lines.append("# TYPE trinity_last_query_ts gauge")
        lines.append(f"trinity_last_query_ts {int(last_q)}")

        # 语义缓存命中率（hybrid retriever 层，若可用）
        try:
            from trinity.retrieval.hybrid_retriever import _get_configured_cache
            cache = _get_configured_cache()
            if cache is not None:
                cs = cache.statistics()
                lines.append("# HELP trinity_semantic_cache_hit_rate_pct Semantic cache hit rate")
                lines.append("# TYPE trinity_semantic_cache_hit_rate_pct gauge")
                lines.append(f"trinity_semantic_cache_hit_rate_pct {cs.get('hit_rate_pct', 0)}")
                lines.append("# HELP trinity_semantic_cache_entries Semantic cache entries")
                lines.append("# TYPE trinity_semantic_cache_entries gauge")
                lines.append(f"trinity_semantic_cache_entries {int(cs.get('memory_entries', 0) or 0)}")
        except Exception:
            pass
    except Exception:
        pass  # 指标扩展失败不影响主指标

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
