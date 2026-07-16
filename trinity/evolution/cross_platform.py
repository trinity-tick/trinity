"""
CrossPlatformAdapter — Enables Trinity to run across different agent platforms.

Strategy:
  1. State serialization: all evolution state saved as JSON files
  2. MCP protocol: standard interface for any MCP-compatible agent
  3. File-based handoff: write checkpoints that other agents can read
  4. Platform-agnostic format: no platform-specific dependencies
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from trinity.evolution.serialization import EvolutionStateSerializer


class CrossPlatformAdapter:
    """
    Adapter for running Trinity evolution across different platforms.

    Supported platforms:
      - Local (Goose/Claude Code): direct file access
      - MCP servers: via trinity.mcp.server
      - Custom agents: via JSON state files
    """

    def __init__(self, work_dir: str = None):
        self.work_dir = work_dir or os.path.join(
            os.path.expanduser("~"), ".trinity", "cross_platform"
        )
        os.makedirs(self.work_dir, exist_ok=True)
        self.serializer = EvolutionStateSerializer()

    def prepare_handoff(self, evolution_state: Dict) -> str:
        """Prepare a handoff file for another agent/window to pick up."""
        handoff = {
            "_format": "trinity_handoff_v1",
            "created_at": time.time(),
            "state": evolution_state,
            "instructions": [
                "1. Read this handoff file to restore evolution state",
                "2. Continue the Observe-Analyze-Plan-Execute-Certify loop",
                "3. Write updated state back to this file when done",
            ],
        }

        path = os.path.join(self.work_dir, f"handoff_{int(time.time())}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(handoff, f, indent=2, ensure_ascii=False)

        return path

    def read_handoff(self, path: str) -> Optional[Dict]:
        """Read a handoff file from another agent/window."""
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def list_handoffs(self) -> List[Dict]:
        """List all pending handoff files."""
        handoffs = []
        for fn in os.listdir(self.work_dir):
            if fn.startswith("handoff_") and fn.endswith(".json"):
                path = os.path.join(self.work_dir, fn)
                handoffs.append({
                    "file": fn,
                    "path": path,
                    "modified": os.path.getmtime(path),
                    "size": os.path.getsize(path),
                })
        return sorted(handoffs, key=lambda h: -h["modified"])

    def platform_diagnostics(self) -> Dict[str, Any]:
        """Report cross-platform readiness."""
        return {
            "module": "CrossPlatformAdapter",
            "work_dir": self.work_dir,
            "handoffs_available": len(self.list_handoffs()),
            "serializer_available": True,
            "mcp_compatible": True,
            "file_based": True,
            "session_independent": True,
        }
