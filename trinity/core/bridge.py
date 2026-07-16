#!/usr/bin/env python3
"""
Trinity Bridge — Unified entry point for Trinity engine.
=========================================================
Bridges the legacy trinity_call.py interface with the new trinity.core.client.Trinity API.
No longer re-loads engine.py separately — uses the canonical Trinity() instance.

Usage (Marvis python_executor):
    import sys; sys.path.insert(0, r"<project_root>")
    from trinity.core.bridge import trinity
    result = trinity("search", query="Alice OpenAI", top_k=5)
"""

import sys
import os

# Ensure project root is on path
PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
for _p in [PROJECT_ROOT, os.path.dirname(PROJECT_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from trinity.core.client import Trinity

# Singleton Trinity instance shared by all bridge calls
_TRINITY_INSTANCE: Trinity = None


def _get_trinity() -> Trinity:
    """Lazy-initialized singleton Trinity instance."""
    global _TRINITY_INSTANCE
    if _TRINITY_INSTANCE is None:
        _TRINITY_INSTANCE = Trinity()
    return _TRINITY_INSTANCE


def trinity(action: str, **kwargs) -> dict:
    """
    Unified invocation entry point (legacy compatible).

    Routes to Trinity() methods. If the engine is not available,
    falls back gracefully.

    action:
        "search"        -> query, top_k (default 10)
        "contradiction" -> statement_a, statement_b
        "hopfield"      -> memories (list[dict]), query
        "strategy"      -> actions (list[str])
        "ingest"        -> content, persona_id, role, importance, tags
        "diagnostics"   -> (no args)
    """
    try:
        mem = _get_trinity()
    except Exception as e:
        return {"error": f"Trinity engine failed to initialize: {e}",
                "hint": "pip install trinity-memory"}

    if action == "search":
        results = mem.search(
            query=kwargs.get("query", ""),
            top_k=kwargs.get("top_k", 10),
        )
        return {"results": results, "count": len(results)}

    elif action == "contradiction":
        return mem.detect_contradiction(
            kwargs.get("statement_a", ""),
            kwargs.get("statement_b", ""),
        )

    elif action == "hopfield":
        return mem.hopfield_energy(
            memories=kwargs.get("memories", []),
            query=kwargs.get("query", ""),
        )

    elif action == "strategy":
        return mem.selfmem_strategy(
            kwargs.get("actions", []),
        )

    elif action == "ingest":
        result = mem.ingest(
            content=kwargs.get("content", ""),
            persona_id=kwargs.get("persona_id", kwargs.get("role", "default")),
            tags=kwargs.get("tags", []),
            importance=kwargs.get("importance", 0.5),
        )
        return {"memory_id": result.get("memory_id", ""), "status": "stored"}

    elif action == "diagnostics":
        return mem.diagnostics()

    else:
        return {"error": f"Unknown action: {action}",
                "valid_actions": [
                    "search", "contradiction", "hopfield",
                    "strategy", "ingest", "diagnostics"
                ]}
