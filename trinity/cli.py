"""
Trinity CLI — unified command-line interface.

Supports:
  - search        Semantic search across all retrieval channels
  - ingest        Write memory (CRDT versioned)
  - diagnostics   Full system diagnostics
  - contradiction Contradiction detection
  - hopfield      Hopfield energy evaluation
  - strategy      SelfMem strategy execution
  - mcp           Start MCP server

Usage:
    trinity search --query "Alice" --top-k 5
    trinity ingest --content "..." --tags '["pref"]'
    trinity diagnostics
    trinity mcp [--mode sse --port 8000]
"""

import sys
import json
import argparse
from typing import Any, Dict, List, Optional


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        description="Trinity — 三位一体智能记忆系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version="Trinity v6.36.0")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── search ──────────────────────────────────────────────────────
    p_search = sub.add_parser("search", help="Semantic memory search")
    p_search.add_argument("--query", "-q", required=True, help="Search query")
    p_search.add_argument("--top-k", type=int, default=10, help="Number of results (default: 10)")
    p_search.add_argument("--mode", choices=["semantic", "graph", "exact", "hybrid"],
                          default="hybrid", help="Retrieval mode (default: hybrid)")

    # ── ingest ──────────────────────────────────────────────────────
    p_ingest = sub.add_parser("ingest", help="Write memory (CRDT versioned)")
    p_ingest.add_argument("--content", "-c", required=True, help="Memory content")
    p_ingest.add_argument("--source", "-s", default="", help="Source window identifier")
    p_ingest.add_argument("--role", default="user", help="Role: user/assistant/system")
    p_ingest.add_argument("--importance", type=float, default=0.5, help="Importance 0-1")
    p_ingest.add_argument("--tags", default="[]", help='JSON array of tags, e.g. ["pref"]')
    p_ingest.add_argument("--category", default="general", help="Memory category")

    # ── diagnostics ─────────────────────────────────────────────────
    sub.add_parser("diagnostics", help="Print full system diagnostics")

    # ── contradiction ───────────────────────────────────────────────
    p_contra = sub.add_parser("contradiction", help="Detect contradiction between statements")
    p_contra.add_argument("--a", required=True, help="Statement A")
    p_contra.add_argument("--b", required=True, help="Statement B")

    # ── hopfield ────────────────────────────────────────────────────
    p_hop = sub.add_parser("hopfield", help="Hopfield energy evaluation")
    p_hop.add_argument("--memories", required=True, help='JSON array of memory dicts')
    p_hop.add_argument("--query", required=True, help="Query text")

    # ── strategy ────────────────────────────────────────────────────
    p_strat = sub.add_parser("strategy", help="Execute SelfMem strategy")
    p_strat.add_argument("--actions", required=True,
                         help="Comma-separated: memory_read,rag_search,meta_log_read,memory_change,memory_review,declare_procedure")

    # ── mcp ─────────────────────────────────────────────────────────
    p_mcp = sub.add_parser("mcp", help="Start MCP server")
    p_mcp.add_argument("--mode", choices=["stdio", "sse"], default="stdio",
                       help="Transport mode (default: stdio)")
    p_mcp.add_argument("--port", type=int, default=8000, help="SSE port (default: 8000)")
    p_mcp.add_argument("--host", default="127.0.0.1", help="SSE host (default: 127.0.0.1)")

    # ── embed ───────────────────────────────────────────────────────
    p_embed = sub.add_parser("embed", help="Semantic embedding (v6.37+)")
    p_embed.add_argument("--text", "-t", required=True, help="Text to embed")
    p_embed.add_argument("--backend", choices=["auto", "ollama", "sklearn", "hash"],
                         default="auto", help="Embedding backend (default: auto)")
    p_embed.add_argument("--model", default=None, help="Ollama model name (default: bge-m3)")
    p_embed.add_argument("--compare", "-c", default=None, help="Compare with another text (cosine sim)")

    # ── vector ──────────────────────────────────────────────────────
    p_vec = sub.add_parser("vector", help="Vector index operations (v6.37+)")
    vec_sub = p_vec.add_subparsers(dest="vector_cmd", help="Vector sub-commands")

    p_vec_search = vec_sub.add_parser("search", help="Vector similarity search")
    p_vec_search.add_argument("--query", "-q", required=True, help="Search query")
    p_vec_search.add_argument("--top-k", type=int, default=10, help="Number of results")
    p_vec_search.add_argument("--backend", choices=["numpy", "faiss", "chromadb", "hybrid"],
                              default="numpy", help="Index backend")

    p_vec_list = vec_sub.add_parser("list", help="List available backends and info")

    # ── bench ───────────────────────────────────────────────────────
    p_bench = sub.add_parser("bench", help="Run benchmarks")
    p_bench.add_argument("--name", default="longmemeval",
                         help="Benchmark name (default: longmemeval)")
    p_bench.add_argument("--config", default="{}", help='JSON config overrides')

    return parser


