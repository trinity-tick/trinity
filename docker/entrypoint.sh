#!/bin/bash
# =============================================================================
# Trinity Docker Entrypoint
# =============================================================================
# Usage:
#   docker run trinity:latest                        -> MCP server (stdio mode)
#   docker run trinity:latest --mode sse --port 8000 -> MCP server (SSE mode)
#   docker run trinity:latest diagnostics            -> Run diagnostics
#   docker run trinity:latest search --query "..."   -> CLI search
#   docker run trinity:latest bench --name mock      -> Run benchmark
# =============================================================================

set -e

# Default: MCP server in stdio mode
if [ $# -eq 0 ]; then
    echo "Starting Trinity MCP server (stdio mode)..."
    exec python -m trinity mcp --mode stdio
fi

# Check for specific commands
case "$1" in
    mcp)
        shift
        echo "Starting Trinity MCP server..."
        exec python -m trinity mcp "$@"
        ;;
    diagnostics|search|ingest|bench)
        exec python -m trinity "$@"
        ;;
    api)
        echo "Starting Trinity REST API server..."
        exec python -m trinity.api.server "$@"
        ;;
    dashboard)
        echo "Starting Trinity Dashboard..."
        exec python -m trinity.api.dashboard "$@"
        ;;
    shell|bash|sh)
        exec "$@"
        ;;
    *)
        # Unknown command — try as trinity CLI subcommand
        exec python -m trinity "$@"
        ;;
esac
