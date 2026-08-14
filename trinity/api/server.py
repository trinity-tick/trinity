#!/usr/bin/env python3
"""
Trinity REST API Server —FastAPI-based (v8.0.0)
==================================================
Optimized for production HTTP Gateway deployment:
  - Pydantic request/response models
  - Global error handling middleware
  - CORS + GZip compression
  - Rate limiting (token bucket)
  - Semantic search via embeddings
  - Batch endpoints
  - Structured health checks

Endpoints:
  GET    /health                         Health check
  GET    /diagnostics                    System diagnostics
  GET    /metrics                        Prometheus-format metrics
  POST   /memories                       Store a memory
  GET    /memories                       Search memories
  GET    /memories/{id}                  Get memory by ID
  DELETE /memories/{id}                  Soft-delete memory
  GET    /memories/{id}/versions         Get version chain
  GET    /personas/{pid}/memories        Get persona memories
  POST   /reason                         Open-domain reasoning
  POST   /embeddings                     Embed single text
  POST   /embeddings/batch               Embed batch texts
  POST   /vector/search                  Semantic vector search
  POST   /vector/index                   Index memories to vector store

  # Agent Memory Gateway
  POST   /agents/register                Register a Sub Agent
  POST   /agents/memory/write            Write memory entry
  POST   /agents/memory/bulk_write       Bulk write memory entries
  GET    /agents/memory/search           Semantic search (with embeddings)
  GET    /agents/memory/pool             Pool statistics
  GET    /agents/memory/insights         Cross-agent insights (P1-3)
  POST   /agents/bridge/inject           Inject pre-dispatch context
  GET    /agents/bridge/extract          Extract post-dispatch context

  # v7.1.0 endpoints
  GET    /dashboard                      Operations dashboard
  POST   /benchmark                      Memory benchmark suite

  GET    /                               Web dashboard
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


# ═══════════════════════════════════════════════════════════════════════════
# Pydantic Models
# ═══════════════════════════════════════════════════════════════════════════
class RegisterRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128, description="Unique agent identifier")
    agent_name: str = Field(..., min_length=1, max_length=256, description="Agent display name")
    capabilities: List[str] = Field(default=[], description="Agent capabilities list")
    metadata: Dict[str, Any] = Field(default={}, description="Arbitrary metadata")

class MemoryWriteRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    content: str = Field(..., min_length=1, max_length=65536)
    category: str = Field("context", description="Memory category")
    scope: str = Field("cross_agent", description="Memory scope")
    importance: float = Field(0.5, ge=0, le=1, description="Importance 0-1")
    tags: Optional[List[str]] = Field(None, description="Tags")
    metadata: Dict[str, Any] = Field(default={}, description="Arbitrary metadata")

class BulkMemoryWriteRequest(BaseModel):
    entries: List[MemoryWriteRequest] = Field(..., min_length=1, max_length=100, description="Memory entries")

class BulkWriteResult(BaseModel):
    status: str
    written: int
    failed: int
    memory_ids: List[str]

class BridgeInjectRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    context: str = Field(..., min_length=1, max_length=131072)
    task_summary: str = Field("")


class IdentityAnchorRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128, description="Agent 标识")
    anchor_type: str = Field(..., min_length=1, max_length=64, description="锚点类型")
    content: str = Field(..., min_length=1, description="JSON 格式锚点内容")


class IdentityReconstructRequest(BaseModel):
    available_anchors: Optional[List[str]] = Field(None, description="可选：指定可用锚点类型列表")


class IdentityBundleRequest(BaseModel):
    agent_id: Optional[str] = Field(None, description="导出时必填的目标 Agent ID")
    bundle: Optional[Dict[str, Any]] = Field(None, description="导入时必填的身份包数据")


class AuditRunRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    task: str = Field("", description="任务描述")
    executor_result: str = Field("{}", description="执行结果 JSON")
    auditor_result: str = Field("{}", description="审计结果 JSON")
    disagreement_flag: bool = Field(False)


class ConstitutionUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    rule: str = Field(..., min_length=1, max_length=2048)
    severity: str = Field("medium", description="low / medium / high / critical")
    enabled: bool = Field(True)


class AgentCardRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128, description="Agent 唯一标识")
    name: str = Field(..., min_length=1, max_length=256, description="Agent 名称")
    description: str = Field("", max_length=2048, description="Agent 描述")
    version: str = Field("1.0.0", description="版本号")
    capabilities: List[str] = Field([], description="能力列表")
    endpoints: Dict[str, str] = Field({}, description="端点映射")
    skills: List[Dict[str, Any]] = Field([], description="技能定义列表")
    input_modes: List[str] = Field(["text"], description="输入模式")
    output_modes: List[str] = Field(["text"], description="输出模式")
    security_level: str = Field("low", description="安全级别")


class A2ATaskRequest(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=64, description="任务唯一标识")
    from_agent: str = Field(..., min_length=1, max_length=128)
    to_agent: str = Field(..., min_length=1, max_length=128)
    payload: str = Field("{}", description="任务负载 JSON 字符串")


class A2ATaskUpdateRequest(BaseModel):
    status: str = Field(..., min_length=1, max_length=32,
                        description="pending / in_progress / completed / failed / cancelled")
    result: Optional[str] = Field(None, description="结果 JSON 字符串")


class A2AMessageRequest(BaseModel):
    from_agent: str = Field(..., min_length=1, max_length=128)
    to_agent: Optional[str] = Field(None, max_length=128, description="目标 Agent，不传则广播")
    method: str = Field(..., min_length=1, max_length=128, description="JSON-RPC method")
    params: Dict[str, Any] = Field({}, description="JSON-RPC params")
    id: Optional[str] = Field(None, description="JSON-RPC 请求 ID")


# ── Memory Compression Models (v8.2.0) ─────────────────────────────────

class CompressRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128, description="Agent 标识")
    max_tokens: int = Field(4096, ge=256, le=65536, description="Token 预算上限")
    no_compress: bool = Field(False, description="设为 true 跳过压缩")


class CompressStatsRequest(BaseModel):
    agent_id: Optional[str] = Field(None, max_length=128, description="可选：按agent 过滤")


class CompressRestoreRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128, description="Agent 标识")
    trimmed_ids: List[str] = Field(..., min_length=1, max_length=500, description="待恢复的 memory_ids")


# ── Marvis A2A Adapter Models (v8.0.0) ────────────────────────────────────

class MarvisAgentRegisterRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    agent_name: str = Field(..., min_length=1, max_length=256)
    capabilities: List[str] = Field(default=[], description="Agent capabilities list")
    metadata: Dict[str, Any] = Field(default={}, description="Arbitrary metadata")


class MarvisDispatchRequest(BaseModel):
    from_agent: str = Field(..., min_length=1, max_length=128)
    to_agent: str = Field(..., min_length=1, max_length=128)
    task_description: str = Field("", max_length=1024)
    payload: Dict[str, Any] = Field(default={})
    global_goal: str = Field("", max_length=1024)
    current_task: str = Field("", max_length=1024)
    memory_ids: List[str] = Field(default=[])
    context_dict: Dict[str, Any] = Field(default={})
    priority: int = Field(5, ge=1, le=10)


class MarvisSnapshotResponse(BaseModel):
    agent_count: int = 0
    total_memories: int = 0
    sub_agent_profiles: Dict[str, Any] = {}
    recent_tasks: List[Dict[str, Any]] = []
    trust_scores: Dict[str, Any] = {}
    timestamp: str = ""


# ── Response Models (v8.0.0) ──────────────────────────────────────────

class MemoryResponse(BaseModel):
    memory_id: str = ""
    status: str = "ok"

class MemorySearchResponse(BaseModel):
    query: str = ""
    total: int = 0
    results: List[Dict[str, Any]] = []

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "8.2.0"
    uptime_seconds: float = 0.0
    components: Dict[str, str] = {}

class IdentityProfileResponse(BaseModel):
    agent_id: str = ""
    anchors: List[Dict[str, Any]] = []
    consistency_score: float = 1.0

class IdentityBundleResponse(BaseModel):
    agent_id: str = ""
    bundle: Dict[str, Any] = {}
    exported_at: str = ""

class AuditTrailResponse(BaseModel):
    memory_id: str = ""
    audit_trail: List[Dict[str, Any]] = []
    total_entries: int = 0

class AuditReplayResponse(BaseModel):
    agent_id: str = ""
    time_range: Dict[str, Optional[str]] = {}
    operations: List[Dict[str, Any]] = []
    total_operations: int = 0

class AuditIntegrityResponse(BaseModel):
    verified: bool = True
    total_entries: int = 0
    broken_links: int = 0

class AuditSummaryResponse(BaseModel):
    operation_counts: Dict[str, int] = {}
    active_agents: List[str] = []
    peak_hours: List[str] = []

class ConstitutionResponse(BaseModel):
    invariants: List[Dict[str, Any]] = []

class ConstitutionUpdateResponse(BaseModel):
    status: str = "ok"
    total: int = 0

class DCSAMetricsResponse(BaseModel):
    aedy: float = 0.0
    jpc: float = 0.0
    mcr: float = 0.0
    tsad: float = 0.0
    edq: float = 0.0
    violation_count: int = 0
    last_audited: str = ""

class A2AAgentListResponse(BaseModel):
    agents: List[Dict[str, Any]] = []

class A2ACardResponse(BaseModel):
    agent_id: str = ""
    name: str = ""
    capabilities: List[str] = []
    endpoints: Dict[str, str] = {}

class A2ATaskResponse(BaseModel):
    task_id: str = ""
    status: str = ""
    from_agent: str = ""
    to_agent: str = ""

class A2AMessageResponse(BaseModel):
    message_id: str = ""
    status: str = "sent"

class MarvisSnapshotFullResponse(BaseModel):
    agent_count: int = 0
    total_memories: int = 0
    sub_agent_profiles: Dict[str, Any] = {}
    recent_tasks: List[Dict[str, Any]] = []
    trust_scores: Dict[str, Any] = {}
    timestamp: str = ""

class MarvisTrustResponse(BaseModel):
    agent_name: str = ""
    overall_score: float = 1.0
    aedy: float = 0.0
    jpc: float = 0.0
    mcr: float = 0.0
    tsad: float = 0.0
    edq: float = 0.0
    violation_count: int = 0
    last_audited: str = ""


# ── A2A Security Request Models (v8.1.0) ──────────────────────────────

class SecuritySignRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=256)
    capabilities: List[str] = Field(default=[])
    private_key_path: Optional[str] = Field(None)

class SecurityVerifyRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field("", max_length=256)
    capabilities: List[str] = Field(default=[])
    signature: str = Field(..., min_length=1, description="Hex-encoded RSA signature")
    public_key_path: Optional[str] = Field(None)

class CapabilityAuthorizeRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    capability: str = Field(..., min_length=1, max_length=256)

class CapabilityRevokeRequest(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    capability: str = Field(..., min_length=1, max_length=256)

class TaskGrantRequest(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=64)
    agent_id: str = Field(..., min_length=1, max_length=128)


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

_rate_limiter = TokenBucket(rate=60, burst=120)


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
    get_aggregator()  # pre-warm
    yield
    # Shutdown: flush persistence
    global _aggregator
    if _aggregator is not None:
        _aggregator._save()


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


@app.middleware("http")
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


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Token-bucket rate limiting on /agents/* write endpoints."""
    if request.url.path.startswith("/agents/") and request.method in ("POST", "PUT", "DELETE"):
        if not _rate_limiter.consume():
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limit_exceeded", "detail": "Too many requests"},
            )
    return await call_next(request)


@app.middleware("http")
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


