#!/usr/bin/env python3
"""
Trinity REST API Server — agent gateway / dashboard / coze bridge routes.
"""

import time
from typing import Any, Dict, List, Optional

from trinity.version import __version__ as TRINITY_VERSION  # 2026-09-01: 版本单一源

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from ._deps import (
    _live_aggregator as get_aggregator,
    _live_memory as get_memory,
)
from ._models import (
    BridgeInjectRequest,
    BulkMemoryWriteRequest,
    MemoryWriteRequest,
    RegisterRequest,
)
from ._routers_memories import search_memories

router = APIRouter()


@router.get("/agents/{agent_id}/biography")
async def get_agent_biography(agent_id: str):
    """自传式记忆（2026-09-01 大脑化层3）：per-agent 聚合视图。"""
    import json as _json
    import os as _os
    import sqlite3 as _sq
    _db = _os.path.expanduser("~/.trinity/store/trinity_store.db")
    _out = {"agent_id": agent_id}
    try:
        _conn = _sq.connect(_db, timeout=15)
        _conn.row_factory = _sq.Row
        _q = lambda sql, *p: _conn.execute(sql, p).fetchall()
        _out["sessions"] = _q("SELECT COUNT(*) c FROM dsh_sessions WHERE agent_id=?", agent_id)[0][0]
        _out["active_memories"] = _q("SELECT COUNT(*) c FROM memories WHERE agent_id=? AND status='active'", agent_id)[0][0]
        _out["categories"] = {r[0]: r[1] for r in _q(
            "SELECT category, COUNT(*) c FROM memories WHERE agent_id=? AND status='active' GROUP BY category ORDER BY 2 DESC LIMIT 15", agent_id)}
        _out["importance_dist"] = {str(r[0]): r[1] for r in _q(
            "SELECT ROUND(importance*10)/10 b, COUNT(*) c FROM memories WHERE agent_id=? AND status='active' GROUP BY b ORDER BY 1", agent_id)}
        _conn.close()
    except Exception as _e:
        _out["error"] = str(_e)
    # 2026-09-02（brain fix）：新会话 agent 无记忆时回退全库聚合视图，避免"自我空白"
    # （条件用 active_memories==0：会话自身已注册 dsh_sessions 计 1，但仍无"自我"）
    if "error" not in _out and _out.get("active_memories", 0) == 0:
        try:
            _conn2 = _sq.connect(_db, timeout=15)
            _conn2.row_factory = _sq.Row
            _q2 = lambda sql, *p: _conn2.execute(sql, p).fetchall()
            _out["_fallback"] = "all_agents"
            _out["total_agents"] = _q2("SELECT COUNT(DISTINCT agent_id) c FROM memories WHERE status='active'")[0][0]
            _out["sessions"] = _q2("SELECT COUNT(*) c FROM dsh_sessions")[0][0]
            _out["active_memories"] = _q2("SELECT COUNT(*) c FROM memories WHERE status='active'")[0][0]
            _out["categories"] = {r[0]: r[1] for r in _q2(
                "SELECT category, COUNT(*) c FROM memories WHERE status='active' GROUP BY category ORDER BY 2 DESC LIMIT 15")}
            _out["importance_dist"] = {str(r[0]): r[1] for r in _q2(
                "SELECT ROUND(importance*10)/10 b, COUNT(*) c FROM memories WHERE status='active' GROUP BY b ORDER BY 1")}
            _conn2.close()
        except Exception as _e2:
            _out["_fallback_error"] = str(_e2)
    try:
        _evo_path = _os.path.expanduser("~/.trinity/evolution_state.json")
        if _os.path.exists(_evo_path):
            _evo = _json.load(open(_evo_path, encoding="utf-8"))
            _out["preferences"] = {k: v for k, v in (_evo.get("active_preferences") or {}).items()}
    except Exception:
        pass
    return _out


@router.get("/agents/weights")
async def get_agent_weights():
    """查看所有Agent 权重配置。"""
    mem = get_memory()
    return {"weights": mem.get_agent_weights()}


