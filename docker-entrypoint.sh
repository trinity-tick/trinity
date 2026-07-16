#!/bin/bash
# ============================================================
# Trinity Docker 入口点
# 同时启动 API 服务和 MCP Server（SSE 模式）
# ============================================================
set -e

PORT=${PORT:-8100}
MCP_PORT=${MCP_PORT:-8000}
STORE_PATH=${STORE_PATH:-/data}

echo "============================================"
echo " Trinity Memory Server"
echo "============================================"
echo " API Port:   $PORT"
echo " MCP Port:   $MCP_PORT"
echo " Store Path: $STORE_PATH"
echo "============================================"

# 确保存储目录存在
mkdir -p "$STORE_PATH"/{data,logs,sessions}

# 启动 API Server（后台）
echo "[1/2] Starting API Server on port $PORT..."
python -m trinity.api.server --port "$PORT" --host "0.0.0.0" &
API_PID=$!

# 启动 MCP Server（SSE 模式，后台）
echo "[2/2] Starting MCP Server (SSE) on port $MCP_PORT..."
python -m trinity.mcp.server --mode sse --port "$MCP_PORT" --host "0.0.0.0" &
MCP_PID=$!

echo ""
echo " API:   http://0.0.0.0:$PORT"
echo " MCP:   http://0.0.0.0:$MCP_PORT/sse"
echo " Docs:  http://0.0.0.0:$PORT/docs"
echo ""

# 捕获退出信号
trap "echo 'Shutting down...'; kill $API_PID $MCP_PID 2>/dev/null; exit 0" SIGTERM SIGINT

# 等待任一进程退出
wait
