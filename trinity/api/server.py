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


# ── Static files (Dashboard) ────────────────────────────────────────────

# Serve static files from the api/static directory
_static_dir = Path(__file__).parent / "static"
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Web dashboard homepage."""
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

    uvicorn.run(
        "trinity.api.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


if __name__ == "__main__":
    main()
