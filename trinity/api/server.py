#!/usr/bin/env python3
"""
Trinity REST API Server — FastAPI-based.

Provides RESTful endpoints for all Trinity operations, plus a web dashboard.

Endpoints:
  GET    /health                    Health check
  GET    /diagnostics               System diagnostics
  POST   /memories                  Store a memory
  GET    /memories                  Search memories
  GET    /memories/{id}             Get memory by ID
  DELETE /memories/{id}             Soft-delete memory
  GET    /memories/{id}/versions    Get version chain
  GET    /personas/{pid}/memories   Get persona memories
  POST   /reason                    Open-domain reasoning
  POST   /embeddings                Embed single text
  POST   /embeddings/batch          Embed batch texts
  POST   /vector/search             Semantic vector search
  POST   /vector/index              Index memories to vector store
  GET    /                          Web dashboard (static files)

Usage:
    python -m trinity.api.server --port 8001
    uvicorn trinity.api.server:app --host 0.0.0.0 --port 8001
"""

import sys
import os
import json
import time
import argparse
from pathlib import Path
from typing import Any, Dict, List, Optional

from trinity import Trinity

# FastAPI imports (optional dependency)
try:
    from fastapi import FastAPI, HTTPException, Query, Body
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    FastAPI = object


# ── Create FastAPI app ───────────────────────────────────────────────────
app = FastAPI(
    title="Trinity Memory API",
    description="Trinity 三位一体记忆系统 REST API",
    version="1.0.0",
)

# Global Trinity instance
_memory: Optional[Trinity] = None


def get_memory() -> Trinity:
    global _memory
    if _memory is None:
        _memory = Trinity()
    return _memory


# ── Endpoints ────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "timestamp": time.time()}


@app.get("/diagnostics")
async def diagnostics():
    """Full system diagnostics."""
    return get_memory().diagnostics()


@app.post("/memories")
async def store_memory(
    content: str = Body(..., description="Memory content text"),
    persona_id: str = Body("default", description="Persona/user identifier"),
    session_id: Optional[str] = Body(None, description="Session identifier"),
    role: str = Body("user", description="Role: user/assistant/system"),
    importance: float = Body(0.5, description="Importance 0-1", ge=0, le=1),
    tags: Optional[List[str]] = Body(None, description="List of tags"),
    category: str = Body("general", description="Memory category"),
    tenant_id: str = Body("default", description="Tenant ID (multi-tenant)"),
):
    """Store a memory entry."""
    result = get_memory().ingest(
        content=content,
        persona_id=persona_id,
        session_id=session_id,
        role=role,
        importance=importance,
        tags=tags or [],
        category=category,
        tenant_id=tenant_id,
    )
    return result


@app.get("/memories")
async def search_memories(
    query: str = Query(..., description="Search query"),
    top_k: int = Query(10, description="Number of results"),
    persona_id: Optional[str] = Query(None, description="Filter by persona"),
    tenant_id: Optional[str] = Query(None, description="Filter by tenant"),
):
    """Search memories."""
    mem = get_memory()
    results = mem.search(
        query=query,
        top_k=top_k,
        persona_id=persona_id,
        tenant_id=tenant_id,
    )
    return {"query": query, "total": len(results), "results": results}


@app.get("/memories/{memory_id}")
async def get_memory_by_id(memory_id: str):
    """Get a single memory by ID."""
    mem = get_memory()
    result = mem.get_memory(memory_id) if hasattr(mem, 'get_memory') else None
    if result is None:
        # Try using adapter directly
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
        result = mem.delete_memory(memory_id)
        return {"deleted": result, "memory_id": memory_id}
    raise HTTPException(status_code=501, detail="delete_memory not implemented")


@app.get("/memories/{memory_id}/versions")
async def get_memory_versions(memory_id: str):
    """Get version/audit chain for a memory."""
    mem = get_memory()
    if hasattr(mem, 'get_version_chain'):
        return {"memory_id": memory_id, "versions": mem.get_version_chain(memory_id)}
    return {"memory_id": memory_id, "versions": []}


@app.get("/personas/{persona_id}/memories")
async def get_persona_memories(persona_id: str, limit: int = Query(50, le=200)):
    """Get all memories for a persona."""
    mem = get_memory()
    if hasattr(mem, 'get_persona_memories'):
        return {"persona_id": persona_id, "memories": mem.get_persona_memories(persona_id, limit)}
    raise HTTPException(status_code=501, detail="get_persona_memories not available")