@router.put("/agents/{agent_id}/weight")
async def set_agent_weight(agent_id: str, request: dict):
    """设置 Agent 检索权重。
    Body:
        weight: 权重值（建议 0.1-2.0）。    """
    mem = get_memory()
    weight = float(request["weight"])
    return mem.set_agent_weight(agent_id, weight)


@router.delete("/agents/{agent_id}/weight")
async def delete_agent_weight(agent_id: str):
    """删除 Agent 权重配置。"""
    mem = get_memory()
    ok = mem.delete_agent_weight(agent_id)
    return {"agent_id": agent_id, "deleted": ok}


@router.get("/api/stats")
async def get_stats():
    """Unified dashboard statistics."""
    from trinity.evolution import MetaEvolution
    evo = MetaEvolution()
    diag = get_memory().diagnostics()
    evo_diag = evo.diagnostics()
    return {"evolution": evo_diag, "adapter": diag.get("adapter", diag),
            "trinity_version": diag.get("trinity_version", "unknown")}


@router.get("/api/search")
async def search_api(q: str = Query(...), top_k: int = Query(10)):
    """Dashboard search."""
    return await search_memories(query=q, top_k=top_k)


@router.get("/", response_class=HTMLResponse)
# ── Coze Bot Bridge (unchanged) ───────────────────────────────────────
@router.post("/api/coze-bridge")
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


@router.get("/api/coze-bridge-intents")
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


@router.get("/api/coze-bridge/completions")
async def coze_completions(query: str, top_k: int = 5):
    result = bridge(query=query)
    texts = [r.get("content", "") for r in result.get("memory", [])]
    return {"results": texts}


@router.post("/agents/register")
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


@router.post("/agents/memory/write")
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


@router.post("/agents/memory/bulk_write")
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


@router.get("/agents/memory/search")
async def agent_memory_search(
    q: str = Query(..., description="Search query (semantic if embeddings available)"),
    top_k: int = Query(10, description="Number of results"),
    agent_id: Optional[str] = Query(None, description="Filter by source agent"),
    category: Optional[str] = Query(None, description="Filter by category"),
    scope: Optional[str] = Query(None, description="Filter by scope"),
    mode: str = Query("hybrid", description="Search mode: keyword / vector / hybrid"),
    use_embeddings: bool = Query(True, description="Use semantic embedding search (deprecated, use mode)"),
    include_archived: bool = Query(False, description="Include source-archived memories (default False — 检索面与引擎库 active 口径统一, 2026-08-24 R8 P0-1)"),
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
                source="api:/agents/memory/search",
                include_archived=include_archived,
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
            if not include_archived:
                all_dvs = [
                    dv for dv in all_dvs
                    if dv.source_status not in ("archived", "deleted")
                ]
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
    results = agg.query(filters, limit=top_k, source="api:/agents/memory/search")
    return {
        "query": q, "total": len(results),
        "method": "keyword",
        "results": [r.to_dict() for r in results],
    }


@router.get("/agents/memory/pool")
async def agent_memory_pool():
    """Get shared Aggregator pool statistics."""
    return get_aggregator().statistics()


@router.post("/agents/memory/feedback", tags=["Agents"], summary="RL 记忆反馈（Q 值强化）")
async def agent_memory_feedback(
    memory_id: str = Body(..., description="目标记忆 ID"),
    positive: bool = Body(True, description="True=用户确认/任务成功，False=纠正/任务失败"),
):
    """记录 RL 强化信号并更新记忆 Q 值（影响后续混合检索的排序微调）。

    对标 MemRL（arxiv.org/abs/2601.03192）：检索-使用-反馈闭环的在线更新。
    未注册的记忆先以默认语义分冷启动注册，再记录反馈。
    """
    agg = get_aggregator()
    try:
        r = agg.rl_feedback(memory_id, positive=positive)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"feedback failed: {exc}")
    return {"memory_id": memory_id, "positive": positive, **r}


