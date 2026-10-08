#!/usr/bin/env python3
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13, 顶部插入）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None
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
import asyncio  # 2026-10-06（G7）：存活心跳任务需要（此前本模块未导入）
from pathlib import Path
from typing import Any, Dict, List, Optional
from contextlib import asynccontextmanager, suppress

from trinity import Trinity
from trinity.version import __version__ as TRINITY_VERSION  # 2026-09-01: 版本单一源
from trinity.agents import MemoryAggregator, create_aggregator
from trinity.api.rbac_middleware import RBACMiddleware, get_rbac_engine
from trinity.api.auth import cors_origins, cors_allow_credentials, require_local_client  # 2026-09-30 安全修复：CORS 白名单单一来源 + 影子写端点本地限制
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
    from fastapi import FastAPI, HTTPException, Query, Body, Request, Depends
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
        # 2026-10-06（复评 G7）：**存活心跳**。
        # 动机：当天 12:56:22 本进程无痕消失 —— supervisor 见 `procs=0`，
        # 但 api.out.log 末条仍是正常 200、api.err.log 无任何崩溃栈 ⇒ 死因不可查。
        # 心跳文件把"无痕"变成"有痕"：最后一次心跳给出存活下界，并可区分
        # "卡死"（进程在但心跳停更）与"瞬死"（心跳正常后突然中断）。
        # 该任务**自身绝不抛异常**（内部全包 try/except），不会成为服务死因。
        _liveness_task = None
        try:
            from ._liveness import liveness_loop
            _liveness_task = asyncio.create_task(liveness_loop())
        except Exception as _lv_exc:  # noqa: BLE001 —— 诊断能力缺失不得阻止启动
            # 但**不得静默**：这里踩过一次真实的坑 —— `asyncio` 当时并未在本模块导入，
            # 于是 create_task 抛 NameError 被吞掉、心跳**静默不生效**（正是本轮要消灭的形态）。
            # 故降级为 WARNING 并带上异常类型，让"心跳没起来"一眼可见。
            logger.warning("liveness heartbeat NOT started (%s: %s) —— "
                           "本次进程的存活轨迹不会落盘，无痕死亡将再次不可诊断",
                           type(_lv_exc).__name__, _lv_exc)
        try:
            yield
        finally:
            if _liveness_task is not None:
                _liveness_task.cancel()
                # 2026-10-06（测试归因轮 T1，实测）：`cancel()` 之后 `await task` **必然**
                # 把 `CancelledError` 抛回这里（这是 asyncio 的正常收尾协议）。
                # 而 `CancelledError` 自 3.8 起继承 **BaseException**，**不是** Exception 的子类
                # ⇒ 原来写的 `suppress(Exception)` 一个字都拦不住：关停路径照样抛出，
                # 任何走到 lifespan 收尾的调用方（含 TestClient.__exit__）都拿到
                # `concurrent.futures._base.CancelledError`，9 条健康/端点判据一起判红。
                # 只吞"我刚刚取消掉的那个任务"的 CancelledError，其余异常照旧上抛。
                with suppress(asyncio.CancelledError):
                    await _liveness_task

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
def _docs_endpoints() -> Dict[str, Any]:
    """交互式文档与 OpenAPI 的暴露开关（2026-09-30 外部审计）。

    现场：`/docs`、`/redoc`、`/openapi.json` 三者**无条件公开**（实测均 200），
    等于把一个 186 路径 / 197 操作的完整接口地图交给任何能连到端口的人 ——
    对攻击者是"免费侦察"。服务虽只绑回环且已加跨站守卫，但**对外暴露的部署**
    （反代/容器端口映射）下这就是真实的信息泄露面。

    现提供显式开关，**默认 `on` 以保持既有行为不变**：
      * ``TRINITY_DOCS_ENABLED=off`` ⇒ 三个端点全部关闭（404）；
      * 其余取值（含未设）⇒ 维持 /docs + /redoc + /openapi.json。
    关闭时自定义的 ``/api/openapi.json``（agent 可读的受控规范）**照常提供**，
    因为它本来就是给程序消费的、路径面更小的那一个（实测 12 paths / 15 ops）。
    """
    raw = os.environ.get("TRINITY_DOCS_ENABLED", "on").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}


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
    version=TRINITY_VERSION,  # 2026-09-01: 跟随 version.py 单一源（原硬编码 8.2.0）
    lifespan=lifespan,
    **_docs_endpoints(),
)

