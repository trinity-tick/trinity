#!/usr/bin/env python3
"""
Trinity Bridge — Legacy import bridge for Marvis integration.
================================================================
This bridges the original trinity_call.py interface with the
new trinity package structure. Maintained for backward compatibility.

Usage (Marvis python_executor):
    import sys; sys.path.insert(0, r"<output_dir>")
    from trinity.core.bridge import trinity
    result = trinity("search", query="Alice OpenAI", top_k=5)
"""

import sys
import json
import os

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in [OUTPUT_DIR, os.path.dirname(OUTPUT_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

import importlib.util
_spec = importlib.util.spec_from_file_location(
    "trinity_mcp_server",
    os.path.join(OUTPUT_DIR, os.pardir, os.pardir, "modules", "second_brain", "engine.py")  # fallback chain
)

# Try to locate the trinity_mcp_server
_candidates = [
    os.path.join(OUTPUT_DIR, "..", "..", "modules", "second_brain", "engine.py"),
    os.path.join(OUTPUT_DIR, "..", "..", "..", "second_brain_v6_36.py"),
]
_candidate = None
for _c in _candidates:
    _p = os.path.normpath(os.path.join(OUTPUT_DIR, _c))
    if os.path.exists(_p):
        _candidate = _p
        break

if _candidate and os.path.exists(_candidate):
    _spec = importlib.util.spec_from_file_location("trinity_engine", _candidate)
    _trinity_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_trinity_mod)
else:
    _trinity_mod = None
    import warnings
    warnings.warn("Trinity engine module not found. Run `trinity diagnostics` to verify setup.")


def trinity(action: str, **kwargs):
    """
    Unified invocation entry point (legacy compatible).

    action:
        "search"        — query, top_k (default 10), use_all_channels (default True)
        "contradiction" — statement_a, statement_b
        "hopfield"      — memories (list[dict]), query
        "strategy"      — actions (list[str])
        "ingest"        — content, source_window, role, importance, tags
        "diagnostics"   — none
    """
    if _trinity_mod is None:
        return {"error": "Trinity engine not loaded", "hint": "pip install trinity-memory"}

    if action == "search":
        return _trinity_mod.trinity_search(
            query=kwargs.get("query", ""),
            top_k=kwargs.get("top_k", 10),
            use_all_channels=kwargs.get("use_all_channels", True)
        )
    elif action == "contradiction":
        return _trinity_mod.trinity_detect_contradiction(
            kwargs.get("statement_a", ""),
            kwargs.get("statement_b", "")
        )
    elif action == "hopfield":
        return _trinity_mod.trinity_hopfield_energy(
            memories=kwargs.get("memories", []),
            query=kwargs.get("query", "")
        )
    elif action == "strategy":
        return _trinity_mod.trinity_selfmem_strategy(
            kwargs.get("actions", [])
        )
    elif action == "ingest":
        return _trinity_mod.trinity_ingest(
            content=kwargs.get("content", ""),
            source_window=kwargs.get("source_window", ""),
            role=kwargs.get("role", "user"),
            importance=kwargs.get("importance", 0.5),
            tags=kwargs.get("tags", []),
        )
    elif action == "diagnostics":
        return _trinity_mod.trinity_diagnostics()
    else:
        return {"error": f"Unknown action: {action}", "valid_actions": [
            "search", "contradiction", "hopfield", "strategy", "ingest", "diagnostics"
        ]}