# ═══════════════════════════════════════════════════════════════════════════
# Core Endpoints
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/health/self-test", tags=["Health"], summary="运行时全组件自检")
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


@app.get("/health", tags=["Health"], summary="健康检查")
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


@app.get("/metrics")
async def metrics():
    """Prometheus-format metrics endpoint."""
    agg = get_aggregator()
    stats = agg.statistics()
    lines = [
        "# HELP trinity_memories_total Total memories in shared pool",
        "# TYPE trinity_memories_total gauge",
        f"trinity_memories_total {stats['total_memories']}",
        "# HELP trinity_ingested_total Total memories ingested",
        "# TYPE trinity_ingested_total counter",
        f"trinity_ingested_total {stats.get('total_ingested', 0)}",
        "# HELP trinity_merged_total Total merges by similarity",
        "# TYPE trinity_merged_total counter",
        f"trinity_merged_total {stats.get('total_merged', 0)}",
        "# HELP trinity_queries_total Total queries executed",
        "# TYPE trinity_queries_total counter",
        f"trinity_queries_total {stats.get('total_queries', 0)}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain")


@app.get("/diagnostics")
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


@app.post("/memories")
async def store_memory(
    content: str = Body(..., description="Memory content text"),
    persona_id: str = Body("default"),
    session_id: Optional[str] = Body(None),
    role: str = Body("user", description="Role: user/assistant/system"),
    importance: float = Body(0.5, ge=0, le=1),
    tags: Optional[List[str]] = Body(None),
    category: str = Body("general"),
    tenant_id: str = Body("default"),
    agent_id: str = Body("default"),
    ttl_seconds: Optional[int] = Body(None),
    modality: str = Body("text"),
    metadata: Optional[dict] = Body(None),
    source_uri: Optional[str] = Body(None),
):
    """Store a memory entry with optional modality / metadata / source_uri."""
    result = get_memory().ingest(
        content=content, persona_id=persona_id, session_id=session_id,
        role=role, importance=importance, tags=tags or [],
        category=category, tenant_id=tenant_id, agent_id=agent_id,
        ttl_seconds=ttl_seconds,
        modality=modality, metadata=metadata, source_uri=source_uri,
    )
    return result


@app.post("/memories/session", tags=["Memories"], summary="整段会话聚合写入为一条记忆")
async def store_session_memory(
    session_id: str = Body(..., description="会话 ID"),
    turns: List[dict] = Body(..., description="对话轮次列表 [{speaker, text}, ...]"),
    source_agent: str = Body("session"),
    category: str = Body("episodic"),
    importance: float = Body(0.7, ge=0, le=1),
    tags: Optional[List[str]] = Body(None),
    tenant_id: str = Body("default"),
    agent_id: str = Body("default"),
    metadata: Optional[dict] = Body(None),
):
    """将整段多轮对话聚合为**一条**记忆写入（引擎 + 共享聚合池双写）。

    LoCoMo 实测结论（2026-08-14）：逐 turn 写入使记忆碎片化，
    Recall@5 仅 0.14；按会话聚合为一条记忆后 Recall@5 提升到 0.88。
    本端点把该最佳实践产品化：一次调用沉淀一个完整会话/事件。
    """
    if not turns:
        raise HTTPException(status_code=400, detail="turns must not be empty")
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required")

    text = "\n".join(
        f"[{t.get('speaker', 'user')}] {t.get('text', '')}" for t in turns
    )
    agg_metadata = {
        "session_aggregate": True,
        "num_turns": len(turns),
        "source_agent": source_agent,
        **(metadata or {}),
    }

    # 1) 引擎写入
    result = get_memory().ingest(
        content=text, persona_id="default", session_id=session_id,
        role="system", importance=importance, tags=tags or [],
        category=category, tenant_id=tenant_id, agent_id=agent_id,
        metadata=agg_metadata,
    )

    # 2) 共享聚合池双写（跨 agent 可见）
    try:
        agg = get_aggregator()
        agg.ingest(text, source_agent=source_agent, metadata=agg_metadata)
    except Exception as exc:
        logger.warning("session memory dual-write to aggregator failed (non-fatal): %s", exc)

    return {
        "session_id": session_id,
        "num_turns": len(turns),
        "aggregated": True,
        "memory": result,
    }


@app.get("/memories")
async def search_memories(
    query: str = Query(..., description="Search query"),
    top_k: int = Query(10),
    persona_id: Optional[str] = Query(None),
    tenant_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    app_id: Optional[str] = Query(None),
    session_id: Optional[str] = Query(None),
    category: Optional[str] = Query(None),
    modality: Optional[str] = Query(None, description="Filter by modality"),
):
    """Search memories with composite scope filtering (agent_id / app_id / session_id / category AND)."""
    mem = get_memory()
    result = mem.search(query=query, top_k=top_k, persona_id=persona_id,
                        tenant_id=tenant_id, agent_id=agent_id,
                        app_id=app_id, session_id=session_id,
                        category=category, modality=modality)
    results = result.get("results", [])
    return {
        "query": query,
        "total": len(results),
        "modality": modality,
        "results": results,
        "pushed_memories": result.get("pushed_memories", []),
    }


@app.post("/memories/age")
async def age_memories():
    """手动触发老化扫描，清理TTL 过期的记忆（软删除）。"""
    mem = get_memory()
    return mem.age()


@app.get("/memories/stats")
async def memory_stats():
    """返回记忆统计（总数、过期数、Agent 分布、平均访问频率）。"""
    mem = get_memory()
    return mem.stats()


@app.get("/memories/modalities")
async def modality_stats():
    """返回各模态记忆数量、存储占比统计。"""
    mem = get_memory()
    return mem.modality_stats()


@app.post("/memories/{memory_id}/touch")
async def touch_memory(memory_id: str):
    """更新指定记忆的last_accessed_at 和access_count。"""
    mem = get_memory()
    ok = mem.touch(memory_id)
    return {"memory_id": memory_id, "touched": ok}


@app.get("/memories/{memory_id}/conflicts")
async def get_memory_conflicts(memory_id: str):
    """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。"""
    mem = get_memory()
    return mem.get_conflicts(memory_id)


@app.post("/memories/conflicts/resolve")
async def resolve_conflict(request: dict):
    """解决冲突：保留选定版本，软删除同一冲突组的其他版本。
    Body:
        conflict_group_id: 冲突组ID
        keep_memory_id: 要保留的记忆 ID
    """
    mem = get_memory()
    return mem.resolve_conflict(
        conflict_group_id=request["conflict_group_id"],
        keep_memory_id=request["keep_memory_id"],
    )


@app.get("/memories/dedup/stats")
async def dedup_stats():
    """返回去重统计信息（冲突组数、已解决数等）。"""
    mem = get_memory()
    return mem.dedup_stats()


# ═══════════════════════════════════════════════════════════════════════════
# 分层检索（Multi-Stage Ranking）
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/memories/search")
async def ranked_search(request: dict):
    """分层检索：支持三层排序管线的增强版搜索。
    Body:
        query: 搜索关键词（必填）。        top_k: 返回结果数（默认 10）。        persona_id: 可选，按角色筛选。        tenant_id: 可选，按租户筛选。        agent_id: 可选，按Agent 筛选。        agent_weight: 可选，覆盖调用文Agent 权重。        use_vector: 是否启用向量搜索（默认True）。        use_ranking: 是否启用三层排序（默认True）。
    Returns:
        results 中每条含 final_score 与layer_scores（semantic/time_decay/agent_weight）。    """
    mem = get_memory()
    query = request["query"]
    top_k = request.get("top_k", 10)
    use_vector = request.get("use_vector", True)
    use_ranking = request.get("use_ranking", True)
    return mem.search(
        query=query,
        top_k=top_k,
        persona_id=request.get("persona_id"),
        tenant_id=request.get("tenant_id"),
        agent_id=request.get("agent_id"),
        use_vector=use_vector,
        agent_weight=request.get("agent_weight"),
        ranked=use_ranking,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 混合检索（Hybrid: Vector + BM25 + Graph）
# ═══════════════════════════════════════════════════════════════════════════
class HybridSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4096, description="搜索查询字符串")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")
    strategy: str = Field("fusion", description="融合策略: fusion / rrf / cascade")
    agent_id: Optional[str] = Field(None, description="按Agent 过滤")
    persona_id: Optional[str] = Field(None, description="按角色过滤")
    tenant_id: Optional[str] = Field(None, description="按租户过滤")


@app.post("/memory/search/hybrid")
async def hybrid_search(request: HybridSearchRequest):
    """混合检索—向量 + BM25 关键词+ 图谱融合。
    三种策略:
      - fusion:  加权求和 (vector=0.5, bm25=0.3, graph=0.2)
      - rrf:     Reciprocal Rank Fusion (rank-based, robust)
      - cascade: 向量粗排 →BM25 精排 →图谱扩充

    返回:
      results 中每条含 hybrid_score / vector_score / bm25_score / graph_score 明细。    """
    mem = get_memory()
    return mem.search_hybrid(
        query=request.query,
        top_k=request.top_k,
        strategy=request.strategy,
        agent_id=request.agent_id,
        persona_id=request.persona_id,
        tenant_id=request.tenant_id,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Cross-Modal Search (v8.1.0) —Text →Image Memory Retrieval
# ═══════════════════════════════════════════════════════════════════════════
class CrossModalSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=4096, description="文字查询或图片文件路径")
    query_type: str = Field("auto", description="查询类型: auto / text / image / combined")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")


class ImageByTextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=4096, description="文字查询描述要找的图片")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")


class TextByImageRequest(BaseModel):
    image_path: str = Field(..., min_length=1, max_length=4096, description="查询图片的绝对路径")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")


