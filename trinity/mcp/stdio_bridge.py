#!/usr/bin/env python3
"""Trinity MCP stdio bridge launcher - sets up sys.path before starting."""
import sys
import os

TRINITY_ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRINITY_ROOT)
sys.path.insert(0, os.path.join(TRINITY_ROOT, "trinity", "mcp"))

os.chdir(TRINITY_ROOT)

from trinity.mcp.server import create_server, SERVER_NAME, SERVER_VERSION
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("trinity_mcp")
logger.info("%s v%s starting in stdio mode (bridge).", SERVER_NAME, SERVER_VERSION)

mcp = create_server()
mcp.run(transport="stdio")
