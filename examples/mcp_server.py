"""
Trinity MCP Server — start as a standalone MCP service.

Usage:
    python examples/mcp_server.py                # stdio mode (for MCP clients)
    python examples/mcp_server.py --mode sse     # SSE mode (HTTP)
    python examples/mcp_server.py --mode sse --port 8000
"""

import sys
sys.path.insert(0, "..")

from trinity.cli import main

if __name__ == "__main__":
    # Pass command-line args to the CLI's MCP subcommand
    if "--mode" not in sys.argv and "mcp" not in sys.argv:
        sys.argv.insert(1, "mcp")
    main()
