#!/usr/bin/env python3
"""
Trinity MCP Server 启动脚本

Usage:
    python run_server.py --mode stdio
    python run_server.py --mode sse --port 8080
    python run_server.py --mode sse --host 0.0.0.0 --port 8080
"""

import sys
import os

# 确保包路径正确
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import argparse
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)

from server import create_server, SERVER_NAME, SERVER_VERSION

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=f"{SERVER_NAME} v{SERVER_VERSION} Launch Script",
    )
    parser.add_argument(
        "--mode",
        choices=["stdio", "sse"],
        default="stdio",
        help="传输模式: stdio / sse。默认 stdio。",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="SSE 监听端口。默认 8000。",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="SSE 监听地址。默认 127.0.0.1。",
    )
    args = parser.parse_args()

    mcp = create_server()

    if args.mode == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = args.port

    logger = logging.getLogger("trinity_mcp")

    if args.mode == "stdio":
        logger.info("%s v%s starting in stdio mode.", SERVER_NAME, SERVER_VERSION)
    else:
        logger.info(
            "%s v%s starting in SSE mode on %s:%d",
            SERVER_NAME, SERVER_VERSION, args.host, args.port,
        )

    try:
        if args.mode == "stdio":
            mcp.run(transport="stdio")
        else:
            mcp.run(transport="sse")
    except KeyboardInterrupt:
        logger.info("Server shutdown by user (Ctrl+C).")
    except Exception:
        logger.exception("Server terminated with error.")
        sys.exit(1)
