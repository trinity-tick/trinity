#!/usr/bin/env python3
"""
Trinity REST API Server — retrieval routes (/memory/search/*, /embeddings, /vector/*, /reason).
"""

import os
from typing import List, Optional

from fastapi import APIRouter, Body, HTTPException

from ._deps import _live_memory as get_memory
from ._models import (
    CrossModalSearchRequest,
    HybridSearchRequest,
    ImageByTextRequest,
    TextByImageRequest,
)

router = APIRouter()


@router.post("/memory/search/hybrid")
async def hybrid_search(request: HybridSearchRequest):
    """混合检索—向量 + BM25 关键词+ 图谱融合。
    三种策略:
      - fusion:  加权求和 (vector=0.5, bm25=0.3, graph=0.2)
      - rrf:     Reciprocal Rank Fusion (rank-based, robust)
      - cascade: 向量粗排 →BM25 精排 →图谱扩充

    返回:
      results 中每条含 hybrid_score / vector_score / bm25_score / graph_score 明细，
      引擎库可查到的记忆附 content_preview（聚合池专属 id 保持仅分数，见 A1 评测修复）。    """
    mem = get_memory()
    data = mem.search_hybrid(
        query=request.query,
        top_k=request.top_k,
        strategy=request.strategy,
        agent_id=request.agent_id,
        persona_id=request.persona_id,
        tenant_id=request.tenant_id,
    )
    # A1 修复：为引擎库记忆回填 content_preview，避免调用方二次请求
    results = data.get("results", data if isinstance(data, list) else [])
    adapter = getattr(mem, "_adapter", None)
    for r in results:
        mid = r.get("memory_id")
        if mid and not r.get("content_preview") and adapter is not None:
            try:
                detail = adapter.get_memory(mid)
                if detail and detail.get("content"):
                    r["content_preview"] = detail["content"][:200]
            except Exception:
                pass
    return data


@router.post("/memory/search/cross-modal", tags=["Cross-Modal"])
async def cross_modal_search(request: CrossModalSearchRequest):
    """跨模态检索—自动检测输入类型并路由。
    支持:
      - auto:   自动检测query 是text / image / combined
      - text:   文字搜图片记忆(image_description)
      - image:  图片搜文字记忆(text)
      - combined: 联合检索（需 [text, image_path] 格式）    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    # A4 修复：无可用编码器（离线/模型缺失）时返回明确的降级响应，而非 500/挂起
    if getattr(cm, "_text_encoder", None) is None and not getattr(cm, "use_clip", False):
        return {"results": [], "query_type": request.query_type, "degraded": True,
                "detail": "CLIP/文本编码器不可用（离线或模型未缓存）；配置本地模型后可启用"}
    return cm.search_cross_modal(
        query=request.query,
        query_type=request.query_type,
        top_k=request.top_k,
    )


@router.post("/memory/search/image-by-text", tags=["Cross-Modal"])
async def image_by_text(request: ImageByTextRequest):
    """文搜图—用自然语言描述检索相关图片记忆。
    在image_description 模态记忆中做语义检索，返回最相关的图片描述    及其关联的图片文件路径。    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    if getattr(cm, "_text_encoder", None) is None and not getattr(cm, "use_clip", False):
        return {"results": [], "degraded": True, "detail": "文本编码器不可用（离线/模型未缓存）"}
    return mem.search_image_by_text(text=request.text, top_k=request.top_k)


@router.post("/memory/search/text-by-image", tags=["Cross-Modal"])
async def text_by_image(request: TextByImageRequest):
    """图搜文—用图片检索相关文字记忆。
    对传入的图片进行编码后，在text 模态记忆中做语义检索，
    返回与图片语义最相近的文字记忆。    """
    mem = get_memory()
    cm = mem._ensure_cross_modal_retriever()
    if getattr(cm, "_text_encoder", None) is None and not getattr(cm, "use_clip", False):
        return {"results": [], "degraded": True, "detail": "CLIP/文本编码器不可用（离线/模型未缓存）"}
    return mem.search_text_by_image(image_path=request.image_path, top_k=request.top_k)


@router.post("/reason")
async def reason(
    query: str = Body(...),
    multi_hop: bool = Body(False),
    top_k: int = Body(5),
    qtype: Optional[str] = Body(None, description="题型提示（multi-session/temporal-reasoning/single-session-preference…），用于策略路由"),
    question_date: Optional[str] = Body(None, description="问题日期 YYYY/MM/DD（temporal REL 计算用）"),
    route: bool = Body(False, description="走 RouteReasoner（已验证生成策略；需 DEEPSEEK_API_KEY）"),
):
    """Open-domain reasoning.

    2026-08-17 产品化: route=True（或环境 TRINITY_ROUTE_REASONER=on）时走
    RouteReasoner——multi→turn 粒度 / temporal→REL+inner2 / pref→两段式 /
    其他→dated plain；否则回退 OpenDomainReasoner。
    """
    mem = get_memory()
    if not hasattr(mem, 'reason'):
        raise HTTPException(status_code=501, detail="reason() not available")
    prev_route = os.environ.get("TRINITY_ROUTE_REASONER", "off")
    if route:
        os.environ["TRINITY_ROUTE_REASONER"] = "on"
    try:
        return mem.reason(
            query=query, multi_hop=multi_hop, top_k=top_k,
            qtype=qtype, question_date=question_date,
        )
    finally:
        if route:
            os.environ["TRINITY_ROUTE_REASONER"] = prev_route


@router.post("/embeddings")
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


@router.post("/embeddings/batch")
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


@router.post("/vector/search")
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


@router.post("/vector/index")
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


