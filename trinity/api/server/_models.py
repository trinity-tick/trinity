#!/usr/bin/env python3
"""
Trinity REST API Server — Pydantic request/response models.

Extracted verbatim from the former trinity/api/server.py monolith (v8.0.0+).
This module holds ONLY the BaseModel classes; it must not import from
trinity.api.server (no circular imports).
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

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

class HybridSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=32768, description="搜索查询字符串")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")
    strategy: str = Field("fusion", description="融合策略: fusion / rrf / cascade")
    agent_id: Optional[str] = Field(None, description="按Agent 过滤")
    persona_id: Optional[str] = Field(None, description="按角色过滤")
    tenant_id: Optional[str] = Field(None, description="按租户过滤")
    recall: bool = Field(False, description="2026-09 EXECUTION 105.11：为 True 时响应附加重建式回忆（按需深度加工）")

class CrossModalSearchRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=32768, description="文字查询或图片文件路径")
    query_type: str = Field("auto", description="查询类型: auto / text / image / combined")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")


class ImageByTextRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=32768, description="文字查询描述要找的图片")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")


class TextByImageRequest(BaseModel):
    image_path: str = Field(..., min_length=1, max_length=32768, description="查询图片的绝对路径")
    top_k: int = Field(10, ge=1, le=50, description="返回结果数量")

class RouteRequest(BaseModel):
    """Request model for /identity/route."""
    query: str = Field(..., min_length=1, max_length=32768, description="Query string to route")
    context: Optional[Dict[str, Any]] = Field(None, description="Optional routing context")
    top_k: int = Field(1, ge=1, le=10, description="Number of top strategies to return")


class RouteFeedbackRequest(BaseModel):
    """Request model for /identity/route/feedback."""
    query: str = Field(..., min_length=1, max_length=32768, description="Original query")
    strategy: str = Field(..., min_length=1, max_length=128, description="Selected strategy name")
    success: bool = Field(..., description="Whether the routing was successful")

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
