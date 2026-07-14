"""
Trinity MCP Server — Model Context Protocol interface.

Supports stdio and SSE transport modes.
Compatible with MCP 1.0 (2026-06-24) protocol standard.

Exposed tools:
  - memory_search     Tri-signal semantic search
  - memory_write      Write memory (CRDT versioned, SHA-256 audited)
  - memory_update     Update memory (conflict-preserving)
  - memory_delete     Soft delete memory
  - audit_query       SHA-256 provenance query
"""

import argparse
import logging
import sys

from trinity.mcp.tools.memory_tools import register_memory_tools
from trinity.mcp.resources.memory_resources import register_memory_resources
from trinity.mcp.prompts.memory_prompts import register_memory_prompts

logger = logging.getLogger("trinity.mcp")

SERVER_NAME = "Trinity MCP Server"
SERVER_VERSION = "1.0.0"
DESCRIPTION = (
    "Trinity 三位一体记忆系统 MCP Server — "
    "提供记忆写入/检索/更新/删除及审计溯源能力。"
)


def create_server():
    """Create and configure FastMCP server instance."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("ERROR: mcp package not installed. Run: pip install trinity-memory[mcp]",
              file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP(name=SERVER_NAME, instructions=DESCRIPTION)
    register_memory_tools(mcp)
    register_memory_resources(mcp)
    register_memory_prompts(mcp)
    logger.info("Server '%s' v%s initialized.", SERVER_NAME, SERVER_VERSION)
    return mcp


def run_server(mode: str = "stdio", port: int = 8000, host: str = "127.0.0.1"):
    """Start the MCP server.

    Args:
        mode: Transport mode ("stdio" or "sse").
        port: SSE port (default: 8000).
        host: SSE host (default: 127.0.0.1).
    """
    mcp = create_server()

    if mode == "stdio":
        logger.info("Starting in stdio mode...")
        mcp.run(transport="stdio")
    else:
        mcp.settings.host = host
        mcp.settings.port = port
        logger.info("Starting in SSE mode on %s:%d...", host, port)
        mcp.run(transport="sse")


def main():
    """CLI entry point for MCP server."""
    parser = argparse.ArgumentParser(description=f"{SERVER_NAME} v{SERVER_VERSION}")
    parser.add_argument("--mode", choices=["stdio", "sse"], default="stdio")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )

    try:
        run_server(mode=args.mode, port=args.port, host=args.host)
    except KeyboardInterrupt:
        logger.info("Server shutdown.")
    except Exception as e:
        logger.exception("Server error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
