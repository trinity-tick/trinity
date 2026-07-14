"""
Trinity CLI entry point.

Usage:
    python -m trinity search --query "Alice" --top-k 5
    python -m trinity ingest --content "user prefers dark mode"
    python -m trinity diagnostics
    python -m trinity mcp                    # MCP server (stdio)
    python -m trinity mcp --mode sse --port 8000
"""

import sys
import json
from trinity.cli import main

if __name__ == "__main__":
    main()
