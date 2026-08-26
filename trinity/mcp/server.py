"""
Trinity MCP Server — Model Context Protocol interface.

Supports stdio, SSE and streamable-http transport modes.
Compatible with MCP 1.0 (2026-06-24) protocol standard.

Auth (2026-08-24, COMPARISON_VS_2026_SOTA_R7 P0-3):
  - streamable-http 模式默认启用 OAuth 2.1 资源服务器模式（Bearer token）：
    TRINITY_MCP_HTTP_AUTH=on（默认）/ off 关闭；token 取自
    TRINITY_MCP_API_KEY，未设置时回退 TRINITY_API_KEY。
    未配置任何 key 时自动降级为不启用鉴权（保持可用性）。
  - stdio / SSE 模式保持无鉴权（本地进程管道，MCP 生态约定）。

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
  - trinity://health            Health check
  - sessions://list              List historical sessions
  - sessions://{id}              Get session detail
"""

import argparse
import logging
import os
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


def _resolve_mcp_api_key() -> Optional[str]:
    """Resolve the bearer token for HTTP auth.

    Priority: TRINITY_MCP_API_KEY → TRINITY_API_KEY → GATEWAY_API_KEY
    (统一对外鉴权体系：Gateway 与 MCP HTTP 共用同一 key 时客户端一处配置即可)。
    Returns None when none is configured (auth degrades to disabled).
    """
    for name in ("TRINITY_MCP_API_KEY", "TRINITY_API_KEY", "GATEWAY_API_KEY"):
        val = os.environ.get(name, "").strip()
        if val:
            return val
    return None


class ApiKeyTokenVerifier:
    """OAuth 2.1 resource-server style Bearer token verifier.

    Implements the mcp TokenVerifier protocol: a request passes only when
    its ``Authorization: Bearer <token>`` matches the configured API key.
    Used by FastMCP's streamable-http auth middleware; the server also
    advertises protected-resource metadata (``.well-known`` routes) so
    OAuth-aware MCP clients can discover it.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key

    async def verify_token(self, token: str):
        if self._api_key and token == self._api_key:
            from mcp.server.auth.provider import AccessToken
            return AccessToken(
                token=token,
                client_id="trinity-mcp",
                scopes=["memory.read", "memory.write"],
                expires_at=None,
                subject="trinity-mcp-client",
            )
        return None


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


def create_server(auth_enabled: bool = False):
    """Create and configure FastMCP server instance.

    Args:
        auth_enabled: Enable OAuth 2.1 Bearer auth (resource-server mode)
            for the streamable-http transport. When True but no API key is
            configured, auth silently degrades to disabled (availability
            first). stdio/SSE transports never attach auth middleware.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError:
        print("ERROR: mcp package not installed. Run: pip install trinity-memory[mcp]",
              file=sys.stderr)
        sys.exit(1)

    mcp_kwargs: dict = {"name": SERVER_NAME, "instructions": DESCRIPTION}

    if auth_enabled:
        api_key = _resolve_mcp_api_key()
        if api_key:
            try:
                from mcp.server.fastmcp.server import AuthSettings
                mcp_kwargs["token_verifier"] = ApiKeyTokenVerifier(api_key)
                mcp_kwargs["auth"] = AuthSettings(
                    issuer_url="http://127.0.0.1:8003",
                    resource_server_url="http://127.0.0.1:8003",
                    required_scopes=["memory.read", "memory.write"],
                )
                logger.info("MCP streamable-http auth ENABLED (Bearer token; TRINITY_MCP_API_KEY/TRINITY_API_KEY)")
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("MCP auth setup failed, running without auth: %s", e)
        else:
            logger.warning(
                "MCP auth requested but no TRINITY_MCP_API_KEY/TRINITY_API_KEY set — "
                "running WITHOUT auth (degraded; set a key to enable)"
            )

    mcp = FastMCP(**mcp_kwargs)

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
        mode: Transport mode ("stdio" | "sse" | "streamable-http").
        port: HTTP/SSE port (default: 8000).
        host: Bind host (default: 127.0.0.1).
    """
    auth_enabled = False
    if mode == "streamable-http":
        # P0-3 (2026-08-24): HTTP 模式默认开启 Bearer 鉴权（OAuth 2.1 RS 模式），
        # TRINITY_MCP_HTTP_AUTH=off 可关闭；无 key 时 server 内部自动降级。
        auth_enabled = os.environ.get(
            "TRINITY_MCP_HTTP_AUTH", "on"
        ).strip().lower() in ("1", "on", "true", "yes")

    mcp = create_server(auth_enabled=auth_enabled)

    if mode == "stdio":
        logger.info("Starting in stdio mode...")
        mcp.run(transport="stdio")
    elif mode == "streamable-http":
        mcp.settings.host = host
        mcp.settings.port = port
        # MCP v2 (2026-08-15)：单端点 /mcp，streamable-http transport
        logger.info("Starting in streamable-http mode on %s:%d (mount /mcp)...", host, port)
        mcp.run(transport="streamable-http", mount_path="/mcp")
    else:
        mcp.settings.host = host
        mcp.settings.port = port
        logger.info("Starting in SSE mode on %s:%d...", host, port)
        mcp.run(transport="sse")


def main():
    """CLI entry point for MCP server."""
    parser = argparse.ArgumentParser(description=f"{SERVER_NAME} v{SERVER_VERSION}")
    parser.add_argument("--mode", choices=["stdio", "sse", "streamable-http"], default="stdio")
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