@router.get("/agents/memory/cleanup")
async def agent_memory_cleanup():
    """Manually trigger expired memory cleanup (P0-2)."""
    agg = get_aggregator()
    removed = agg.cleanup()
    return {"status": "ok", "removed": removed}


@router.get("/agents/memory/insights")
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


@router.get("/agents/memory/degradation")
async def agent_degradation_status():
    """Current degradation tier and channel health (P1-4)."""
    try:
        aggr = get_aggregator()
        return JSONResponse(aggr._degradation.statistics())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/memory/degradation/reset")
async def agent_degradation_reset():
    """Reset degradation state to FULL (P1-4)."""
    try:
        aggr = get_aggregator()
        aggr._degradation.reset()
        return JSONResponse({"status": "reset", "tier": "full"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/agents/memory/consolidate")
async def agent_memory_consolidate(topic: Optional[str] = Query(None)):
    """Trigger offline memory consolidation (Auto-Dreamer v7.0.0)."""
    try:
        aggr = get_aggregator()
        merged = aggr.merge_memories(topic=topic)
        return JSONResponse({"merged": merged})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/memory/contradictions")
async def agent_memory_contradictions(topic: Optional[str] = Query(None)):
    """Detect contradictory memory pairs (SecondBrain CF v7.0.0)."""
    try:
        aggr = get_aggregator()
        contradictions = aggr.detect_contradictions(topic=topic)
        return JSONResponse({"contradictions": contradictions, "count": len(contradictions)})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/memory/export")
async def agent_memory_export(format: str = Query("readable", pattern="^(readable|json)$")):
    """Export all memories —readable text or raw JSON (Memsearch v7.0.0)."""
    try:
        aggr = get_aggregator()
        if format == "readable":
            content = aggr.export_readable()
            return PlainTextResponse(content, media_type="text/plain; charset=utf-8")
        else:
            # B4 修复: vars(dv) 含 set 等不可序列化属性 → 用 to_dict(full=True)
            return JSONResponse({"memories": [dv.to_dict(full=True) for dv in aggr._pool.values()]})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/dashboard")
async def trinity_dashboard():
    """Aggregated operations dashboard (v7.1.0)."""
    try:
        aggr = get_aggregator()
        obs = aggr._observability if hasattr(aggr, '_observability') else None
        dash = obs.dashboard() if obs else {}
        dash.update({
            "version": TRINITY_VERSION,  # 2026-09-01: 去硬编码（原 7.1.0）
            "retrieval_channels": aggr.statistics().get("retrieval_channels", {}),
            "pool_size": len(aggr._pool),
            "degradation": aggr._degradation.statistics() if hasattr(aggr, '_degradation') else {},
        })
        return JSONResponse(dash)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/benchmark")
async def run_benchmark():
    """Run memory benchmark suite (v7.1.0)."""
    try:
        aggr = get_aggregator()
        results = aggr.run_benchmark()
        return JSONResponse({"benchmark": results})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/agents/memory/stats/{memory_id}")
async def agent_memory_stats(memory_id: str):
    """Get access statistics for a single memory (P0-2)."""
    agg = get_aggregator()
    stats = agg.memory_stats(memory_id)
    if stats is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    return stats


@router.post("/agents/bridge/inject")
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


@router.get("/agents/bridge/extract")
async def agent_bridge_extract(
    agent_id: Optional[str] = Query(None), top_k: int = Query(5),
):
    """Bridge: extract post-dispatch context."""
    agg = get_aggregator()
    filters: Dict[str, Any] = {"scope": "cross_agent"}
    if agent_id:
        filters["source_agent"] = agent_id
    results = agg.query(filters, limit=top_k * 3, source="api:/agents/bridge/extract")
    bridge_entries = [
        r for r in results
        if hasattr(r, "metadata") and r.metadata and r.metadata.get("_source") == "marvis_bridge"
    ]
    all_entries = bridge_entries if bridge_entries else results
    sorted_entries = sorted(all_entries, key=lambda r: r.updated_at, reverse=True)[:top_k]
    return {"agent_id": agent_id, "total": len(sorted_entries),
            "entries": [r.to_dict() for r in sorted_entries]}