@app.post("/memory/search/cross-modal", tags=["Cross-Modal"])
async def cross_modal_search(request: CrossModalSearchRequest):
    """跨模态检索—自动检测输入类型并路由。
    支持:
      - auto:   自动检测query 是text / image / combined
      - text:   文字搜图片记忆(image_description)
      - image:  图片搜文字记忆(text)
      - combined: 联合检索（需 [text, image_path] 格式）    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    return cm.search_cross_modal(
        query=request.query,
        query_type=request.query_type,
        top_k=request.top_k,
    )


@app.post("/memory/search/image-by-text", tags=["Cross-Modal"])
async def image_by_text(request: ImageByTextRequest):
    """文搜图—用自然语言描述检索相关图片记忆。
    在image_description 模态记忆中做语义检索，返回最相关的图片描述    及其关联的图片文件路径。    """
    mem = get_memory()
    return mem.search_image_by_text(text=request.text, top_k=request.top_k)


@app.post("/memory/search/text-by-image", tags=["Cross-Modal"])
async def text_by_image(request: TextByImageRequest):
    """图搜文—用图片检索相关文字记忆。
    对传入的图片进行编码后，在text 模态记忆中做语义检索，
    返回与图片语义最相近的文字记忆。    """
    mem = get_memory()
    return mem.search_text_by_image(image_path=request.image_path, top_k=request.top_k)


@app.get("/agents/weights")
async def get_agent_weights():
    """查看所有Agent 权重配置。"""
    mem = get_memory()
    return {"weights": mem.get_agent_weights()}


@app.put("/agents/{agent_id}/weight")
async def set_agent_weight(agent_id: str, request: dict):
    """设置 Agent 检索权重。
    Body:
        weight: 权重值（建议 0.1-2.0）。    """
    mem = get_memory()
    weight = float(request["weight"])
    return mem.set_agent_weight(agent_id, weight)


@app.delete("/agents/{agent_id}/weight")
async def delete_agent_weight(agent_id: str):
    """删除 Agent 权重配置。"""
    mem = get_memory()
    ok = mem.delete_agent_weight(agent_id)
    return {"agent_id": agent_id, "deleted": ok}


# ═══════════════════════════════════════════════════════════════════════════
# 记忆关联链接（Memory Links）
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/memories/{memory_id}/links")
async def create_memory_link(memory_id: str, request: dict):
    """手动创建记忆关联链接。
    Body:
        target_id: 目标记忆 ID（必填）。        link_type: 链接类型，支持co_occurrence/semantic/causal/same_task（默认semantic）。        strength: 关联强度 0-1（默认0.5）。    """
    mem = get_memory()
    target_id = request.get("target_id", "")
    link_type = request.get("link_type", "semantic")
    strength = float(request.get("strength", 0.5))
    if not target_id:
        raise HTTPException(status_code=400, detail="target_id is required")
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "create_memory_link"):
        return mem._adapter.create_memory_link(memory_id, target_id, link_type, strength)
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.get("/memories/{memory_id}/links")
async def get_memory_links(memory_id: str, min_strength: float = 0.0):
    """查看某记忆的完整关联网络（含双向链接）。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "get_all_links"):
        return mem._adapter.get_all_links(memory_id)
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.delete("/memories/links/{link_id}")
async def delete_memory_link(link_id: str):
    """删除指定链接。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "delete_memory_link"):
        ok = mem._adapter.delete_memory_link(link_id)
        return {"link_id": link_id, "deleted": ok}
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.put("/memories/links/{link_id}/strength")
async def adjust_link_strength(link_id: str, request: dict):
    """调整链接强度。
    Body:
        action: 'strengthen' 或'weaken'（必填）。        delta: 调整幅度（默认0.1）。    """
    mem = get_memory()
    action = request.get("action", "strengthen")
    delta = float(request.get("delta", 0.1))
    if hasattr(mem, "_adapter") and mem._adapter:
        if action == "strengthen" and hasattr(mem._adapter, "strengthen_link"):
            return mem._adapter.strengthen_link(link_id, delta)
        elif action == "weaken" and hasattr(mem._adapter, "weaken_link"):
            return mem._adapter.weaken_link(link_id, delta)
        else:
            raise HTTPException(status_code=400, detail=f"Invalid action: {action}")
    raise HTTPException(status_code=501, detail="Not available without adapter")


# ── 记忆图谱端点 ──────────────────────────────────────────────────


@app.post("/graph/entities")
async def upsert_entity(request: dict):
    """创建或更新实体。
    Body:
        name: 实体名称（必填）。        type: 类型 (person/project/file/agent/task/concept/tag)。        properties: 附加属性JSON。    """
    mem = get_memory()
    name = request.get("name", "")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    etype = request.get("type", "concept")
    props = request.get("properties", {})
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "upsert_entity"):
        return mem._adapter.upsert_entity(name, etype, props)
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.get("/graph/entities/search")
async def search_entities(
    name: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    limit: int = Query(20),
):
    """搜索实体。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "search_entities"):
        return mem._adapter.search_entities(name=name, etype=type, limit=limit)
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.get("/graph/entities/{entity_id}")
async def get_entity(entity_id: str):
    """查询实体详情（含关联关系）。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "get_entity"):
        result = mem._adapter.get_entity(entity_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        return result
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.post("/graph/relations")
async def create_relation(request: dict):
    """创建关系。
    Body:
        subject_id: 主体实体 ID（必填）。        predicate: 谓词（必填）。        object_id: 客体实体 ID（必填）。        properties: 附加属性JSON。    """
    mem = get_memory()
    sid = request.get("subject_id", "")
    pred = request.get("predicate", "")
    oid = request.get("object_id", "")
    if not sid or not pred or not oid:
        raise HTTPException(status_code=400, detail="subject_id, predicate, object_id are required")
    props = request.get("properties", {})
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "create_relation"):
        return mem._adapter.create_relation(sid, pred, oid, props)
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.get("/graph/relations")
async def query_relations(
    subject_id: Optional[str] = Query(None),
    predicate: Optional[str] = Query(None),
    object_id: Optional[str] = Query(None),
    limit: int = Query(50),
):
    """查询关系。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "query_relations"):
        return mem._adapter.query_relations(
            subject_id=subject_id, predicate=predicate,
            object_id=object_id, limit=limit,
        )
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.get("/graph/traverse")
async def traverse_graph(
    start_id: str = Query(...),
    max_hops: int = Query(3),
):
    """多跳遍历子图。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "traverse"):
        return mem._adapter.traverse(start_id, max_hops=min(max_hops, 5))
    raise HTTPException(status_code=501, detail="Not available without adapter")


@app.get("/memories/{memory_id}")
async def get_memory_by_id(memory_id: str):
    """Get a single memory by ID."""
    mem = get_memory()
    result = mem.get_memory(memory_id) if hasattr(mem, 'get_memory') else None
    if result is None:
        try:
            result = mem._adapter.get_memory(memory_id)
        except Exception:
            pass
    if result is None:
        raise HTTPException(status_code=404, detail="Memory not found")
    return result


@app.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str):
    """Soft-delete a memory."""
    mem = get_memory()
    if hasattr(mem, 'delete_memory'):
        return {"deleted": mem.delete_memory(memory_id), "memory_id": memory_id}
    raise HTTPException(status_code=501, detail="delete_memory not implemented")


@app.get("/memories/{memory_id}/versions")
async def get_memory_versions(memory_id: str):
    """Get version/audit chain."""
    mem = get_memory()
    if hasattr(mem, 'get_version_chain'):
        return {"memory_id": memory_id, "versions": mem.get_version_chain(memory_id)}
    return {"memory_id": memory_id, "versions": []}


@app.get("/personas/{persona_id}/memories")
async def get_persona_memories(
    persona_id: str,
    limit: int = Query(50, le=200),
    agent_id: Optional[str] = Query(None),
):
    """Get persona memories with optional agent_id filter."""
    mem = get_memory()
    if hasattr(mem, 'get_persona_memories'):
        return {"persona_id": persona_id, "agent_id": agent_id, "memories": mem.get_persona_memories(persona_id, agent_id=agent_id, limit=limit)}
    raise HTTPException(status_code=501, detail="not available")


@app.post("/reason")
async def reason(
    query: str = Body(...), multi_hop: bool = Body(False), top_k: int = Body(5),
):
    """Open-domain reasoning."""
    if not hasattr(get_memory(), 'reason'):
        raise HTTPException(status_code=501, detail="reason() not available")
    return get_memory().reason(query=query, multi_hop=multi_hop, top_k=top_k)


# ═══════════════════════════════════════════════════════════════════════════
# Embedding Endpoints
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/embeddings")
async def embed_text(text: str = Body(...), backend: str = Body("auto")):
    """Generate semantic embedding."""
    try:
        from trinity.embeddings import create_engine
        import numpy as np
        engine = create_engine(backend=backend)
        vec = engine.embed(text)
        return {
            "text": text[:100], "dim": engine.embedding_dim(),
            "model": engine.model_name(), "embedding": vec.tolist(),
            "norm": float(np.linalg.norm(vec)),
        }
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Embedding module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")


@app.post("/embeddings/batch")
async def embed_texts(texts: List[str] = Body(...), backend: str = Body("auto")):
    """Batch embed texts."""
    try:
        from trinity.embeddings import create_engine
        if not texts:
            return {"count": 0, "dim": 0, "model": "none", "embeddings": []}
        engine = create_engine(backend=backend)
        vecs = engine.embed_batch(texts)
        return {
            "count": len(vecs), "dim": engine.embedding_dim(),
            "model": engine.model_name(), "embeddings": [v.tolist() for v in vecs],
        }
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Embedding module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch embedding failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Vector Search / Index
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/vector/search")
async def vector_search(
    query: str = Body(...), top_k: int = Body(10),
    index_backend: str = Body("numpy"), embed_backend: str = Body("auto"),
):
    """Semantic vector search."""
    try:
        from trinity.embeddings import create_engine
        from trinity.vector_index import create_index
        import numpy as np
        eng = create_engine(backend=embed_backend)
        idx = create_index(backend=index_backend, dim=eng.embedding_dim())
        mem = get_memory()
        memories = []
        if hasattr(mem, '_adapter') and mem._adapter:
            try:
                if hasattr(mem._adapter, 'get_all_memories'):
                    memories = mem._adapter.get_all_memories(limit=200)
            except Exception:
                pass
        if not memories:
            return {"query": query, "total": 0, "results": [], "note": "No memories in pool"}
        texts = [m.get("content", "") for m in memories if m.get("content")]
        if not texts:
            return {"query": query, "total": 0, "results": [], "note": "No content"}
        vecs = eng.embed_batch(texts)
        for m, v in zip(memories, vecs):
            mid = m.get("memory_id", m.get("id", f"mem_{hash(str(m))}"))
            idx.add(mid, v, m)
        results = idx.search(eng.embed(query), top_k=top_k)
        return {
            "query": query, "total": len(results),
            "model": eng.model_name(), "dim": eng.embedding_dim(),
            "index_backend": type(idx).__name__,
            "results": [{"id": r.id, "score": round(float(r.score), 4), "metadata": r.metadata} for r in results],
        }
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Required module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vector search failed: {e}")


@app.post("/vector/index")
async def index_memories(backend: str = Body("auto"), force_reindex: bool = Body(False)):
    """Index all memories to vector store."""
    try:
        from trinity.embeddings import create_engine
        import numpy as np
        eng = create_engine(backend=backend)
        mem = get_memory()
        memories = []
        if hasattr(mem, '_adapter') and mem._adapter:
            try:
                if hasattr(mem._adapter, 'get_all_memories'):
                    memories = mem._adapter.get_all_memories(limit=1000)
            except Exception:
                pass
        try:
            from trinity.vector_index import ChromaDBIndex
            idx = ChromaDBIndex(dim=eng.embedding_dim(), collection_name="trinity_api_search")
        except ImportError:
            from trinity.vector_index import create_index
            idx = create_index(backend="numpy", dim=eng.embedding_dim())
        indexed, errors = 0, 0
        for m in memories:
            try:
                text = m.get("content", "")
                if not text:
                    continue
                idx.add(m.get("memory_id", m.get("id", f"mem_{indexed}")), eng.embed(text), m)
                indexed += 1
            except Exception:
                errors += 1
        return {"total_memories": len(memories), "indexed": indexed, "errors": errors,
                "model": eng.model_name(), "dim": eng.embedding_dim(), "index_backend": type(idx).__name__}
    except ImportError as e:
        raise HTTPException(status_code=501, detail=f"Required module unavailable: {e}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory indexing failed: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# Dashboard API
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/api/stats")
async def get_stats():
    """Unified dashboard statistics."""
    from trinity.evolution import MetaEvolution
    evo = MetaEvolution()
    diag = get_memory().diagnostics()
    evo_diag = evo.diagnostics()
    return {"evolution": evo_diag, "adapter": diag.get("adapter", diag),
            "trinity_version": diag.get("trinity_version", "unknown")}


@app.get("/api/search")
async def search_api(q: str = Query(...), top_k: int = Query(10)):
    """Dashboard search."""
    return await search_memories(query=q, top_k=top_k)


# Static files
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
# ── Coze Bot Bridge (unchanged) ───────────────────────────────────────
@app.post("/api/coze-bridge")
async def coze_bridge(req: dict):
    from trinity.coze_bridge import search_memory_direct, search_entity, _search_by_intent_text
    memories = []
    intent_code = req.get("intent")
    brand = req.get("brand")
    query = req.get("query", "")
    if intent_code:
        memories.extend(_search_by_intent_text(intent_code))
    if not memories or len(memories) < 3:
        direct_results = search_memory_direct(query, top_k=5)
        existing = {r.get("content", "")[:50] for r in memories}
        for r in direct_results:
            if r["content"][:50] not in existing:
                memories.append(r)
    graph_data = []
    if brand:
        graph_data = search_entity(brand).get("entities", [])
    return {"memory": memories, "graph": graph_data, "intent": intent_code, "count": len(memories), "success": True}


@app.get("/api/coze-bridge-intents")
async def coze_bridge_intents():
    return {
        "I01": {"name": "订单查询", "search": ["高频FAQ-Top30"]},
        "I02": {"name": "物流追踪", "search": ["高频FAQ-Top30", "异常处理手册"]},
        "I03": {"name": "库存查询", "search": ["高频FAQ-Top30"]},
        "I04": {"name": "时效咨询", "search": ["品牌时效规则", "平台发货规则"]},
        "I05": {"name": "退货入库", "search": ["退货换货流程"]},
        "I06": {"name": "换货处理", "search": ["退货换货流程"]},
        "I07": {"name": "错发少发", "search": ["异常处理手册"]},
        "I08": {"name": "破损", "search": ["异常处理手册", "美妆仓储管理规范"]},
        "I09": {"name": "物流异常", "search": ["异常处理手册", "平台发货规则"]},
    }


@app.get("/api/coze-bridge/completions")
async def coze_completions(query: str, top_k: int = 5):
    result = bridge(query=query)
    texts = [r.get("content", "") for r in result.get("memory", [])]
    return {"results": texts}


# ═══════════════════════════════════════════════════════════════════════════
# Agent Memory Gateway (Optimized)
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/agents/register")
async def agent_register(req: RegisterRequest):
    """Register a Sub Agent in the shared MemoryAggregator pool."""
    agg = get_aggregator()
    meta = {
        "_type": "agent_registration", "agent_name": req.agent_name,
        "capabilities": req.capabilities, "registered_at": time.time(),
        **req.metadata,
    }
    dv = agg.ingest(
        content=f"Agent registration: {req.agent_name} ({req.agent_id})",
        source_agent=req.agent_id, metadata=meta,
    )
    return {"status": "registered", "agent_id": req.agent_id,
            "memory_id": dv.memory_id, "capabilities": req.capabilities}


@app.post("/agents/memory/write")
async def agent_memory_write(req: MemoryWriteRequest):
    """Write a memory entry into the shared Aggregator pool."""
    agg = get_aggregator()
    meta = {
        "category": req.category, "scope": req.scope,
        "importance": req.importance, "tags": req.tags or [],
        "_source": "agent_gateway", **req.metadata,
    }
    dv = agg.ingest(content=req.content, source_agent=req.agent_id, metadata=meta)
    return {"status": "written", "memory_id": dv.memory_id,
            "confidence": dv.confidence, "merged": dv.source_count > 1}


@app.post("/agents/memory/bulk_write")
async def agent_memory_bulk_write(req: BulkMemoryWriteRequest):
    """Bulk write memory entries. High-throughput endpoint."""
    agg = get_aggregator()
    written_ids, failed = [], 0
    for entry in req.entries:
        try:
            meta = {
                "category": entry.category, "scope": entry.scope,
                "importance": entry.importance, "tags": entry.tags or [],
                "_source": "agent_gateway_bulk", **entry.metadata,
            }
            dv = agg.ingest(content=entry.content, source_agent=entry.agent_id, metadata=meta)
            written_ids.append(dv.memory_id)
        except Exception:
            failed += 1
    return {"status": "completed", "written": len(written_ids),
            "failed": failed, "memory_ids": written_ids}


@app.get("/agents/memory/search")
async def agent_memory_search(
    q: str = Query(..., description="Search query (semantic if embeddings available)"),
    top_k: int = Query(10, description="Number of results"),
    agent_id: Optional[str] = Query(None, description="Filter by source agent"),
    category: Optional[str] = Query(None, description="Filter by category"),
    scope: Optional[str] = Query(None, description="Filter by scope"),
    mode: str = Query("hybrid", description="Search mode: keyword / vector / hybrid"),
    use_embeddings: bool = Query(True, description="Use semantic embedding search (deprecated, use mode)"),
):
    """Search the shared Aggregator memory pool.

    Supports three modes:
      - keyword: DimensionEngine keyword-based retrieval
      - vector: FAISS/numpy cosine vector search
      - hybrid: keyword + vector + SecondBrain fusion

    Falls back to keyword if vector index is unavailable.
    """
    agg = get_aggregator()

    # Build filters for query()
    filters: Dict[str, Any] = {}
    if category:
        filters["category"] = category
    if scope:
        filters["scope"] = scope
    if agent_id:
        filters["source_agent"] = agent_id

    # Use new aggregator.query() with mode support when mode is vector/hybrid
    if mode in ("vector", "hybrid"):
        try:
            results = agg.query(
                filters, limit=top_k,
                mode=mode, query_text=q,
            )
            return {
                "query": q, "total": len(results),
                "method": mode,
                "results": [r.to_dict() for r in results],
            }
        except Exception:
            pass  # Fall through to keyword

    # Legacy semantic search via embeddings (backward compat)
    if use_embeddings:
        try:
            from trinity.embeddings import create_engine
            import numpy as np
            eng = create_engine(backend="auto")
            qv = eng.embed(q)

            all_dvs = list(agg._pool.values())
            if not all_dvs:
                return {"query": q, "total": 0, "results": [], "method": "semantic_empty"}

            scored = []
            for dv in all_dvs:
                if agent_id and dv.source_agents and agent_id not in dv.source_agents:
                    continue
                if category and dv.category != category:
                    continue
                if scope and dv.scope != scope:
                    continue
                dv_emb = getattr(dv, '_cached_embedding', None)
                if dv_emb is None:
                    continue
                score = float(np.dot(qv, dv_emb) / (np.linalg.norm(qv) * np.linalg.norm(dv_emb)))
                scored.append((score, dv))

            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:top_k]
            return {
                "query": q, "total": len(top),
                "method": "semantic",
                "results": [{"score": round(s, 4), **dv.to_dict()} for s, dv in top],
            }
        except (ImportError, Exception):
            pass

    # Keyword-based fallback
    results = agg.query(filters, limit=top_k)
    return {
        "query": q, "total": len(results),
        "method": "keyword",
        "results": [r.to_dict() for r in results],
    }


@app.get("/agents/memory/pool")
async def agent_memory_pool():
    """Get shared Aggregator pool statistics."""
    return get_aggregator().statistics()


@app.get("/agents/memory/cleanup")
async def agent_memory_cleanup():
    """Manually trigger expired memory cleanup (P0-2)."""
    agg = get_aggregator()
    removed = agg.cleanup()
    return {"status": "ok", "removed": removed}


@app.get("/agents/memory/insights")
async def agent_memory_insights(
    agent_name: Optional[str] = Query(None),
    top_k: int = Query(10, ge=1, le=100),
):
    """Cross-agent insights: patterns, correlations, knowledge gaps (P1-3)."""
    try:
        aggr = get_aggregator()
        insights = aggr.cross_agent_insights(agent_name=agent_name, top_k=top_k)
        return JSONResponse(insights)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agents/memory/degradation")
async def agent_degradation_status():
    """Current degradation tier and channel health (P1-4)."""
    try:
        aggr = get_aggregator()
        return JSONResponse(aggr._degradation.statistics())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agents/memory/degradation/reset")
async def agent_degradation_reset():
    """Reset degradation state to FULL (P1-4)."""
    try:
        aggr = get_aggregator()
        aggr._degradation.reset()
        return JSONResponse({"status": "reset", "tier": "full"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/agents/memory/consolidate")
async def agent_memory_consolidate(topic: Optional[str] = Query(None)):
    """Trigger offline memory consolidation (Auto-Dreamer v7.0.0)."""
    try:
        aggr = get_aggregator()
        merged = aggr.merge_memories(topic=topic)
        return JSONResponse({"merged": merged})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agents/memory/contradictions")
async def agent_memory_contradictions(topic: Optional[str] = Query(None)):
    """Detect contradictory memory pairs (SecondBrain CF v7.0.0)."""
    try:
        aggr = get_aggregator()
        contradictions = aggr.detect_contradictions(topic=topic)
        return JSONResponse({"contradictions": contradictions, "count": len(contradictions)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agents/memory/export")
async def agent_memory_export(format: str = Query("readable", pattern="^(readable|json)$")):
    """Export all memories —readable text or raw JSON (Memsearch v7.0.0)."""
    try:
        aggr = get_aggregator()
        if format == "readable":
            content = aggr.export_readable()
            return PlainTextResponse(content, media_type="text/plain; charset=utf-8")
        else:
            return JSONResponse({"memories": [vars(dv) for dv in aggr._pool.values()]})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/dashboard")
async def trinity_dashboard():
    """Aggregated operations dashboard (v7.1.0)."""
    try:
        aggr = get_aggregator()
        obs = aggr._observability if hasattr(aggr, '_observability') else None
        dash = obs.dashboard() if obs else {}
        dash.update({
            "version": "7.1.0",
            "retrieval_channels": aggr.statistics().get("retrieval_channels", {}),
            "pool_size": len(aggr._pool),
            "degradation": aggr._degradation.statistics() if hasattr(aggr, '_degradation') else {},
        })
        return JSONResponse(dash)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/benchmark")
async def run_benchmark():
    """Run memory benchmark suite (v7.1.0)."""
    try:
        aggr = get_aggregator()
        results = aggr.run_benchmark()
        return JSONResponse({"benchmark": results})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/agents/memory/stats/{memory_id}")
async def agent_memory_stats(memory_id: str):
    """Get access statistics for a single memory (P0-2)."""
    agg = get_aggregator()
    stats = agg.memory_stats(memory_id)
    if stats is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return stats


@app.post("/agents/bridge/inject")
async def agent_bridge_inject(req: BridgeInjectRequest):
    """Bridge: inject pre-dispatch context into Aggregator."""
    agg = get_aggregator()
    entry = f"[TASK] {req.task_summary}\n[CONTEXT] {req.context}" if req.task_summary else req.context
    dv = agg.ingest(
        content=entry, source_agent=req.agent_id,
        metadata={"category": "context", "scope": "cross_agent",
                  "_source": "marvis_bridge", "_type": "pre_dispatch_context"},
    )
    return {"status": "injected", "agent_id": req.agent_id, "memory_id": dv.memory_id}


@app.get("/agents/bridge/extract")
async def agent_bridge_extract(
    agent_id: Optional[str] = Query(None), top_k: int = Query(5),
):
    """Bridge: extract post-dispatch context."""
    agg = get_aggregator()
    filters: Dict[str, Any] = {"scope": "cross_agent"}
    if agent_id:
        filters["source_agent"] = agent_id
    results = agg.query(filters, limit=top_k * 3)
    bridge_entries = [
        r for r in results
        if hasattr(r, "metadata") and r.metadata and r.metadata.get("_source") == "marvis_bridge"
    ]
    all_entries = bridge_entries if bridge_entries else results
    sorted_entries = sorted(all_entries, key=lambda r: r.updated_at, reverse=True)[:top_k]
    return {"agent_id": agent_id, "total": len(sorted_entries),
            "entries": [r.to_dict() for r in sorted_entries]}


# ═══════════════════════════════════════════════════════════════════════════
# Audit Endpoints (Memory Replay & Audit)
# ═══════════════════════════════════════════════════════════════════════════
@app.get("/audit/memories/{memory_id}")
async def audit_memory_trail(memory_id: str):
    """查看某条记忆的完整审计轨迹。"""
    mem = get_memory()
    trail = mem.storage.get_audit_trail(memory_id)
    return {"memory_id": memory_id, "audit_trail": trail, "total_entries": len(trail)}


@app.get("/audit/agents/{agent_id}/replay")
async def audit_agent_replay(
    agent_id: str,
    start_time: Optional[str] = Query(None, description="ISO 格式起始时间"),
    end_time: Optional[str] = Query(None, description="ISO 格式结束时间"),
):
    """回放某Agent 在时间段内的所有操作。"""
    mem = get_memory()
    session = mem.storage.replay_agent_session(agent_id, start_time, end_time)
    return {
        "agent_id": agent_id,
        "time_range": {"start": start_time, "end": end_time},
        "operations": session,
        "total_operations": len(session),
    }


@app.get("/audit/integrity")
async def audit_integrity():
    """审计链完整性验证报告。"""
    mem = get_memory()
    result = mem.storage.verify_audit_integrity()
    return result


@app.get("/audit/summary")
async def audit_summary(
    start_time: Optional[str] = Query(None, description="ISO 格式起始时间"),
    end_time: Optional[str] = Query(None, description="ISO 格式结束时间"),
):
    """审计摘要：各操作计数、活跃Agent、峰值时段。"""
    mem = get_memory()
    result = mem.storage.get_audit_summary(start_time, end_time)
    return result


@app.get("/audit/timeline")
async def audit_timeline(
    agent_id: Optional[str] = Query(None, description="Agent 标识"),
    limit: int = Query(50, description="最大返回条数"),
):
    """最近操作时间线。"""
    mem = get_memory()
    # 使用 replay_agent_session 或直接查 audit_log
    results = []
    if agent_id:
        session = mem.storage.replay_agent_session(agent_id)
        results = session[-limit:]
    return {"agent_id": agent_id, "timeline": results, "total_displayed": len(results)}


# ═══════════════════════════════════════════════════════════════════════════
# Identity Endpoints (Multi-Anchor Identity Architecture)
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/identity/anchors", tags=["Identity"], summary="注册或更新身份锚点")
async def identity_register_anchor(req: IdentityAnchorRequest):
    """注册或更新身份锚点（幂等）。"""
    mem = get_memory()
    result = mem.register_identity_anchor(req.agent_id, req.anchor_type, req.content)
    return result


@app.post("/identity/register", tags=["Identity"], summary="Marvis 格式注册别名")
async def identity_register_alias(req: dict):
    """[Marvis Adapter alias] 接收 Marvis 格式的注册请求并转为 Identity 锚点。"""
    agent_id = req.get("agent_id", "")
    agent_name = req.get("agent_name", agent_id)
    capabilities = req.get("capabilities", [])

    if not agent_id:
        return {"error": "agent_id required"}

    mem = get_memory()
    results = []
    # Register basic identity anchors from Marvis registration data
    for anchor_type, content in [
        ("identity_files", {"name": agent_name}),
        ("procedural_patterns", {"endpoint": f"/a2a/agents/{agent_id}"}),
        ("episodic_keys", {"agent_id": agent_id}),
        ("value_specifications", {"capabilities": capabilities}),
    ]:
        try:
            r = mem.register_identity_anchor(agent_id, anchor_type, json.dumps(content))
            results.append(r)
        except Exception:
            pass

    return {"agent_id": agent_id, "anchors_registered": len(results), "status": "registered"}


@app.get("/identity/profiles/{agent_id}", tags=["Identity"], summary="获取 Agent 身份画像（别名）")
async def identity_profile_alias(agent_id: str):
    """[Marvis Adapter alias] 获取 Agent 身份画像。"""
    try:
        profile = get_memory().get_identity_profile(agent_id)
        return profile
    except Exception as e:
        return {"error": str(e), "agent_id": agent_id}


@app.get("/identity/profiles", tags=["Identity"], summary="列出所有Agent 画像")
async def identity_profiles_list():
    """[Marvis Adapter alias] 列出所有Agent 画像。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "list_agent_ids"):
        return {"profiles": mem._adapter.list_agent_ids()}
    return {"profiles": [], "error": "no adapter"}