# Middleware
#
# 2026-09-30 安全修复（外部审计确认的漏洞）：
#   旧配置 allow_origins=["*"] + allow_credentials=True 会被 Starlette 解释为
#   「把请求里的 Origin 原样回显，并附 Access-Control-Allow-Credentials: true」。
#   实测（带 Origin: https://evil.example.com 请求 /health）：
#       access-control-allow-origin: https://evil.example.com
#       access-control-allow-credentials: true
#   ⇒ 任意网页都能带凭证跨源**读取**本机 127.0.0.1:8001 的记忆 API（该 API 默认
#   无鉴权），包括全池导出端点。现改为：白名单来自 auth.cors_origins()（单一来源），
#   且**绝不在通配符下允许凭证**。
_cors_origins = cors_origins()
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=cors_allow_credentials(),
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(GZipMiddleware, minimum_size=512)

# P0.6: RBAC access control middleware (multi-scope ACL)
app.add_middleware(RBACMiddleware)

# 2026-09-30：全局跨站守卫 —— 对**非幂等方法**拒绝跨站浏览器请求。
# 本地脚本（无 Origin / Sec-Fetch-Site）照常通过，因此不打断 compliance/
# federation/dsh 插件等既有调用链；被拒的只有"用户浏览器里打开的网页"。
@app.middleware("http")
async def cross_site_guard(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = (request.headers.get("origin") or "").strip()
        site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        allowed = set(_cors_origins)
        if not (origin and origin in allowed):
            if site == "cross-site" or (origin and "*" not in allowed):
                from starlette.responses import JSONResponse as _JSONResponse
                return _JSONResponse(
                    {
                        "error": "cross_site_blocked",
                        "detail": (
                            "Cross-site browser requests are not allowed for "
                            f"{request.method} {request.url.path}."
                        ),
                    },
                    status_code=403,
                )
    return await call_next(request)

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
from ._routers_recall import router as recall_router  # noqa: E402  # 2026-09 重建式回忆（EXECUTION 105）
from ._routers_brain import router as brain_router  # noqa: E402  # 2026-09 工作记忆+元认知（EXECUTION 105.6）
from ._routers_cognition import router as cognition_router  # noqa: E402  # 2026-09 认知主体层（EXECUTION 105.21）
from ._routers_context import router as context_router  # noqa: E402  # 2026-09-09 融合上下文/轨迹/自检诊断（Mano-P 借鉴）
from ._routers_prov import router as prov_router  # noqa: E402  # 2026-09-11 审计面服务化：PROV-O/时间点/约束（只读）

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
_register_router_routes(recall_router)
_register_router_routes(brain_router)
_register_router_routes(cognition_router)
_register_router_routes(context_router)

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
_register_router_routes(prov_router)  # 2026-09-11 审计面服务化（只读）


# ═══════════════════════════════════════════════════════════════════════════
# OpenAPI 规范 + 自动化统计（Budibase 借鉴 Phase 3，2026-08-26）
# ═══════════════════════════════════════════════════════════════════════════
# 注：/openapi.json 由 FastAPI 内置自动生成（147 paths 全量）；本端点提供
# 增强版 OpenAPI 文档（中文描述 + view/visibility/automation 参数说明）。
@app.get("/api/openapi.json")
async def api_openapi_json(request: Request):
    from trinity.api.openapi_spec import build_spec
    try:
        base = str(request.base_url).rstrip("/")
    except Exception:
        base = "http://127.0.0.1:8001"
    return JSONResponse(build_spec(server_url=base))


@app.get("/automation/stats")
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
@app.get("/evolution/status")
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
            swallow(__name__, None)
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
@app.get("/knowledge/sources")
async def knowledge_sources():
    """知识源注册表（freshness/coverage/usage/health）。"""
    try:
        from trinity.knowledge import sources
        return sources()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/knowledge/search")
async def knowledge_search(q: str = "", source: str = "", top_k: int = 10):
    """独立知识检索（doc 层；源过滤 + 健康度元数据）。"""
    try:
        from trinity.knowledge import knowledge_search as _ks
        return _ks(query=q, source=source or None, top_k=top_k)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# 技能运行时（DSH 借鉴 Phase 3，2026-08-26）：data/skills 注册表
@app.get("/skills")
async def skills_list():
    try:
        from trinity.skills import list_skills
        return {"skills": list_skills()}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/skills/{name}")
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
@app.get("/goals")
async def goals_list(phase: str = ""):
    """目标列表（phase 过滤：active/paused/blocked/complete）。"""
    try:
        from trinity.evolution.goals import goal_list
        return {"goals": goal_list(phase or None)}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/goals", dependencies=[Depends(require_local_client)])
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


@app.get("/goals/{goal_id}")
async def goals_get(goal_id: str):
    try:
        from trinity.evolution.goals import goal_get
        goal = goal_get(goal_id)
        if goal is None:
            return JSONResponse({"error": "goal not found"}, status_code=404)
        return goal
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/goals/{goal_id}/update", dependencies=[Depends(require_local_client)])
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
@app.get("/automation/pending")
async def automation_pending():
    """待审批动作队列（approval always/on-failure 产生）。"""
    try:
        from trinity.automation import get_engine
        return {"enabled": get_engine().enabled(),
                "items": get_engine().pending_items()}
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/automation/approve", dependencies=[Depends(require_local_client)])
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
    # 2026-09-30 安全修复：默认监听回环。原默认值 "0.0.0.0" 会把一个**默认无鉴权**
    # 的记忆 API 暴露到整个局域网 —— 线上之所以安全只是因为启动命令显式传了
    # --host 127.0.0.1，默认值本身是危险的。需要对外暴露时显式传参或设
    # TRINITY_API_HOST，并同时配置 TRINITY_API_KEY + TRINITY_REQUIRE_API_KEY=1。
    parser.add_argument("--host", default=os.environ.get("TRINITY_API_HOST", "127.0.0.1"),
                        help="Host (default 127.0.0.1; set TRINITY_API_HOST to override)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()
    # 2026-09-02（CE 修复后恢复预加载）：模型缓存 tokenizer.json 已重下修复，
    # CE 在 transformers 4.51 下加载 0.3s / 重排 0.2s。顺序 preload（先于 onnx）
    # 保证 DLL 安全（st→pg→onnx 顺序实证安全）；失败静默降级 ollama。
    try:
        from trinity.vector_index.preload_reranker import preload, prewarm_model
        if preload():
            prewarm_model("chinese")
    except Exception:
        swallow(__name__, None)
    # ── 2026-09-30（外部审计 · 冷启动根因，**同步**预热）───────────────────
    #
    # 现场（cProfile，本机生产后端 TRINITY_EMBED_BACKEND=onnx，冷 full 查询 9.62s / 热 0.56s）：
    #     _vec_budget.py:54(_run) → engine.py:536(embed)
    #       → transformers/__init__.py            4.195s   ← **惰性导入**
    #       → transformers/utils/chat_template_utils.py 3.366s
    #       → torch/functional.py                 1.023s
    # 即首个用户查询要在**只有 3s 上限**的查询嵌入线程里现付 ~4.2s 的
    # `import transformers`（外加 ~1s torch）⇒ 必然超时被丢弃
    # （现场 stderr：「查询嵌入超 3.0s 未返回 ⇒ 该向量通道降级为不可用」），
    # 结果是**白付 3s 且向量通道拿不到任何候选**。
    #
    # `_deps._warm` 里本来就有一次等价预热，但它在**后台守护线程**里跑：
    # CPython 的 import 锁会让同时在跑的查询嵌入线程**阻塞在导入上**，
    # 于是"预热"救不了恰好撞上它的首个查询。这里改成在 `uvicorn.run` **之前**
    # 同步完成，保证监听端口时就已就绪。
    # 代价：启动多花 ~5–10s（该进程本就由 supervisor 拉起，不影响可用性口径）。
    # 关闭：TRINITY_PREWARM_EMBED=0；失败静默（惰性路径兜底，行为与改动前一致）。
    if os.environ.get("TRINITY_PREWARM_EMBED", "1") == "1":
        try:
            _t_emb = time.time()
            from trinity.core.client._helpers import _get_embedding_engine
            _eng = _get_embedding_engine()
            if _eng is not None:
                _eng.embed("startup-warmup")
                logger.info(
                    "embedding engine warm-up done in %.2fs (%s)",
                    time.time() - _t_emb, type(_eng).__name__)
        except Exception:  # noqa: BLE001 — 预热尽力而为，失败静默降级（有意）
            swallow(__name__, None)

    # 2026-09-30（外部审计 · 目标项 16）：**BM25 索引也要在监听前就绪**。
    #
    # 现场（进程内 cProfile，含文档语料 24,863 篇）：冷 full 查询 **5.52s** /
    # 热 **0.08s（69×）**，而首个查询里 **3.275s 是 `threading.wait`**
    # —— 主线程在**等后台 BM25 构建完成**。
    #
    # `_deps._startup_prewarm` 确实会构建它，但也在**后台守护线程**里轮询
    # （`_deps.py:159` 的 `_warm`）⇒ 与首个请求**竞速**，谁先到未定义，
    # 而这个竞速的输家正好是"用户看到的第一个查询"。
    #
    # **先量了持久化，再选的同步等待**（`probe_bm25_persist.py`，24,863 文档 /
    # 216,812 词项）：
    #     fetch 1.95s + build 2.18s = 4.13s
    #     落盘 11.3 MB / 保存 6.26s / **载入 1.90s** ⇒ 只省 ~2.2s
    #     载入后 top-10 与在线构建**逐字一致**（`IDENTICAL RESULTS: True`），
    #     指纹不符与损坏文件均被拒绝
    # ⇒ 落盘只值 2.2s，却要背 11.3 MB 缓存 + 失效判定 + 6.3s 保存；
    #   **同步等待构建**代价相同、零缓存、零失效风险，且启动本就几分钟
    #   ⇒ 把 ~4s 从"首个请求"挪到"启动期"是净赚。故默认走同步等待。
    # 回滚：TRINITY_PREWARM_BM25=0（回到与首请求竞速的旧行为）。
    if os.environ.get("TRINITY_PREWARM_BM25", "1") == "1":
        try:
            _t_bm = time.time()
            _mem0 = get_memory()
            if _mem0 is not None and hasattr(_mem0, "_ensure_bm25_index"):
                _mem0._ensure_bm25_index()          # 触发构建（内部可能再起线程）
                if hasattr(_mem0, "_wait_bm25_ready"):
                    _mem0._wait_bm25_ready(timeout=120.0)
                logger.info(
                    "bm25 index ready before listen in %.2fs (docs=%s, stage=%s)",
                    time.time() - _t_bm, getattr(_mem0, "_bm25_doc_count", "?"),
                    getattr(_mem0, "_bm25_build_stage", "?"))
        except Exception:  # noqa: BLE001 — 预热尽力而为，失败静默降级（有意）
            swallow(__name__, None)
    tls_kwargs = _tls_uvicorn_kwargs()
    scheme = "https" if tls_kwargs else "http"
    # §785.6 遗留⑤：自报身份（pid / 启动锚 / **package_file**）——
    # 让"这个进程跑的是哪一版代码"可被**决定性**判定（判据在调用方算，服务侧零扫描成本）。
    try:
        from trinity import code_provenance as _cp
        logger.info("code provenance: %s", _cp.identity())
    except Exception:  # noqa: BLE001 — 自报失败不得影响启动
        swallow(__name__, None)
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