def main(argv: Optional[List[str]] = None) -> None:
    """Main CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "mcp":
        _run_mcp(args)
        return

    if args.command == "embed":
        _run_embed(args)
        return

    if args.command == "vector":
        _run_vector(args)
        return

    # For all other commands, use the trinity_call bridge
    from trinity.core.client import Trinity
    memory = Trinity()

    if args.command == "search":
        result = memory.search(args.query, top_k=args.top_k, mode=args.mode)

    elif args.command == "ingest":
        import json as _json
        tags = _json.loads(args.tags)
        result = memory.ingest(
            content=args.content,
            source_window=args.source,
            role=args.role,
            importance=args.importance,
            tags=tags,
            category=args.category,
        )

    elif args.command == "diagnostics":
        result = memory.diagnostics()

    elif args.command == "contradiction":
        result = memory.detect_contradiction(args.a, args.b)

    elif args.command == "hopfield":
        import json as _json
        mems = _json.loads(args.memories)
        result = memory.hopfield_energy(mems, args.query)

    elif args.command == "strategy":
        actions = [a.strip() for a in args.actions.split(",")]
        result = memory.selfmem_strategy(actions)

    elif args.command == "bench":
        import json as _json
        config = _json.loads(args.config)
        result = memory.benchmark(args.name, config)

    else:
        print(f"Unknown command: {args.command}")
        sys.exit(1)

    # Pretty print result
    if isinstance(result, (dict, list)):
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(result)


def _run_mcp(args) -> None:
    """Start MCP server (delegated to trinity.mcp.server)."""
    from trinity.mcp.server import run_server
    run_server(mode=args.mode, port=args.port, host=args.host)


def _run_embed(args) -> None:
    """Generate semantic embedding for a text (v6.37+)."""
    from trinity.embeddings import create_engine
    import numpy as np

    model_kwargs = {}
    if args.model:
        model_kwargs["model"] = args.model

    engine = create_engine(backend=args.backend, use_cache=False, **model_kwargs)
    vec = engine.embed(args.text)

    result = {
        "text": args.text[:100],
        "model": engine.model_name(),
        "dim": engine.embedding_dim(),
        "norm": round(float(np.linalg.norm(vec)), 4),
        "embedding": [round(v, 6) for v in vec.tolist()[:8]] + ["..."],
    }

    if args.compare:
        vec2 = engine.embed(args.compare)
        sim = engine.cosine_similarity(vec, vec2)
        result["comparison"] = {
            "text_b": args.compare[:100],
            "cosine_similarity": round(float(sim), 4),
        }

    print(json.dumps(result, indent=2, ensure_ascii=False))


def _run_vector(args) -> None:
    """Vector index operations (v6.37+)."""
    if args.vector_cmd == "list":
        print(json.dumps({
            "available_backends": ["numpy", "faiss", "chromadb", "hybrid"],
            "recommended": "numpy (no extra deps) | chromadb (persistent) | faiss (fast GPU)",
            "usage": "trinity vector search --query 'text' --top-k 10 --backend numpy",
        }, indent=2))
        return

    if args.vector_cmd == "search":
        from trinity.embeddings import create_engine
        from trinity.vector_index import create_index

        engine = create_engine(backend="auto", use_cache=True)
        idx = create_index(backend=args.backend, dim=engine.embedding_dim())

        # Get all memories from Trinity
        from trinity.core.client import Trinity
        memory = Trinity()

        # Search using Trinity's own search first (for memory pool)
        mem_results = memory.search(args.query, top_k=args.top_k)

        result = {
            "query": args.query,
            "top_k": args.top_k,
            "embedding_model": engine.model_name(),
            "index_backend": args.backend,
            "results": [
                {
                    "score": round(r.get("score", r.get("final_score", 0)), 4) if isinstance(r, dict) else 0,
                    "content": (r.get("content", r.get("content_preview", ""))[:100] if isinstance(r, dict) else str(r)[:100]),
                }
                for r in mem_results
            ] if mem_results else [],
        }

        if not mem_results:
            result["note"] = "Using Trinity search. For vector-only search, use --vector flag in API."

        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    print(json.dumps({"error": f"Unknown vector sub-command: {args.vector_cmd}"}, indent=2))


if __name__ == "__main__":
    main()