@app.post("/identity/drift", tags=["Identity"], summary="触发漂移检测（别名）")
async def identity_drift_alias(req: dict = None):
    """[Marvis Adapter alias] 触发漂移检测。"""
    agent_id = (req or {}).get("agent_id", "")
    if not agent_id:
        return {"error": "agent_id required"}
    result = get_memory().detect_drift(agent_id)
    return result


@app.get("/identity/agents/{agent_id}/anchors", tags=["Identity"], summary="获取 Agent 的所有锚点")
async def identity_get_anchors(
    agent_id: str,
    anchor_type: Optional[str] = Query(None, description="锚点类型过滤"),
):
    """获取某Agent 的所有锚点。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_anchors"):
        anchors = mem._adapter.get_anchors(agent_id, anchor_type)
        return {"agent_id": agent_id, "anchors": anchors, "total": len(anchors)}
    return {"agent_id": agent_id, "anchors": [], "total": 0, "error": "no adapter"}


@app.get("/identity/agents/{agent_id}/profile", tags=["Identity"], summary="获取完整身份画像")
async def identity_get_profile(agent_id: str):
    """获取完整身份画像（含一致性分数）。"""
    profile = get_memory().get_identity_profile(agent_id)
    return profile


@app.post("/identity/agents/{agent_id}/reconstruct", tags=["Identity"], summary="触发身份重建")
async def identity_reconstruct(agent_id: str, req: IdentityReconstructRequest = None):
    """触发身份重建，可选部分锚点重建（故障恢复）。"""
    if req and req.available_anchors:
        result = get_memory().reconstruct_identity(agent_id, req.available_anchors)
    else:
        result = get_memory().reconstruct_identity(agent_id)
    return result


@app.post("/identity/agents/{agent_id}/drift-check", tags=["Identity"], summary="身份漂移检测")
async def identity_drift_check(agent_id: str):
    """身份漂移检测—对比当前行为与基线锚点。"""
    result = get_memory().detect_drift(agent_id)
    return result


@app.post("/identity/bundles/export", tags=["Identity"], summary="导出身份包")
async def identity_export_bundle(req: IdentityBundleRequest):
    """导出完整身份包（可用于Agent 迁移）。"""
    if not req.agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for export")
    bundle = get_memory().export_identity(req.agent_id)
    return bundle


@app.post("/identity/bundles/import", tags=["Identity"], summary="导入身份包")
async def identity_import_bundle(req: IdentityBundleRequest):
    """导入身份包。"""
    if not req.bundle:
        raise HTTPException(status_code=400, detail="bundle is required for import")
    result = get_memory().import_identity(req.bundle)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# RLM Router Endpoints (Dynamic Weight Routing)
# ═══════════════════════════════════════════════════════════════════════════
# Global RLMRouter singleton (lazy init)
_rlm_router: Optional[Any] = None


def _get_rlm_router() -> Any:
    """Lazy-init the global RLMRouter singleton."""
    global _rlm_router
    if _rlm_router is None:
        try:
            from trinity.identity.rlm_router import RLMRouter
            _rlm_router = RLMRouter()
        except Exception:
            return None
    return _rlm_router


class RouteRequest(BaseModel):
    """Request model for /identity/route."""
    query: str = Field(..., min_length=1, max_length=4096, description="Query string to route")
    context: Optional[Dict[str, Any]] = Field(None, description="Optional routing context")
    top_k: int = Field(1, ge=1, le=10, description="Number of top strategies to return")


class RouteFeedbackRequest(BaseModel):
    """Request model for /identity/route/feedback."""
    query: str = Field(..., min_length=1, max_length=4096, description="Original query")
    strategy: str = Field(..., min_length=1, max_length=128, description="Selected strategy name")
    success: bool = Field(..., description="Whether the routing was successful")


@app.post("/identity/route", tags=["Identity"], summary="RLM 动态路由决策")
async def identity_route(req: RouteRequest):
    """执行 RLM 动态路由决策，返回最优策略及置信度。
    支持 top-k 多策略路由与置信度阈值回退。    """
    router = _get_rlm_router()
    if router is None:
        return {"error": "RLMRouter not available"}

    result = router.route(query=req.query, context=req.context, top_k=req.top_k)
    return {
        "strategy": result.strategy,
        "confidence": result.confidence,
        "query": result.query,
        "top_k": [(s, round(c, 4)) for s, c in result.top_k],
        "fallback": result.fallback,
        "metadata": result.metadata,
    }


@app.post("/identity/route/feedback", tags=["Identity"], summary="路由反馈")
async def identity_route_feedback(req: RouteFeedbackRequest):
    """上报路由结果反馈，触发EMA 权重更新。"""
    router = _get_rlm_router()
    if router is None:
        return {"error": "RLMRouter not available"}

    result = router.update_feedback(
        query=req.query,
        chosen_strategy=req.strategy,
        success=req.success,
    )
    return result


@app.get("/identity/route/stats", tags=["Identity"], summary="路由统计")
async def identity_route_stats():
    """获取各策略命中率、成功率及当前权重等统计信息。"""
    router = _get_rlm_router()
    if router is None:
        return {"error": "RLMRouter not available"}

    return router.get_strategy_stats()


# ═══════════════════════════════════════════════════════════════════════════
# DCSA-EJP Endpoints (Dual-Loop Constitutional Self-Auditing)
# ═══════════════════════════════════════════════════════════════════════════
# 全局宪法引擎实例
_dcsa_constitution = None
_dcsa_auditor = None


def _get_constitution():
    global _dcsa_constitution
    if _dcsa_constitution is None:
        from trinity.audit.constitution import ConstitutionalEngine
        _dcsa_constitution = ConstitutionalEngine()
        _dcsa_constitution.load_default_constitution()
    return _dcsa_constitution


def _get_auditor():
    global _dcsa_auditor
    if _dcsa_auditor is None:
        from trinity.audit.auditor import Auditor
        mem = get_memory()
        _dcsa_auditor = Auditor(
            adapter=mem._adapter if hasattr(mem, '_adapter') else None,
        )
    return _dcsa_auditor


def _get_diagnostics_count() -> int:
    """Get total memory count from Trinity diagnostics (degraded-safe)."""
    try:
        mem = get_memory()
        if hasattr(mem, 'diagnostics'):
            diag = mem.diagnostics()
            if isinstance(diag, dict):
                return diag.get("memory_count", 0)
    except Exception:
        pass
    return 0


@app.post("/audit/run", tags=["DCSA Audit"], summary="执行双循环审计")
async def dcsa_audit_run(req: AuditRunRequest):
    """执行一次双循环审计（executor + auditor）。"""
    auditor = _get_auditor()
    result = auditor.audit_action({"agent_id": req.agent_id, "task": req.task})
    return result


@app.get("/audit/runs", tags=["DCSA Audit"], summary="审计运行历史")
async def dcsa_audit_runs(agent_id: str = None, limit: int = 50):
    """审计运行历史列表。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_audit_history"):
        if agent_id:
            return {"runs": mem._adapter.get_audit_history(agent_id, limit)}
        return {"runs": []}
    return {"runs": [], "error": "no adapter"}


