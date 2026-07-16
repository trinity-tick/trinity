"""
Trinity MCP Server — Model Context Protocol interface.

Supports stdio and SSE transport modes.
Compatible with MCP 1.0 (2026-06-24) protocol standard.

Exposed tools:
  - memory_search        Tri-signal semantic search (with session fallback)
  - memory_write         Write memory (CRDT versioned, SHA-256 audited)
  - memory_update        Update memory (conflict-preserving)
  - memory_delete        Soft delete memory
  - audit_query          SHA-256 provenance query
  - trinity_diagnostics  Full system diagnostics
  - memory_chronicle     Record event sequences (journal-style)
  - memory_tag_search    Search memories by tags

Exposed resources:
  - trinity://stats              System statistics
  - trinity://snapshot/{ts}      Point-in-time snapshot
  - trinity://health             Health check
  - sessions://list              List historical sessions
  - sessions://{id}              Get session detail
"""

import argparse
import logging
import sys
from typing import Any, Optional

from trinity.mcp.tools.memory_tools import register_memory_tools, set_session_recorder as tools_set_session_recorder
from trinity.mcp.resources.memory_resources import register_memory_resources, set_session_recorder as resources_set_session_recorder
from trinity.mcp.prompts.memory_prompts import register_memory_prompts
from trinity.session_recorder import ChatSessionRecorder

logger = logging.getLogger("trinity.mcp")

SERVER_NAME = "Trinity MCP Server"
SERVER_VERSION = "1.1.0"
DESCRIPTION = (
    "Trinity 三位一体记忆系统 MCP Server — "
    "提供记忆写入/检索/更新/删除及审计溯源能力。\n"
    "扩展功能：历史会话自动记录、全文搜索、标签搜索、事件序列记录。"
)


def _init_session_recorder() -> Any:
    """初始化 ChatSessionRecorder 并注入到 tools 和 resources 模块。

    Returns:
        ChatSessionRecorder 实例。
    """
    recorder = ChatSessionRecorder()
    logger.info("ChatSessionRecorder 已初始化，日志目录: %s", recorder.log_dir)

    # 注入到 tools 模块
    tools_set_session_recorder(recorder)

    # 注入到 resources 模块
    resources_set_session_recorder(recorder)

    return recorder


def create_server():
    """Create and configure FastMCP server instance."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("ERROR: mcp package not installed. Run: pip install trinity-memory[mcp]",
              file=sys.stderr)
        sys.exit(1)

    mcp = FastMCP(name=SERVER_NAME, instructions=DESCRIPTION)

    # 初始化会话记录器
    _init_session_recorder()

    # 注册所有工具、资源和提示
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
