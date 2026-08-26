#!/usr/bin/env python3
"""
Trinity REST API Server -FastAPI-based (v8.0.0)
==================================================
Optimized for production HTTP Gateway deployment:
  - Pydantic request/response models
  - Global error handling middleware
  - CORS + GZip compression
  - Rate limiting (token bucket)
  - Semantic search via embeddings
  - Batch endpoints
  - Structured health checks

This is the package assembly module (formerly trinity/api/server.py, split
into trinity/api/server/): models live in _models.py, shared runtime state
and middleware in _deps.py, and the endpoints are grouped by domain in the
_routers_*.py modules. The public surface is unchanged:

  - uvicorn trinity.api.server:app
  - python -m trinity.api.server --port 8001
  - from trinity.api import server; TestClient(server.app)
  - pyproject.toml: trinity-api = "trinity.api.server:main"
"""

import sys, os, json, time, argparse, threading
import logging
logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager

from trinity import Trinity
from trinity.agents import MemoryAggregator, create_aggregator
from trinity.api.rbac_middleware import RBACMiddleware, get_rbac_engine
from trinity.api.middleware import (
    get_metrics,
    is_rate_limited_request,
    metrics_dispatch,
    rate_limit_burst,
    rate_limit_enabled,
    rate_limit_rate,
)
from trinity.market import (
    OrderBook,
    TrustExchange,
    ReputationEngine,
    ReputationScore,
    create_asset,
    verify_asset_integrity,
    get_asset_metadata,
    estimate_value,
    get_market_price,
)

# FastAPI imports
try:
    from fastapi import FastAPI, HTTPException, Query, Body, Request
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.middleware.gzip import GZipMiddleware
    from pydantic import BaseModel, Field, field_validator
    import uvicorn
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    FastAPI = object

# Re-export all Pydantic models (import styles like
# "from trinity.api.server import MemoryWriteRequest" keep working).
from ._models import (  # noqa: E402
    RegisterRequest, MemoryWriteRequest, BulkMemoryWriteRequest, BulkWriteResult, BridgeInjectRequest, IdentityAnchorRequest, IdentityReconstructRequest, IdentityBundleRequest, AuditRunRequest, ConstitutionUpdateRequest, AgentCardRequest, A2ATaskRequest, A2ATaskUpdateRequest, A2AMessageRequest, CompressRequest, CompressStatsRequest, CompressRestoreRequest, MarvisAgentRegisterRequest, MarvisDispatchRequest, MarvisSnapshotResponse, MemoryResponse, MemorySearchResponse, HealthResponse, IdentityProfileResponse, IdentityBundleResponse, AuditTrailResponse, AuditReplayResponse, AuditIntegrityResponse, AuditSummaryResponse, ConstitutionResponse, ConstitutionUpdateResponse, DCSAMetricsResponse, A2AAgentListResponse, A2ACardResponse, A2ATaskResponse, A2AMessageResponse, MarvisSnapshotFullResponse, MarvisTrustResponse, SecuritySignRequest, SecurityVerifyRequest, CapabilityAuthorizeRequest, CapabilityRevokeRequest, TaskGrantRequest, HybridSearchRequest, CrossModalSearchRequest, ImageByTextRequest, TextByImageRequest, RouteRequest, RouteFeedbackRequest, MemoryAccessRequest, FeedbackRequest, MarketListRequest, MarketDelistRequest, MarketBuyRequest, MarketEndorseRequest, MarketReportRequest, MarketPriceRequest,
)

# Shared runtime state, helpers and HTTP middleware handlers.
from ._deps import (  # noqa: E402
    _startup_prewarm,
    _static_dir,
    get_aggregator,
    get_memory,
    global_error_handler,
    metrics_middleware,
    rate_limit_middleware,
    reconfigure_rate_limiter,
    request_logging_middleware,
    TokenBucket,
)
from ._deps import lifespan as _deps_lifespan