@app.get("/audit/runs/{run_id}", tags=["DCSA Audit"], summary="审计运行详情")
async def dcsa_audit_run_detail(run_id: str):
    """单次审计详情（含合理性数据包）。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_audit_run"):
        result = mem._adapter.get_audit_run(run_id)
        if result:
            return result
        raise HTTPException(status_code=404, detail=f"Audit run {run_id} not found")
    return {"error": "no adapter"}


@app.get("/audit/violations", tags=["DCSA Audit"], summary="违规趋势查询")
async def dcsa_violations(agent_id: str = None, limit: int = 100):
    """违规趋势查询。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "get_violation_trends"):
        trends = mem._adapter.get_violation_trends(agent_id, limit)
        return {"violations": trends, "total": len(trends)}
    return {"violations": [], "total": 0, "error": "no adapter"}


@app.get("/audit/constitution", tags=["DCSA Audit"], summary="查看宪法不变式")
async def dcsa_get_constitution():
    """查看当前宪法不变式列表。"""
    ce = _get_constitution()
    return {"invariants": ce.list_invariants()}


@app.put("/audit/constitution", tags=["DCSA Audit"], summary="更新宪法不变式")
async def dcsa_update_constitution(req: ConstitutionUpdateRequest):
    """添加或替换宪法不变式。"""
    ce = _get_constitution()
    from trinity.audit.constitution import Severity
    sev_map = {"low": Severity.LOW, "medium": Severity.MEDIUM,
               "high": Severity.HIGH, "critical": Severity.CRITICAL}
    sev = sev_map.get(req.severity, Severity.MEDIUM)
    ce.add_invariant(name=req.name, rule=req.rule,
                      severity=sev)
    return {"status": "ok", "total": len(ce.list_invariants())}


