#!/usr/bin/env python3
"""
Trinity REST API Server — memory engine routes (/memories*, /personas/*, /graph/*).
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException, Query

from ._deps import (
    _live_aggregator as get_aggregator,
    _live_memory as get_memory,
)

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/memories")
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


@router.post("/memories/session", tags=["Memories"], summary="整段会话聚合写入为一条记忆")
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


@router.get("/memories")
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


@router.post("/memories/age")
async def age_memories():
    """手动触发老化扫描，清理TTL 过期的记忆（软删除）。"""
    mem = get_memory()
    return mem.age()


@router.get("/memories/stats")
async def memory_stats():
    """返回记忆统计（总数、过期数、Agent 分布、平均访问频率）。"""
    mem = get_memory()
    return mem.stats()


@router.get("/memories/modalities")
async def modality_stats():
    """返回各模态记忆数量、存储占比统计。"""
    mem = get_memory()
    return mem.modality_stats()


@router.post("/memories/{memory_id}/touch")
async def touch_memory(memory_id: str):
    """更新指定记忆的last_accessed_at 和access_count。"""
    mem = get_memory()
    ok = mem.touch(memory_id)
    return {"memory_id": memory_id, "touched": ok}


@router.get("/memories/{memory_id}/conflicts")
async def get_memory_conflicts(memory_id: str):
    """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。"""
    mem = get_memory()
    return mem.get_conflicts(memory_id)


@router.post("/memories/conflicts/resolve")
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


@router.get("/memories/dedup/stats")
async def dedup_stats():
    """返回去重统计信息（冲突组数、已解决数等）。"""
    mem = get_memory()
    return mem.dedup_stats()


@router.post("/memories/search")
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


@router.post("/memories/{memory_id}/links")
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


@router.get("/memories/{memory_id}/links")
async def get_memory_links(memory_id: str, min_strength: float = 0.0):
    """查看某记忆的完整关联网络（含双向链接）。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "get_all_links"):
        return mem._adapter.get_all_links(memory_id)
    raise HTTPException(status_code=501, detail="Not available without adapter")


@router.delete("/memories/links/{link_id}")
async def delete_memory_link(link_id: str):
    """删除指定链接。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "delete_memory_link"):
        ok = mem._adapter.delete_memory_link(link_id)
        return {"link_id": link_id, "deleted": ok}
    raise HTTPException(status_code=501, detail="Not available without adapter")


@router.put("/memories/links/{link_id}/strength")
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


@router.post("/graph/entities")
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


@router.get("/graph/entities/search")
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


@router.get("/graph/entities/{entity_id}")
async def get_entity(entity_id: str):
    """查询实体详情（含关联关系）。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "get_entity"):
        result = mem._adapter.get_entity(entity_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Entity not found")
        return result
    raise HTTPException(status_code=501, detail="Not available without adapter")


@router.post("/graph/relations")
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


@router.get("/graph/relations")
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


@router.get("/graph/traverse")
async def traverse_graph(
    start_id: str = Query(...),
    max_hops: int = Query(3),
):
    """多跳遍历子图。"""
    mem = get_memory()
    if hasattr(mem, "_adapter") and mem._adapter and hasattr(mem._adapter, "traverse"):
        return mem._adapter.traverse(start_id, max_hops=min(max_hops, 5))
    raise HTTPException(status_code=501, detail="Not available without adapter")


@router.get("/memories/{memory_id}")
async def get_memory_by_id(memory_id: str):
    """Get a single memory by ID."""
    mem = get_memory()
    result = None
    try:
        result = mem.get_memory(memory_id) if hasattr(mem, 'get_memory') else None
    except Exception:
        result = None
    if result is None:
        try:
            result = mem._adapter.get_memory(memory_id)
        except Exception:
            pass
    if result is None:
        # 聚合池记忆（mem_vid_*/mem_wms_* 等）不在引擎库时给出明确 404 提示
        raise HTTPException(status_code=404, detail="Memory not found (pool-only ids may not be fetchable here)")
    # 2026-08-16 修复:embedding 是 bytes(2048维向量),JSON 无法序列化
    # → base64 字符串化,避免 GET /memories/{id} 500 (utf-8 codec / not serializable)
    if isinstance(result, dict) and isinstance(result.get("embedding"), (bytes, bytearray)):
        import base64
        result["embedding"] = base64.b64encode(bytes(result["embedding"])).decode("ascii")
        result["embedding_encoding"] = "base64"
    return result


@router.delete("/memories/{memory_id}")
async def delete_memory(memory_id: str):
    """Soft-delete a memory."""
    mem = get_memory()
    deleted = False
    if hasattr(mem, 'delete_memory'):
        deleted = mem.delete_memory(memory_id)
    # 修复(2026-08-14): 删除需三方同步——引擎软删 + 聚合池移除 + BM25 索引移除，
    # 否则已删记忆仍会经聚合/BM25 通道被检索到（隐私泄漏）
    try:
        aggr = get_aggregator()
        if hasattr(aggr, "_remove_from_pool"):
            aggr._remove_from_pool(memory_id)
    except Exception:
        pass
    try:
        hr = getattr(mem, "_hybrid_retriever", None)
        if hr is not None and getattr(hr, "_bm25", None) is not None:
            hr._bm25.remove_document(memory_id)
    except Exception:
        pass
    return {"deleted": deleted, "memory_id": memory_id}


@router.get("/memories/{memory_id}/versions")
async def get_memory_versions(memory_id: str):
    """Get version/audit chain."""
    mem = get_memory()
    if hasattr(mem, 'get_version_chain'):
        return {"memory_id": memory_id, "versions": mem.get_version_chain(memory_id)}
    return {"memory_id": memory_id, "versions": []}


@router.get("/personas/{persona_id}/memories")
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