@asynccontextmanager
async def lifespan(app):
    """Wrap the shared lifespan to keep _routers_health._app_start_time in
    sync with the value _deps.lifespan sets at startup (the monolith shared
    this global across the whole module; the package split it across
    _deps.py and _routers_health.py). Imports are lazy to avoid circular
    imports at definition time; everything is fully imported by startup."""
    from . import _routers_health as _health_module
    from . import _deps as _shared

    async with _deps_lifespan(app) as _:
        _health_module._app_start_time = _shared._app_start_time
        yield

__all__ = [
    "app",
    "main",
    "get_memory",
    "get_aggregator",
    "reconfigure_rate_limiter",
    "lifespan",
    "_startup_prewarm",
    "TokenBucket",
    "RegisterRequest",
    "MemoryWriteRequest",
    "BulkMemoryWriteRequest",
    "BulkWriteResult",
    "BridgeInjectRequest",
    "IdentityAnchorRequest",
    "IdentityReconstructRequest",
    "IdentityBundleRequest",
    "AuditRunRequest",
    "ConstitutionUpdateRequest",
    "AgentCardRequest",
    "A2ATaskRequest",
    "A2ATaskUpdateRequest",
    "A2AMessageRequest",
    "CompressRequest",
    "CompressStatsRequest",
    "CompressRestoreRequest",
    "MarvisAgentRegisterRequest",
    "MarvisDispatchRequest",
    "MarvisSnapshotResponse",
    "MemoryResponse",
    "MemorySearchResponse",
    "HealthResponse",
    "IdentityProfileResponse",
    "IdentityBundleResponse",
    "AuditTrailResponse",
    "AuditReplayResponse",
    "AuditIntegrityResponse",
    "AuditSummaryResponse",
    "ConstitutionResponse",
    "ConstitutionUpdateResponse",
    "DCSAMetricsResponse",
    "A2AAgentListResponse",
    "A2ACardResponse",
    "A2ATaskResponse",
    "A2AMessageResponse",
    "MarvisSnapshotFullResponse",
    "MarvisTrustResponse",
    "SecuritySignRequest",
    "SecurityVerifyRequest",
    "CapabilityAuthorizeRequest",
    "CapabilityRevokeRequest",
    "TaskGrantRequest",
    "HybridSearchRequest",
    "CrossModalSearchRequest",
    "ImageByTextRequest",
    "TextByImageRequest",
    "RouteRequest",
    "RouteFeedbackRequest",
    "MemoryAccessRequest",
    "FeedbackRequest",
    "MarketListRequest",
    "MarketDelistRequest",
    "MarketBuyRequest",
    "MarketEndorseRequest",
    "MarketReportRequest",
    "MarketPriceRequest"
]