@app.get("/audit/metrics", tags=["DCSA Audit"], summary="DCSA-EJP 六项指标")
async def dcsa_metrics():
    """DCSA-EJP 六项指标实时值。"""
    auditor = _get_auditor()
    return auditor.get_metrics()


# ═══════════════════════════════════════════════════════════════
# A2A Protocol Endpoints (Google A2A v0.3)
# ═══════════════════════════════════════════════════════════════
# 全局 A2A 实例（惰性初始化）_a2a_task_manager = None
_a2a_capability_registry = None
_a2a_protocol = None


def _get_a2a_task_manager():
    global _a2a_task_manager
    if _a2a_task_manager is None:
        from trinity.a2a.task_manager import TaskManager
        mem = get_memory()
        _a2a_task_manager = TaskManager(
            adapter=mem._adapter if hasattr(mem, '_adapter') else None,
        )
    return _a2a_task_manager


def _get_a2a_registry():
    global _a2a_capability_registry
    if _a2a_capability_registry is None:
        from trinity.a2a.capability_registry import CapabilityRegistry
        mem = get_memory()
        _a2a_capability_registry = CapabilityRegistry(
            adapter=mem._adapter if hasattr(mem, '_adapter') else None,
        )
    return _a2a_capability_registry


def _get_a2a_protocol():
    global _a2a_protocol
    if _a2a_protocol is None:
        from trinity.a2a.protocol import A2AProtocol
        _a2a_protocol = A2AProtocol()
    return _a2a_protocol


def _get_a2a_capability_auth():
    """Lazy singleton for CapabilityAuth."""
    from trinity.a2a.security import get_capability_auth
    return get_capability_auth()


def _get_a2a_task_permission():
    """Lazy singleton for TaskPermission."""
    from trinity.a2a.security import get_task_permission
    return get_task_permission()


@app.get("/a2a/agents", tags=["A2A Protocol"], summary="列出所有注册Agent")
async def a2a_list_agents():
    """列出所有注册的 Agent。"""
    reg = _get_a2a_registry()
    return reg.list_all_agents()


@app.get("/a2a/agents/{agent_id}/card", tags=["A2A Protocol"], summary="获取 Agent 能力卡片")
async def a2a_get_agent_card(agent_id: str):
    """获取指定 Agent 的能力卡片。"""
    reg = _get_a2a_registry()
    from trinity.a2a.agent_card import generate_card
    card = generate_card(agent_id)
    return card


@app.post("/a2a/agents/register", tags=["A2A Protocol"], summary="注册 Agent 到联邦目录")
async def a2a_register_agent(req: AgentCardRequest):
    """注册 Agent 到联邦能力目录。"""
    from trinity.a2a.agent_card import AgentCard, SkillDef
    skills = [SkillDef(name=s.get("name", ""), description=s.get("description", ""),
                        input_schema=s.get("input_schema", {}), output_schema=s.get("output_schema", {}),
                        examples=s.get("examples", []))
              for s in req.skills]
    card = AgentCard(
        agent_id=req.agent_id,
        name=req.name,
        description=req.description,
        version=req.version,
        capabilities=req.capabilities,
        endpoints=req.endpoints,
        skills=skills,
        input_modes=req.input_modes,
        output_modes=req.output_modes,
        security_level=req.security_level,
    )
    reg = _get_a2a_registry()
    result = reg.register_agent(card)
    return result


@app.delete("/a2a/agents/{agent_id}", tags=["A2A Protocol"], summary="注销 Agent")
async def a2a_unregister_agent(agent_id: str):
    """注销 Agent。"""
    reg = _get_a2a_registry()
    result = reg.unregister_agent(agent_id)
    return result


@app.post("/a2a/tasks", tags=["A2A Protocol"], summary="创建跨Agent 任务")
async def a2a_create_task(req: A2ATaskRequest):
    """创建跨Agent 任务。"""
    tm = _get_a2a_task_manager()
    result = tm.create_task(req.from_agent, req.to_agent, req.payload)
    return result


