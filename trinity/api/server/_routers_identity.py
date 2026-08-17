#!/usr/bin/env python3
"""
Trinity REST API Server — identity / RLM routing routes.
"""

import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ._deps import _live_memory as get_memory
from ._models import (
    IdentityAnchorRequest,
    IdentityBundleRequest,
    IdentityReconstructRequest,
    RouteFeedbackRequest,
    RouteRequest,
)

router = APIRouter()


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


@router.post("/identity/anchors", tags=["Identity"], summary="注册或更新身份锚点")
async def identity_register_anchor(req: IdentityAnchorRequest):
    """注册或更新身份锚点（幂等）。"""
    mem = get_memory()
    result = mem.register_identity_anchor(req.agent_id, req.anchor_type, req.content)
    return result


@router.post("/identity/register", tags=["Identity"], summary="Marvis 格式注册别名")
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


@router.get("/identity/profiles/{agent_id}", tags=["Identity"], summary="获取 Agent 身份画像（别名）")
async def identity_profile_alias(agent_id: str):
    """[Marvis Adapter alias] 获取 Agent 身份画像。"""
    try:
        profile = get_memory().get_identity_profile(agent_id)
        return profile
    except Exception as e:
        return {"error": str(e), "agent_id": agent_id}


@router.get("/identity/profiles", tags=["Identity"], summary="列出所有Agent 画像")
async def identity_profiles_list():
    """[Marvis Adapter alias] 列出所有Agent 画像。"""
    mem = get_memory()
    if mem._adapter and hasattr(mem._adapter, "list_agent_ids"):
        return {"profiles": mem._adapter.list_agent_ids()}
    return {"profiles": [], "error": "no adapter"}


@router.post("/identity/drift", tags=["Identity"], summary="触发漂移检测（别名）")
async def identity_drift_alias(req: dict = None):
    """[Marvis Adapter alias] 触发漂移检测。"""
    agent_id = (req or {}).get("agent_id", "")
    if not agent_id:
        return {"error": "agent_id required"}
    result = get_memory().detect_drift(agent_id)
    return result


@router.get("/identity/agents/{agent_id}/anchors", tags=["Identity"], summary="获取 Agent 的所有锚点")
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


@router.get("/identity/agents/{agent_id}/profile", tags=["Identity"], summary="获取完整身份画像")
async def identity_get_profile(agent_id: str):
    """获取完整身份画像（含一致性分数）。"""
    profile = get_memory().get_identity_profile(agent_id)
    return profile


@router.post("/identity/agents/{agent_id}/reconstruct", tags=["Identity"], summary="触发身份重建")
async def identity_reconstruct(agent_id: str, req: IdentityReconstructRequest = None):
    """触发身份重建，可选部分锚点重建（故障恢复）。"""
    if req and req.available_anchors:
        result = get_memory().reconstruct_identity(agent_id, req.available_anchors)
    else:
        result = get_memory().reconstruct_identity(agent_id)
    return result


@router.post("/identity/agents/{agent_id}/drift-check", tags=["Identity"], summary="身份漂移检测")
async def identity_drift_check(agent_id: str):
    """身份漂移检测—对比当前行为与基线锚点。"""
    result = get_memory().detect_drift(agent_id)
    return result


@router.post("/identity/bundles/export", tags=["Identity"], summary="导出身份包")
async def identity_export_bundle(req: IdentityBundleRequest):
    """导出完整身份包（可用于Agent 迁移）。"""
    if not req.agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for export")
    bundle = get_memory().export_identity(req.agent_id)
    return bundle


@router.post("/identity/bundles/import", tags=["Identity"], summary="导入身份包")
async def identity_import_bundle(req: IdentityBundleRequest):
    """导入身份包。"""
    if not req.bundle:
        raise HTTPException(status_code=400, detail="bundle is required for import")
    result = get_memory().import_identity(req.bundle)
    return result


@router.post("/identity/route", tags=["Identity"], summary="RLM 动态路由决策")
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


@router.post("/identity/route/feedback", tags=["Identity"], summary="路由反馈")
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


@router.get("/identity/route/stats", tags=["Identity"], summary="路由统计")
async def identity_route_stats():
    """获取各策略命中率、成功率及当前权重等统计信息。"""
    router = _get_rlm_router()
    if router is None:
        return {"error": "RLMRouter not available"}

    return router.get_strategy_stats()


