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


if __name__ == "__main__":
    main()
