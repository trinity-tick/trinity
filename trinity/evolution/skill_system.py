"""
SkillSystemAdapter — bridges self-improving/ directory with MetaEvolution.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class SkillSystemAdapter:
    """Adapter that reads/writes the self-improving/ skill directory.

    Maps:
      - memory.md       ↔ active_preferences + active_patterns
      - corrections.md  ↔ corrections_log
      - heartbeat-state.md ↔ cycle tracking
      - projects/*.md   ↔ project memory
    """

    def __init__(self, skill_dir: str = None):
        self.skill_dir = skill_dir or os.path.join(
            os.path.expanduser("~"), "self-improving"
        )
        os.makedirs(self.skill_dir, exist_ok=True)
        os.makedirs(os.path.join(self.skill_dir, "archive"), exist_ok=True)
        os.makedirs(os.path.join(self.skill_dir, "domains"), exist_ok=True)
        os.makedirs(os.path.join(self.skill_dir, "projects"), exist_ok=True)

    def read_memory(self) -> Dict[str, Any]:
        """Parse memory.md into structured data."""
        path = os.path.join(self.skill_dir, "memory.md")
        if not os.path.exists(path):
            return {"preferences": [], "patterns": [], "recent": []}

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        preferences = []
        patterns = []
        in_prefs = False
        in_patterns = False

        for line in content.split("\n"):
            if "## Confirmed Preferences" in line:
                in_prefs = True
                in_patterns = False
                continue
            if "## Active Patterns" in line:
                in_patterns = True
                in_prefs = False
                continue
            if line.startswith("## ") and "Preferences" not in line and "Patterns" not in line:
                in_prefs = False
                in_patterns = False
                continue

            stripped = line.strip()
            if stripped.startswith("- ") and in_prefs:
                preferences.append(stripped[2:])
            elif stripped.startswith("- ") and in_patterns:
                patterns.append(stripped[2:])

        return {"preferences": preferences, "patterns": patterns}

    def read_corrections(self) -> List[Dict]:
        """Parse corrections.md into structured records."""
        path = os.path.join(self.skill_dir, "corrections.md")
        if not os.path.exists(path):
            return []

        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

        corrections = []
        for line in content.split("\n"):
            if line.strip().startswith("- [") and "修正" in line:
                corrections.append({"line": line.strip(), "source": "corrections.md"})

        return corrections

    def write_memory(self, preferences: List[str], patterns: List[str]):
        """Write structured data back to memory.md."""
        lines = ["# Self-Improving Memory\n"]

        lines.append("\n## Confirmed Preferences\n")
        for p in preferences:
            lines.append(f"- {p}\n")

        lines.append("\n## Active Patterns\n")
        for p in patterns:
            lines.append(f"- {p}\n")

        lines.append(f"\n## Recent\n- Updated: {datetime.now().isoformat()}\n")

        path = os.path.join(self.skill_dir, "memory.md")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    def save_project_memory(self, project: str, content: str):
        """Save a project memory file."""
        path = os.path.join(self.skill_dir, "projects", f"{project}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def get_index(self) -> Dict[str, Any]:
        """Get current index.md content."""
        path = os.path.join(self.skill_dir, "index.md")
        if not os.path.exists(path):
            return {"files": [], "last_updated": None}
        return {"path": path, "exists": True}

    def diagnostics(self) -> Dict[str, Any]:
        files = []
        for root, dirs, filenames in os.walk(self.skill_dir):
            for fn in filenames:
                if fn.endswith(".md"):
                    fp = os.path.join(root, fn)
                    files.append({"name": fn, "size": os.path.getsize(fp)})
        return {
            "module": "SkillSystemAdapter",
            "skill_dir": self.skill_dir,
            "files_count": len(files),
            "files": files,
        }