@app.post("/reason")
async def reason(
    query: str = Body(..., description="Question to answer"),
    multi_hop: bool = Body(False, description="Use multi-hop expansion"),
    top_k: int = Body(5, description="Evidence per sub-query"),
):
    """Open-domain reasoning."""
    if not hasattr(get_memory(), 'reason'):
        raise HTTPException(status_code=501, detail="reason() not available")
    result = get_memory().reason(query=query, multi_hop=multi_hop, top_k=top_k)
    return result


# ── Embedding Endpoints ──────────────────────────────────────────────────

@app.post("/embeddings")
async def embed_text(
    text: str = Body(..., description="Text to embed"),
    backend: str = Body("auto", description="Embedding backend: auto/ollama/sklearn/hash"),
):
    """Generate semantic embedding for a single text.

    Returns the vector, dimension, model name, and L2 norm.
    Uses trinity.embeddings module (replaces old SHA-256 pseudo-embeddings).
    """
    try:
        from trinity.embeddings import create_engine
        import numpy as np

        engine = create_engine(backend=backend)
        vec = engine.embed(text)

        return {
            "text": text[:100],
            "dim": engine.embedding_dim(),
            "model": engine.model_name(),
            "embedding": vec.tolist(),
            "norm": float(np.linalg.norm(vec)),
        }
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"Embedding module not available: {e}. Ensure trinity.embeddings is installed."
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {e}")


@app.post("/embeddings/batch")
async def embed_texts(
    texts: List[str] = Body(..., description="Texts to embed"),
    backend: str = Body("auto", description="Embedding backend: auto/ollama/sklearn/hash"),
):
    """Generate semantic embeddings for multiple texts.

    Returns all vectors, dimensions, and model info.
    """
    try:
        from trinity.embeddings import create_engine

        if not texts:
            return {"count": 0, "dim": 0, "model": "none", "embeddings": []}

        engine = create_engine(backend=backend)
        vecs = engine.embed_batch(texts)

        return {
            "count": len(vecs),
            "dim": engine.embedding_dim(),
            "model": engine.model_name(),
            "embeddings": [v.tolist() for v in vecs],
        }
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"Embedding module not available: {e}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Batch embedding failed: {e}")


# ── Vector Search / Index Endpoints ─────────────────────────────────────

@app.post("/vector/search")
async def vector_search(
    query: str = Body(..., description="Search query"),
    top_k: int = Body(10, description="Number of results"),
    index_backend: str = Body("numpy", description="Index backend: numpy/faiss/annoy/chromadb"),
    embed_backend: str = Body("auto", description="Embedding backend: auto/ollama/sklearn/hash"),
):
    """Semantic vector search using real embeddings.

    Retrieves memories from the pool, embeds them, and performs
    vector similarity search. Returns ranked results with scores.
    """
    try:
        from trinity.embeddings import create_engine
        from trinity.vector_index import create_index
        import numpy as np

        eng = create_engine(backend=embed_backend)
        idx = create_index(backend=index_backend, dim=eng.embedding_dim())

        # Get memory pool from Trinity
        mem = get_memory()
        memories = []
        if hasattr(mem, '_adapter') and mem._adapter:
            try:
                if hasattr(mem._adapter, 'get_all_memories'):
                    memories = mem._adapter.get_all_memories(limit=200)
            except Exception:
                pass

        if not memories:
            return {
                "query": query,
                "total": 0,
                "results": [],
                "note": "No memories in pool. Index memories first via POST /vector/index.",
            }

        # Embed all memories
        texts = [m.get("content", "") for m in memories if m.get("content")]
        if not texts:
            return {
                "query": query,
                "total": 0,
                "results": [],
                "note": "No memory content found.",
            }

        vecs = eng.embed_batch(texts)
        for m, v in zip(memories, vecs):
            mid = m.get("memory_id", m.get("id", f"mem_{hash(str(m))}"))
            idx.add(mid, v, m)

        # Search
        qv = eng.embed(query)
        results = idx.search(qv, top_k=top_k)

        return {
            "query": query,
            "total": len(results),
            "model": eng.model_name(),
            "dim": eng.embedding_dim(),
            "index_backend": type(idx).__name__,
            "results": [
                {
                    "id": r.id,
                    "score": round(float(r.score), 4),
                    "metadata": r.metadata,
                }
                for r in results
            ],
        }
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"Required module not available: {e}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Vector search failed: {e}")