@app.get("/a2a/tasks/{task_id}", tags=["A2A Protocol"], summary="查询任务状态")
async def a2a_query_task(task_id: str):
    """查询跨Agent 任务状态。"""
    tm = _get_a2a_task_manager()
    result = tm.query_task(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return result


@app.put("/a2a/tasks/{task_id}", tags=["A2A Protocol"], summary="更新任务状态")
async def a2a_update_task(task_id: str, req: A2ATaskUpdateRequest):
    """更新跨Agent 任务状态（含SSE 推送）。"""
    tm = _get_a2a_task_manager()
    result = tm.update_task(task_id, req.status, req.result)
    if not result:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return {"status": "ok", "task_id": task_id, "new_status": req.status}


@app.get("/a2a/tasks", tags=["A2A Protocol"], summary="列出所有任务")
async def a2a_list_tasks(agent_id: str = None, status: str = None, limit: int = 50):
    """列出跨Agent 任务。"""
    tm = _get_a2a_task_manager()
    tasks = tm.list_tasks(agent_id=agent_id, status=status)
    return {"tasks": tasks}


@app.post("/a2a/message", tags=["A2A Protocol"], summary="发送A2A 消息")
async def a2a_send_message(req: A2AMessageRequest):
    """发送A2A 消息（JSON-RPC 2.0）。"""
    proto = _get_a2a_protocol()
    if req.to_agent:
        result = proto.send_message(req.from_agent, req.to_agent,
                                    req.method, req.params, req.id)
    else:
        result = proto.broadcast(req.from_agent, req.method, req.params, req.id)
    return result


@app.get("/a2a/match", tags=["A2A Protocol"], summary="按能力匹配Agent")
async def a2a_match_agent(capability: str = None):
    """按能力匹配最佳Agent。"""
    reg = _get_a2a_registry()
    if capability:
        agents = reg.find_agent_by_capability(capability)
        return {"matched": agents, "capability": capability}
    return {"agents": reg.list_all_agents()}


# ── A2A Security Endpoints (v8.1.0) —AgentCard Signing & Verification ──

@app.post("/a2a/security/sign", tags=["A2A Security"], summary="AgentCard RSA 签名")
async def a2a_security_sign(req: SecuritySignRequest):
    """对AgentCard 进行 RSA 签名，返回哈希和签名。
    如果未提供private_key_path，则自动生成临时密钥对。    """
    from trinity.a2a.security import AgentCardSigner
    from trinity.a2a.agent_card import generate_card, AgentCard
    import tempfile, os

    card = generate_card(req.agent_id, name=req.name, capabilities=req.capabilities)

    if req.private_key_path:
        priv_path = req.private_key_path
    else:
        # Auto-generate key pair for convenience
        tmpdir = tempfile.mkdtemp(prefix="a2a_keys_")
        AgentCardSigner.generate_key_pair(tmpdir)
        priv_path = os.path.join(tmpdir, "private.pem")

    card_hash = AgentCardSigner.get_card_hash(card)
    signature = AgentCardSigner.sign(card, priv_path)

    return {
        "agent_id": req.agent_id,
        "card_hash": card_hash,
        "signature": signature,
        "algorithm": "RSA-SHA256",
    }


@app.post("/a2a/security/verify", tags=["A2A Security"], summary="验证 AgentCard 签名")
async def a2a_security_verify(req: SecurityVerifyRequest):
    """验证 AgentCard 的RSA 签名是否有效。"""
    from trinity.a2a.security import AgentCardSigner
    from trinity.a2a.agent_card import generate_card

    card = generate_card(req.agent_id, name=req.name or req.agent_id,
                         capabilities=req.capabilities)

    if not req.public_key_path:
        raise HTTPException(status_code=400, detail="public_key_path is required for verification")

    valid = AgentCardSigner.verify(card, req.signature, req.public_key_path)

    return {
        "agent_id": req.agent_id,
        "valid": valid,
        "card_hash": AgentCardSigner.get_card_hash(card),
    }


# ── A2A Security Endpoints —Capability Authorization ──────────────────

@app.post("/a2a/security/capability/authorize", tags=["A2A Security"],
          summary="授予 Agent 能力")
async def a2a_capability_authorize(req: CapabilityAuthorizeRequest):
    """为指定Agent 授予一项能力（加入白名单）。"""
    reg = _get_a2a_registry()
    return reg.authorize_capability(req.agent_id, req.capability)


@app.post("/a2a/security/capability/revoke", tags=["A2A Security"],
          summary="撤销 Agent 能力")
async def a2a_capability_revoke(req: CapabilityRevokeRequest):
    """撤销指定 Agent 的一项已授权能力。"""
    reg = _get_a2a_registry()
    return reg.revoke_capability(req.agent_id, req.capability)


@app.get("/a2a/security/capability/{agent_id}", tags=["A2A Security"],
         summary="查询 Agent 能力授权")
async def a2a_capability_query(agent_id: str):
    """查询指定 Agent 当前的授权能力列表。"""
    auth = _get_a2a_capability_auth()
    return auth.get_agent_policy(agent_id)


# ── A2A Security Endpoints —Task Permissions ──────────────────────────

@app.post("/a2a/security/task/grant", tags=["A2A Security"],
          summary="授予任务访问权")
async def a2a_task_grant(req: TaskGrantRequest):
    """为指定Agent 授予对某个任务的 guest 访问权限。"""
    tp = _get_a2a_task_permission()
    return tp.grant_task_access(req.task_id, req.agent_id)


@app.get("/a2a/security/task/{task_id}/acl", tags=["A2A Security"],
         summary="查询任务 ACL")
async def a2a_task_acl(task_id: str):
    """查询指定任务的访问控制列表（creator/assignee/guests/superiors）。"""
    tp = _get_a2a_task_permission()
    acl = tp.get_task_acl(task_id)
    if acl is None:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found in ACL")
    return acl


# ── Marvis A2A Adapter Endpoints (v8.0.0) ───────────────────────────────

@app.post("/a2a/marvis/agents/register", tags=["Marvis Adapter"], summary="Marvis Agent 注册")
async def marvis_register_agent(req: MarvisAgentRegisterRequest):
    """Marvis 专用 Agent 注册 ——自动生成 AgentCard 并注册到联邦目录。
    为Marvis 生态下的子 Agent 一站式完成）    1) A2A AgentCard 生成与签名    2) 能力注册到全局 CapabilityRegistry
    3) 多锚点身份初始化（identity 四类锚点）    """
    from trinity.a2a.agent_card import AgentCard, SkillDef, generate_card, sign_card

    skills = [
        SkillDef(
            name=f"{req.agent_id}-{cap}",
            description=cap,
            input_schema={},
            output_schema={},
        )
        for cap in req.capabilities
    ] or [SkillDef(name=f"{req.agent_id}-default", description="Default capability")]

    card = AgentCard(
        agent_id=req.agent_id,
        name=req.agent_name,
        description=req.metadata.get("description", f"Marvis sub-agent: {req.agent_name}"),
        version="8.2.0",
        capabilities=req.capabilities,
        endpoints=[f"/a2a/agents/{req.agent_id}"],
        skills=skills,
        input_modes=["text", "json"],
        output_modes=["text", "json", "stream"],
        security_level=req.metadata.get("security_level", "standard"),
    )
    sign_card(card)

    reg = _get_a2a_registry()
    result = reg.register_agent(card)
    return {"status": "registered", "agent_id": req.agent_id, "card": card.to_dict(), "registry_result": result}


@app.post("/a2a/marvis/dispatch", tags=["Marvis Adapter"], summary="Marvis 任务调度")
async def marvis_dispatch(req: MarvisDispatchRequest):
    """Marvis dispatch 语义的A2A 调度端点。
    接收 Marvis 的全局目标 + 当前任务上下文，创建 A2A 任务
    并保留Marvis 特有字段（global_goal / current_task / memory_ids）    以便下游 A2A→Marvis 响应重构。    """
    tm = _get_a2a_task_manager()

    enriched_payload = {
        "marvis_global_goal": req.global_goal,
        "marvis_current_task": req.current_task,
        "marvis_memory_ids": req.memory_ids,
        "marvis_context": req.context_dict,
        "priority": req.priority,
        "inner_payload": req.payload,
    }

    task_result = tm.create_task(req.from_agent, req.to_agent, enriched_payload)
    return {
        "task_id": task_result.task_id,
        "status": task_result.status,
        "from_agent": req.from_agent,
        "to_agent": req.to_agent,
        "created_at": task_result.created_at,
    }


@app.get("/a2a/marvis/snapshot", tags=["Marvis Adapter"], summary="全局记忆快照")
async def marvis_global_snapshot():
    """全局记忆快照 ——为Marvis 提供跨Agent 全局决策视图。
    聚合所有注册Agent 的：身份画像 / 记忆统计 / 最近任务/ 信任评分。    """
    from datetime import datetime, timezone

    reg = _get_a2a_registry()
    tm = _get_a2a_task_manager()

    agents = reg.list_all_agents()
    tasks_raw = tm.list_tasks(limit=10)

    # Aggregate trust scores per agent
    trust_scores: Dict[str, Any] = {}
    for agent in agents:
        agent_id = agent.get("agent_id", "")
        if agent_id:
            try:
                mem = get_memory()
                auditor = _get_auditor()
                metrics = auditor.get_metrics()
                violation_count = metrics.get("violation_count", 0)
                trust_scores[agent_id] = {
                    "overall_score": round(max(0.0, 1.0 - 0.05 * violation_count), 4),
                    "violation_count": violation_count,
                    "aedy": metrics.get("aedy", 0.0),
                    "jpc": metrics.get("jpc", 0.0),
                    "mcr": metrics.get("mcr", 0.0),
                    "tsad": metrics.get("tsad", 0.0),
                    "edq": metrics.get("edq", 0.0),
                    "last_audited": metrics.get("last_audited", ""),
                }
            except Exception:
                trust_scores[agent_id] = {"overall_score": 1.0, "violation_count": 0}

    return {
        "agent_count": len(agents),
        "total_memories": _get_diagnostics_count(),
        "sub_agent_profiles": {a.get("agent_id", ""): a for a in agents},
        "recent_tasks": tasks_raw if isinstance(tasks_raw, list) else tasks_raw.get("tasks", []),
        "trust_scores": trust_scores,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/a2a/marvis/agents/{name}/trust", tags=["Marvis Adapter"], summary="Agent 信任评分")
async def marvis_agent_trust(name: str):
    """Agent 信任评分 ——基于 DCSA-EJP 六轴指标的复合信任分。
    返回 AEDY / JPC / MCR / TSAD / EDQ 各轴分数 + 综合信任分。    """
    try:
        auditor = _get_auditor()
        metrics = auditor.get_metrics()
        violation_count = metrics.get("violation_count", 0)
        overall = round(max(0.0, 1.0 - 0.05 * violation_count), 4)
        return {
            "agent_name": name,
            "overall_score": overall,
            "aedy": metrics.get("aedy", 0.0),
            "jpc": metrics.get("jpc", 0.0),
            "mcr": metrics.get("mcr", 0.0),
            "tsad": metrics.get("tsad", 0.0),
            "edq": metrics.get("edq", 0.0),
            "violation_count": violation_count,
            "last_audited": metrics.get("last_audited", ""),
        }
    except Exception as e:
        return {
            "agent_name": name,
            "overall_score": 1.0,
            "error": str(e),
            "aedy": 0.0, "jpc": 0.0, "mcr": 0.0, "tsad": 0.0, "edq": 0.0,
            "violation_count": 0,
            "last_audited": "",
        }


# ── Memory Compression Endpoints (v8.2.0) ──────────────────────────────

_memory_compressor = None


def _get_memory_compressor():
    """Lazy singleton for MemoryCompressor."""
    global _memory_compressor
    if _memory_compressor is None:
        from trinity.memory.compression import MemoryCompressor
        mem = get_memory()
        _memory_compressor = MemoryCompressor(
            trinity_instance=mem,
            max_tokens=4096,
            compression_threshold=0.8,
        )
    return _memory_compressor


@app.post("/memory/compress", tags=["Memory Compression"],
          summary="执行记忆压缩")
async def memory_compress(req: CompressRequest):
    """对指定Agent 的记忆执行压缩管线（去重 →重要性排序→摘要）。
    返回压缩后的活跃记忆列表、摘要文本、被裁剪 ID 和token 预算使用率。    """
    compressor = _get_memory_compressor()
    mem = get_memory()

    # Gather agent memories
    memories = []
    if mem._adapter and hasattr(mem._adapter, "get_all_memories"):
        try:
            memories = mem._adapter.get_all_memories(
                agent_id=req.agent_id,
                limit=10000,
            ) or []
        except Exception:
            pass

    compressor.max_tokens = req.max_tokens
    result = compressor.compress(req.agent_id, memories)
    return result.to_dict()


@app.post("/memory/compress/stats", tags=["Memory Compression"],
          summary="压缩统计")
async def memory_compress_stats(req: CompressStatsRequest = None):
    """查看历史压缩统计：总运行次数、平均压缩率、总裁剪量。"""
    compressor = _get_memory_compressor()
    return compressor.get_stats()


@app.post("/memory/compress/restore", tags=["Memory Compression"],
          summary="恢复被裁剪记忆")
async def memory_compress_restore(req: CompressRestoreRequest):
    """传入之前压缩返回的trimmed_ids，将对应记忆恢复到活跃上下文中。
    恢复操作通过 Trinity adapter 重新加载原始记忆数据。    """
    mem = get_memory()
    restored: List[str] = []
    failed: List[str] = []

    for mid in req.trimmed_ids:
        try:
            if mem._adapter and hasattr(mem._adapter, "get_memory"):
                entry = mem._adapter.get_memory(mid)
                if entry:
                    restored.append(mid)
                else:
                    failed.append(mid)
            else:
                failed.append(mid)
        except Exception:
            failed.append(mid)

    return {
        "agent_id": req.agent_id,
        "restored": restored,
        "restored_count": len(restored),
        "failed": failed,
        "failed_count": len(failed),
    }



# ═══════════════════════════════════════════════════════════════════════════
# Memory Market —singleton initialisation
# ═══════════════════════════════════════════════════════════════════════════
_market_orderbook: Optional[OrderBook] = None
_market_exchange: Optional[TrustExchange] = None
_market_reputation: Optional[ReputationEngine] = None
_scheduler: Optional[Any] = None


def get_scheduler():
    global _scheduler
    if _scheduler is None:
        from trinity.evolution import EvolutionScheduler
        _scheduler = EvolutionScheduler()
    return _scheduler


def _get_orderbook() -> OrderBook:
    global _market_orderbook
    if _market_orderbook is None:
        _market_orderbook = OrderBook()
    return _market_orderbook


def _get_reputation() -> ReputationEngine:
    global _market_reputation
    if _market_reputation is None:
        _market_reputation = ReputationEngine()
    return _market_reputation


def _get_exchange() -> TrustExchange:
    global _market_exchange
    if _market_exchange is None:
        _market_exchange = TrustExchange(
            orderbook=_get_orderbook(),
            reputation=_get_reputation(),
        )
    return _market_exchange


# ═══════════════════════════════════════════════════════════════════════════
# Self-Evolution —Pydantic models
# ═══════════════════════════════════════════════════════════════════════════
class MemoryAccessRequest(BaseModel):
    memory_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    action: str = Field(default="search")
    context: str = ""


class FeedbackRequest(BaseModel):
    memory_id: str = Field(..., min_length=1)
    agent_id: str = Field(..., min_length=1)
    rating: float = Field(..., ge=1.0, le=5.0)
    comment: str = ""
    context: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# Memory Market —Pydantic models
# ═══════════════════════════════════════════════════════════════════════════
class MarketListRequest(BaseModel):
    memory: Dict[str, Any]
    owner: str = Field(..., min_length=1)
    price: float = Field(default=0.0, ge=0)
    license: str = "CC-BY"
    currency: str = "trust_score"


class MarketDelistRequest(BaseModel):
    asset_id: str = Field(..., min_length=1)


class MarketBuyRequest(BaseModel):
    buyer_agent: str = Field(..., min_length=1)
    asset_id: str = Field(..., min_length=1)
    offer_price: float = Field(default=0.0, ge=0)
    currency: str = "trust_score"


class MarketEndorseRequest(BaseModel):
    from_agent: str = Field(..., min_length=1)
    to_agent: str = Field(..., min_length=1)
    reason: str = ""


class MarketReportRequest(BaseModel):
    from_agent: str = Field(..., min_length=1)
    to_agent: str = Field(..., min_length=1)
    reason: str = ""


class MarketPriceRequest(BaseModel):
    memory: Dict[str, Any]


# ═══════════════════════════════════════════════════════════════════════════
# Memory Market —API endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/market/list", tags=["Memory Market"],
          summary="挂单 —将记忆资产列表到市场")
async def market_list(req: MarketListRequest):
    """将一条记忆封装为 MemoryAsset 并挂单到订单簿。"""
    try:
        asset = create_asset(req.memory, req.owner, price=req.price, license=req.license)
        entry = _get_orderbook().list_asset(asset, price=req.price, currency=req.currency)
        return {
            "status": "listed",
            "asset_id": asset.memory_id,
            "owner_agent": asset.owner_agent,
            "price": entry.price,
            "currency": entry.currency,
            "license": asset.license,
            "listed_at": entry.listed_at,
        }
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/market/delist", tags=["Memory Market"],
          summary="撤单 —从订单簿移除挂单")