# ═══════════════════════════════════════════════════════════════════════════
# FastAPI App
# ═══════════════════════════════════════════════════════════════════════════
app = FastAPI(
    title="Trinity Memory OS",
    description="""Trinity Memory OS —Triune Architecture for AGI Long-Term Memory (v8.0.0)

## 三层架构
- **Memory Engine**: 记忆存储、检索、嵌入、向量搜索- **Identity Layer**: 多锚点身份管理与重建
- **Guardian Layer**: DCSA-EJP 双循环宪法自审计

## 模块
- **A2A Protocol**: Google A2A v0.3 跨Agent 通信
- **Marvis Adapter**: Marvis 生态Agent 联邦管理
- **Agent Memory Gateway**: 高质量共享记忆聚合池

## 端点分组
| 分组 | 端点数| 说明 |
|:---|:---:|:---|
| 记忆引擎 | 5 | 存储、搜索、版本、角色|
| 身份管理 | 7 | 锚点注册、画像、漂移检测|
| DCSA 审计 | 7 | 审计轨迹、回放、宪法|
| A2A 协议 | 10 | Agent 注册、任务、消息|
| Marvis 适配器| 4 | 注册、调度、快照、信任|
""",
    version="8.2.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=512)

# P0.6: RBAC access control middleware (multi-scope ACL)
app.add_middleware(RBACMiddleware)

# The four @app.middleware("http") handlers live in _deps (bodies unchanged);
# register them here in the original order so the middleware stack is
# identical: global_error (innermost) -> rate_limit -> request_logging ->
# metrics (outermost, wraps the whole chain and records 429s).
app.middleware("http")(global_error_handler)
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(request_logging_middleware)
app.middleware("http")(metrics_middleware)

# Domain routers - included in the same relative order as the original
# monolith route groups. Within each router the original route order is
# preserved, so static paths (e.g. /memories/stats, /memories/search) are
# registered before parameterized ones (/memories/{memory_id}) exactly as
# before; no route is shadowed.
from ._routers_health import router as health_router  # noqa: E402
from ._routers_memories import router as memories_router  # noqa: E402
from ._routers_search import router as search_router  # noqa: E402
from ._routers_agents import router as agents_router  # noqa: E402
from ._routers_audit import router as audit_router  # noqa: E402
from ._routers_identity import router as identity_router  # noqa: E402
from ._routers_a2a import router as a2a_router  # noqa: E402
from ._routers_marvis import router as marvis_router  # noqa: E402
from ._routers_compress import router as compress_router  # noqa: E402
from ._routers_market import router as market_router  # noqa: E402
from ._routers_evolution import router as evolution_router  # noqa: E402
from ._routers_structure import router as structure_router  # noqa: E402
from ._routers_offload import router as offload_router  # noqa: E402
from ._routers_explain import router as explain_router  # noqa: E402
from ._routers_persona import router as persona_router  # noqa: E402
from ._routers_audit_purge import router as audit_purge_router  # noqa: E402
from ._routers_receipt import router as receipt_router  # noqa: E402  # 2026-08-24 P1-④ 可证明记忆回执

def _register_router_routes(router) -> None:
    """Register an APIRouter's routes directly on the app router (flattened).

    This FastAPI/Starlette version's include_router() wraps the included
    router in a lazy _IncludedRouter object instead of appending the routes,
    which changes len(app.routes) and OpenAPI ordering. The monolith used
    @app.* decorators, which append plain APIRoute objects to
    app.router.routes; flattening reproduces that exactly (same route
    objects, same registration order, no route shadowing).
    """
    for route in router.routes:
        app.router.routes.append(route)
    app.router._mark_routes_changed()


_register_router_routes(health_router)
_register_router_routes(memories_router)
_register_router_routes(search_router)
_register_router_routes(agents_router)

# Static files
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

_register_router_routes(audit_router)
_register_router_routes(identity_router)
_register_router_routes(a2a_router)
_register_router_routes(marvis_router)
_register_router_routes(compress_router)
_register_router_routes(market_router)
_register_router_routes(evolution_router)

# ═══════════════════════════════════════════════════════════════════════════
# GraphQL（strawberry）— 此前 schema 存在但从未挂载，这里接入 FastAPI
# ═══════════════════════════════════════════════════════════════════════════
try:
    from strawberry.fastapi import GraphQLRouter
    from ._deps import _trinity_graphql_schema
    app.include_router(GraphQLRouter(_trinity_graphql_schema), prefix="/graphql")
    logger.info("GraphQL router mounted at /graphql")
except Exception as _gql_err:  # pragma: no cover — 缺依赖时仅降级不阻断
    logger.warning("GraphQL router not mounted: %s", _gql_err)


_register_router_routes(structure_router)
_register_router_routes(offload_router)
_register_router_routes(explain_router)
_register_router_routes(persona_router)
_register_router_routes(audit_purge_router)
_register_router_routes(receipt_router)  # 2026-08-24 P1-④


# ═══════════════════════════════════════════════════════════════════════════
# OpenAPI 规范 + 自动化统计（Budibase 借鉴 Phase 3，2026-08-26）
# ═══════════════════════════════════════════════════════════════════════════
# 注：/openapi.json 由 FastAPI 内置自动生成（147 paths 全量）；本端点提供
# 增强版 OpenAPI 文档（中文描述 + view/visibility/automation 参数说明）。
@app.get("/api/openapi.json", include_in_schema=False)
async def api_openapi_json(request: Request):
    from trinity.api.openapi_spec import build_spec
    try:
        base = str(request.base_url).rstrip("/")
    except Exception:
        base = "http://127.0.0.1:8001"
    return JSONResponse(build_spec(server_url=base))


@app.get("/automation/stats", include_in_schema=False)
async def automation_stats():
    """自动化引擎统计（TRINITY_AUTOMATION=on 时生效；默认关闭返回空统计）。"""
    try:
        from trinity.automation import get_engine
        stats = get_engine().stats()
        stats["enabled"] = get_engine().enabled()
        return stats
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# 进化治理对外可见化（价值兑现路径 1，2026-08-26）：agent 可感知自身进化状态
@app.get("/evolution/status", include_in_schema=False)
async def evolution_status():
    """进化治理全景（agent 可感知）：目标/eval 断言/技能/进化周期/基准指标。"""
    try:
        from trinity.evolution.goals import goal_list, default_metrics
        from trinity.eval.runner import DEFAULT_TASKS
        from trinity.skills import list_skills
        from trinity.evolution import MetaEvolution
        goals = goal_list()
        metrics = default_metrics()
        evo = MetaEvolution()
        diag = {}
        try:
            diag = evo.diagnostics()
        except Exception:
            pass
        return {
            "goals": {
                "total": len(goals),
                "complete": sum(1 for g in goals if g.get("phase") == "complete"),
                "active": sum(1 for g in goals if g.get("phase") == "active"),
                "blocked": sum(1 for g in goals if g.get("phase") == "blocked"),
                "items": [{"id": g["goal_id"][:16], "phase": g.get("phase"),
                           "last_metric": g.get("last_metric"),
                           "objective": g["objective"][:60]} for g in goals],
            },
            "eval": {
                "total_tasks": len(DEFAULT_TASKS),
                "tasks": [t["name"] for t in DEFAULT_TASKS],
            },
            "skills": [s["name"] for s in list_skills()],
            "evolution": {
                "total_cycles": diag.get("total_cycles", 0),
                "state_file": getattr(evo, "state_path", ""),
            },
            "metrics": metrics,
            "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
        }
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# 知识层（Context7 借鉴 Phase 1-2，2026-08-26）：源注册表 + 独立知识检索
@app.get("/knowledge/sources", include_in_schema=False)
async def knowledge_sources():
    """知识源注册表（freshness/coverage/usage/health）。"""
    try:
        from trinity.knowledge import sources
        return sources()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/knowledge/search", include_in_schema=False)
async def knowledge_search(q: str = "", source: str = "", top_k: int = 10):
    """独立知识检索（doc 层；源过滤 + 健康度元数据）。"""
    try:
        from trinity.knowledge import knowledge_search as _ks
        return _ks(query=q, source=source or None, top_k=top_k)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# 技能运行时（DSH 借鉴 Phase 3，2026-08-26）：data/skills 注册表
@app.get("/skills", include_in_schema=False)
async def skills_list():
    try:
        from trinity.skills import list_skills
        return {"skills": list_skills()}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/skills/{name}", include_in_schema=False)
async def skills_get(name: str):
    try:
        from trinity.skills import load_skill
        skill = load_skill(name)
        if skill is None:
            return JSONResponse({"error": "skill not found"}, status_code=404)
        return skill
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# 目标引擎（DSH 借鉴 Phase 1，2026-08-26）：目标驱动自进化
@app.get("/goals", include_in_schema=False)
async def goals_list(phase: str = ""):
    """目标列表（phase 过滤：active/paused/blocked/complete）。"""
    try:
        from trinity.evolution.goals import goal_list
        return {"goals": goal_list(phase or None)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/goals", include_in_schema=False)
async def goals_create(request: Request):
    """创建目标：{objective, acceptance?: {metric,op,value}, max_rounds?: int}。"""
    try:
        from trinity.evolution.goals import goal_create
        body = await request.json()
        objective = body.get("objective") or ""
        if not objective:
            return JSONResponse({"error": "objective required"}, status_code=400)
        goal = goal_create(objective,
                           acceptance=body.get("acceptance"),
                           max_rounds=body.get("max_rounds") or 10)
        return goal
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/goals/{goal_id}", include_in_schema=False)
async def goals_get(goal_id: str):
    try:
        from trinity.evolution.goals import goal_get
        goal = goal_get(goal_id)
        if goal is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        return goal
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/goals/{goal_id}/update", include_in_schema=False)
async def goals_update(goal_id: str, request: Request):
    """更新目标：{action: edit|pause|resume|complete|blocked, ...}。"""
    try:
        from trinity.evolution.goals import goal_update
        body = await request.json()
        goal = goal_update(goal_id,
                           action=body.get("action", "edit"),
                           objective=body.get("objective"),
                           acceptance=body.get("acceptance"),
                           max_rounds=body.get("max_rounds"),
                           blocked_reason=body.get("blocked_reason", ""))
        if goal is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        return goal
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# 自动化审批（Codex 借鉴 Phase 1，2026-08-26）：动作执行策略层
@app.get("/automation/pending", include_in_schema=False)
async def automation_pending():
    """待审批动作队列（approval always/on-failure 产生）。"""
    try:
        from trinity.automation import get_engine
        return {"enabled": get_engine().enabled(),
                "items": get_engine().pending_items()}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/automation/approve", include_in_schema=False)
async def automation_approve(request: Request):
    """审批动作：{pending_id, approve: bool}；approve=True 后台执行。"""
    try:
        from trinity.automation import get_engine
        body = await request.json()
        pid = body.get("pending_id") or ""
        approve = bool(body.get("approve", True))
        if not pid:
            return JSONResponse({"error": "pending_id required"}, status_code=400)
        ok = get_engine().approve(pid, approve=approve)
        if not ok:
            return JSONResponse({"error": "pending item not found"}, status_code=404)
        return {"pending_id": pid, "approve": approve,
                "status": "approved" if approve else "rejected"}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════
def _tls_uvicorn_kwargs() -> Dict[str, str]:
    """Pure env->uvicorn kwargs mapping for optional TLS.

    Both TRINITY_TLS_CERT and TRINITY_TLS_KEY must be set to enable TLS;
    otherwise no SSL kwargs are returned (behaviour identical to no TLS).
    """
    cert = os.environ.get("TRINITY_TLS_CERT", "").strip()
    key = os.environ.get("TRINITY_TLS_KEY", "").strip()
    if cert and key:
        return {"ssl_certfile": cert, "ssl_keyfile": key}
    return {}


def main():
    if not _HAS_FASTAPI:
        print("ERROR: fastapi not installed. Run: pip install trinity-memory[api]")
        sys.exit(1)
    parser = argparse.ArgumentParser(description="Trinity REST API Server")
    parser.add_argument("--port", type=int, default=8001, help="Port")
    parser.add_argument("--host", default="0.0.0.0", help="Host")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()
    tls_kwargs = _tls_uvicorn_kwargs()
    scheme = "https" if tls_kwargs else "http"
    print(f"Trinity API Server v{app.version} starting on {scheme}://{args.host}:{args.port}")
    print(f"Dashboard: {scheme}://{args.host}:{args.port}/")
    print(f"API docs:  {scheme}://{args.host}:{args.port}/docs")
    print(f"Metrics:   {scheme}://{args.host}:{args.port}/metrics")
    if tls_kwargs:
        logger.info("TLS enabled (%s)", os.environ.get("TRINITY_TLS_CERT"))
    uvicorn.run("trinity.api.server:app", host=args.host, port=args.port,
                reload=args.reload, **tls_kwargs)


if __name__ == "__main__":
    main()

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal error: {type(exc).__name__}: {str(exc)[:300]}"},
    )