@app.post("/vector/index")
async def index_memories(
    backend: str = Body("auto", description="Embedding backend: auto/ollama/sklearn/hash"),
    force_reindex: bool = Body(False, description="Force re-index all memories"),
):
    """Index all memories into a vector store for fast semantic search.

    Embeds all existing memories and indexes them into a persistent
    or in-memory vector database. Uses ChromaDB if available.
    """
    try:
        from trinity.embeddings import create_engine
        import numpy as np

        eng = create_engine(backend=backend)
        mem = get_memory()

        # Get all memories
        memories = []
        if hasattr(mem, '_adapter') and mem._adapter:
            try:
                if hasattr(mem._adapter, 'get_all_memories'):
                    memories = mem._adapter.get_all_memories(limit=1000)
            except Exception:
                pass

        # Use ChromaDB if available, else in-memory index
        try:
            from trinity.vector_index import ChromaDBIndex
            idx = ChromaDBIndex(dim=eng.embedding_dim(), collection_name="trinity_api_search")
        except ImportError:
            from trinity.vector_index import create_index
            idx = create_index(backend="numpy", dim=eng.embedding_dim())

        indexed = 0
        errors = 0
        for m in memories:
            try:
                text = m.get("content", "")
                if not text:
                    continue
                mid = m.get("memory_id", m.get("id", f"mem_{indexed}"))
                vec = eng.embed(text)
                idx.add(mid, vec, m)
                indexed += 1
            except Exception:
                errors += 1

        return {
            "total_memories": len(memories),
            "indexed": indexed,
            "errors": errors,
            "model": eng.model_name(),
            "dim": eng.embedding_dim(),
            "index_backend": type(idx).__name__,
        }
    except ImportError as e:
        raise HTTPException(
            status_code=501,
            detail=f"Required module not available: {e}"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Memory indexing failed: {e}")


# ── Dashboard API Endpoints ────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    """Get unified dashboard statistics from evolution + adapter."""
    from trinity.evolution import MetaEvolution
    evo = MetaEvolution()
    diag = get_memory().diagnostics()
    evo_diag = evo.diagnostics()
    # Merge into a flat-ish response the frontend expects
    return {
        "evolution": evo_diag,
        "adapter": diag.get("adapter", diag),
        "trinity_version": diag.get("trinity_version", "unknown"),
    }


@app.get("/api/search")
async def search_api(q: str = Query(..., description="Search query"),
                     top_k: int = Query(10, description="Number of results")):
    """Search memories via the dashboard (thin wrapper around /memories)."""
    return await search_memories(query=q, top_k=top_k)


# ── Static files (Dashboard) ────────────────────────────────────────────

# Serve static files from the api/static directory
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
# ── Coze Bot Bridge ─────────────────────────────────────────────────────────────────────────────────
@app.post("/api/coze-bridge")
async def coze_bridge(req: dict):
    # 服务端直接调用搜索逻辑，不走 HTTP 回环
    from trinity.coze_bridge import search_memory_direct, search_entity, _search_by_intent_text
    
    memories = []
    intent_code = req.get("intent")
    brand = req.get("brand")
    query = req.get("query", "")
    
    # 1. 意图定向搜索
    if intent_code:
        intent_results = _search_by_intent_text(intent_code)
        memories.extend(intent_results)
    
    # 2. 关键词搜索补足
    if not memories or len(memories) < 3:
        direct_results = search_memory_direct(query, top_k=5)
        existing = {r.get("content","")[:50] for r in memories}
        for r in direct_results:
            if r["content"][:50] not in existing:
                memories.append(r)
    
    # 3. 知识图谱
    graph_data = []
    if brand:
        g = search_entity(brand)
        graph_data = g.get("entities", [])
    
    return {"memory": memories, "graph": graph_data, "intent": intent_code,
            "count": len(memories), "success": True}

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

async def dashboard():
    """Web dashboard homepage."""
    # Prefer dashboard.html, fall back to index.html
    html_path = Path(__file__).parent / "static" / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    # Fallback
    html_path = Path(__file__).parent / "static" / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Trinity Dashboard</h1><p>Static files not found.</p>")


# ── CLI entry point ─────────────────────────────────────────────────────

def main():
    """Start the API server."""
    if not _HAS_FASTAPI:
        print("ERROR: fastapi not installed. Run: pip install trinity-memory[api]")
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Trinity REST API Server")
    parser.add_argument("--port", type=int, default=8001, help="Port (default: 8001)")
    parser.add_argument("--host", default="0.0.0.0", help="Host (default: 0.0.0.0)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    args = parser.parse_args()

    print(f"Trinity API Server starting on http://{args.host}:{args.port}")
    print(f"Dashboard: http://{args.host}:{args.port}/")
    print(f"API docs:  http://{args.host}:{args.port}/docs")
    print(f"Stats:     http://{args.host}:{args.port}/api/stats")

    uvicorn.run(
        "trinity.api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