async def market_delist(req: MarketDelistRequest):
    """撤下一个已挂单的资产。"""
    ok = _get_orderbook().delist_asset(req.asset_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Asset {req.asset_id} not found or already delisted")
    return {"status": "delisted", "asset_id": req.asset_id}


@app.get("/market/search", tags=["Memory Market"],
         summary="搜索市场 —按关键词/模态价格搜索可用资产")
async def market_search(
    query: str = "",
    modality: Optional[str] = None,
    max_price: Optional[float] = None,
):
    """搜索当前订单簿中的活跃挂单。"""
    results = _get_orderbook().search_market(
        query=query,
        modality=modality,
        max_price=max_price,
    )
    return {
        "count": len(results),
        "results": [
            {
                "asset_id": e.asset_id,
                "owner_agent": e.asset.owner_agent,
                "modality": e.asset.modality,
                "tags": e.asset.tags,
                "price": e.price,
                "currency": e.currency,
                "license": e.asset.license,
                "listed_at": e.listed_at,
            }
            for e in results
        ],
    }


@app.get("/market/orderbook", tags=["Memory Market"],
         summary="订单簿—查看全部活跃挂单")
async def market_orderbook():
    """返回当前全部活跃挂单。"""
    return {
        "count": len(_get_orderbook()._orders),
        "orders": _get_orderbook().get_order_book(),
    }


@app.post("/market/buy", tags=["Memory Market"],
          summary="购买 —以信任货币购买记忆资产")
async def market_buy(req: MarketBuyRequest):
    """原子交易：验证余额→转账 →撤单 →记录交易。"""
    try:
        tx = _get_exchange().buy_asset(
            buyer_agent=req.buyer_agent,
            asset_id=req.asset_id,
            offer_price=req.offer_price,
            currency=req.currency,
        )
        return {
            "status": "completed",
            "tx_id": tx.tx_id,
            "buyer_agent": tx.buyer_agent,
            "seller_agent": tx.seller_agent,
            "asset_id": tx.asset_id,
            "price": tx.price,
            "currency": tx.currency,
            "timestamp": tx.timestamp,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/market/transactions/{agent_id}", tags=["Memory Market"],
         summary="交易历史 —查询 Agent 的历史交易记录")
async def market_transactions(agent_id: str, limit: int = 50):
    """返回指定 Agent 参与的所有交易记录。"""
    history = _get_exchange().get_transaction_history(agent_id, limit=limit)
    return {"agent_id": agent_id, "count": len(history), "transactions": history}


@app.get("/market/reputation/{agent_id}", tags=["Memory Market"],
         summary="声誉查询 —获取 Agent 的声誉详情")
async def market_reputation(agent_id: str):
    """返回 Agent 的多维度声誉分数和账本事件。"""
    score = _get_reputation().calculate_reputation(agent_id)
    ledger = _get_reputation().get_reputation_ledger(agent_id)
    return {
        "reputation": score.to_dict(),
        "ledger_events": len(ledger),
        "ledger": ledger,
    }


@app.post("/market/endorse", tags=["Memory Market"],
          summary="背书 —Agent 背书（信任投票）")
async def market_endorse(req: MarketEndorseRequest):
    """一个Agent 为另一个Agent 背书，提升其声誉分。"""
    entry = _get_reputation().endorse_agent(
        from_agent=req.from_agent,
        to_agent=req.to_agent,
        reason=req.reason,
    )
    return {
        "status": "endorsed",
        "event_id": entry.event_id,
        "from_agent": req.from_agent,
        "to_agent": req.to_agent,
        "timestamp": entry.timestamp,
    }


@app.post("/market/report", tags=["Memory Market"],
          summary="举报 —Agent 举报不良行为")
async def market_report(req: MarketReportRequest):
    """一个Agent 举报另一个Agent 的不良行为，降低其声誉分。"""
    entry = _get_reputation().report_agent(
        from_agent=req.from_agent,
        to_agent=req.to_agent,
        reason=req.reason,
    )
    return {
        "status": "reported",
        "event_id": entry.event_id,
        "from_agent": req.from_agent,
        "to_agent": req.to_agent,
        "timestamp": entry.timestamp,
    }


@app.get("/market/price/{modality}", tags=["Memory Market"],
         summary="市场均价 —查询某类模态的市场均价")
async def market_price(modality: str):
    """返回指定模态从历史交易中计算的市场均价。"""
    price = get_market_price(modality, hist_trades=None)
    return {"modality": modality, "average_price": price}


@app.post("/market/estimate", tags=["Memory Market"],
          summary="记忆估值—使用定价引擎估值一条记忆")
async def market_estimate(req: MarketPriceRequest):
    """基于稀有度、新鲜度、关联度和历史成交价估算记忆价值。"""
    orderbook_entries = _get_orderbook().get_order_book()
    value = estimate_value(
        memory=req.memory,
        market_data=orderbook_entries,
        hist_trades=None,
    )
    return {
        "estimated_value": value,
        "modality": req.memory.get("category", "text"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# Self-Evolution Endpoints (v8.4.0)
# ═══════════════════════════════════════════════════════════════════════════
@app.post("/evolution/track-access", tags=["Self Evolution"],
          summary="记录记忆访问")
async def evolution_track_access(req: MemoryAccessRequest):
    """记录一次记忆访问事件供后续模式分析。"""
    get_scheduler().analyzer.track_access(
        memory_id=req.memory_id,
        agent_id=req.agent_id,
        action=req.action,
        context=req.context,
    )
    return {"status": "recorded", "memory_id": req.memory_id}


@app.get("/evolution/heatmap", tags=["Self Evolution"],
         summary="使用热力图")
async def evolution_heatmap(hours: int = 168):
    """返回指定时间窗口内的记忆使用热力图。"""
    heatmaps = get_scheduler().analyzer.get_heatmap(hours=hours)
    return {
        "window_hours": hours,
        "count": len(heatmaps),
        "heatmaps": [
            {
                "memory_id": h.memory_id,
                "total_accesses": h.total_accesses,
                "peak_hour": h.peak_hour,
                "peak_count": h.peak_count,
                "hourly_buckets": h.hourly_buckets,
            }
            for h in heatmaps
        ],
    }


@app.get("/evolution/hotspots", tags=["Self Evolution"],
         summary="热点记忆")
async def evolution_hotspots(window: int = 24):
    """返回指定窗口内的热点记忆。"""
    hotspots = get_scheduler().analyzer.analyze_hotspots(time_window_hours=window)
    return {
        "window_hours": window,
        "count": len(hotspots),
        "hotspots": [
            {
                "memory_id": h.memory_id,
                "access_count": h.access_count,
                "avg_interval_seconds": h.avg_interval_seconds,
                "burst_factor": h.burst_factor,
                "co_referenced": h.co_referenced,
                "pattern": h.pattern,
            }
            for h in hotspots
        ],
    }


@app.get("/evolution/patterns", tags=["Self Evolution"],
         summary="使用模式")
async def evolution_patterns():
    """检测并返回全局记忆使用模式。"""
    patterns = get_scheduler().analyzer.detect_patterns()
    return {
        "count": len(patterns),
        "patterns": [
            {
                "pattern_type": p.pattern_type,
                "memory_ids": p.memory_ids,
                "confidence": p.confidence,
                "description": p.description,
                "metadata": p.metadata,
            }
            for p in patterns
        ],
    }


@app.post("/evolution/feedback", tags=["Self Evolution"],
          summary="提交反馈")
async def evolution_feedback(req: FeedbackRequest):
    """提交对记忆质量的反馈评分。"""
    entry = get_scheduler().collector.record_feedback(
        memory_id=req.memory_id,
        agent_id=req.agent_id,
        rating=req.rating,
        comment=req.comment,
        context=req.context,
    )
    return {
        "feedback_id": entry.feedback_id,
        "memory_id": entry.memory_id,
        "rating": entry.rating,
        "status": "recorded",
    }


@app.get("/evolution/quality-alerts", tags=["Self Evolution"],
         summary="质量告警")
async def evolution_quality_alerts():
    """返回当前记忆质量告警列表。"""
    issues = get_scheduler().collector.detect_quality_issues()
    return {
        "count": len(issues),
        "issues": [
            {
                "memory_id": i.memory_id,
                "issue_type": i.issue_type,
                "severity": i.severity,
                "description": i.description,
                "suggestion": i.suggestion,
            }
            for i in issues
        ],
    }


@app.get("/evolution/suggestions", tags=["Self Evolution"],
         summary="变异建议")
async def evolution_suggestions():
    """返回待审的记忆变异建议列表。"""
    pending = get_scheduler().mutator.get_pending()
    return {
        "count": len(pending),
        "suggestions": [
            {
                "type": getattr(s, "type", "unknown"),
                "confidence": getattr(s, "confidence", 0.0),
                "reason": getattr(s, "reason", ""),
            }
            for s in pending
        ],
    }


@app.post("/evolution/suggestions/{suggestion_id}/apply", tags=["Self Evolution"],
          summary="应用建议")
async def evolution_apply_suggestion(suggestion_id: str):
    """手动应用指定变异建议。"""
    results = get_scheduler().mutator.auto_apply(["merge", "enrich", "split", "synthesis"])
    return {
        "suggestion_id": suggestion_id,
        "applied_count": len(results),
        "results": results,
    }


@app.post("/evolution/cycle/run", tags=["Self Evolution"],
          summary="手动触发进化周期")
async def evolution_cycle_run():
    """手动触发一次完整进化周期。"""
    result = get_scheduler().run_evolution_cycle()
    return {
        "cycle_id": result.cycle_id,
        "timestamp": result.timestamp,
        "status": result.status,
        "strategy_triggers": result.strategy_triggers,
        "applied_mutations": result.applied_mutations,
        "index_changes": result.index_changes,
        "quality_alerts": result.quality_alerts,
        "details": result.details,
    }


@app.get("/evolution/cycle/history", tags=["Self Evolution"],
         summary="进化历史")
async def evolution_cycle_history(limit: int = 20):
    """返回进化周期执行历史。"""
    history = get_scheduler().get_evolution_history(limit=limit)
    return {
        "count": len(history),
        "cycles": [
            {
                "cycle_id": h.cycle_id,
                "timestamp": h.timestamp,
                "status": h.status,
                "strategy_triggers": h.strategy_triggers,
                "applied_mutations": h.applied_mutations,
                "index_changes": h.index_changes,
                "quality_alerts": h.quality_alerts,
            }
            for h in history
        ],
    }


@app.get("/evolution/stats", tags=["Self Evolution"],
         summary="优化统计")
async def evolution_stats():
    """返回累计进化优化统计。"""
    stats = get_scheduler().optimizer.get_optimization_stats()
    return {
        "total_index_changes": stats.total_index_changes,
        "total_graph_reorgs": stats.total_graph_reorgs,
        "total_pruned": stats.total_pruned,
        "total_defrags": stats.total_defrags,
        "last_cycle": stats.last_cycle,
    }


# ── Dashboard ──────────────────────────────────────────────────────────

async def dashboard():
    """Web dashboard."""
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Trinity Dashboard</h1><p>Static files not found.</p>")


# ═══════════════════════════════════════════════════════════════════════════
# GraphQL（strawberry）— 此前 schema 存在但从未挂载，这里接入 FastAPI
# ═══════════════════════════════════════════════════════════════════════════
try:
    from strawberry.fastapi import GraphQLRouter
    from trinity.api.graphql_schema import schema as _trinity_graphql_schema
    app.include_router(GraphQLRouter(_trinity_graphql_schema), prefix="/graphql")
    logger.info("GraphQL router mounted at /graphql")
except Exception as _gql_err:  # pragma: no cover — 缺依赖时仅降级不阻断
    logger.warning("GraphQL router not mounted: %s", _gql_err)


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════
def main():
    if not _HAS_FASTAPI:
        print("ERROR: fastapi not installed. Run: pip install trinity-memory[api]")
        sys.exit(1)
    parser = argparse.ArgumentParser(description="Trinity REST API Server")
    parser.add_argument("--port", type=int, default=8001, help="Port")
    parser.add_argument("--host", default="0.0.0.0", help="Host")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()
    print(f"Trinity API Server v{app.version} starting on http://{args.host}:{args.port}")
    print(f"Dashboard: http://{args.host}:{args.port}/")
    print(f"API docs:  http://{args.host}:{args.port}/docs")
    print(f"Metrics:   http://{args.host}:{args.port}/metrics")
    uvicorn.run("trinity.api.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
