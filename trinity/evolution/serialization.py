"""
EvolutionStateSerializer — Cross-session state persistence.
Serializes full evolution state to JSON for:
  - Cross-window migration
  - Cross-platform transfer
  - Backup and restore
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

from trinity.evolution.core import MetaEvolution, EvolutionState


class EvolutionStateSerializer:
    """Handles serialization/deserialization of evolution state."""

    def __init__(self, state_dir: str = None):
        self.state_dir = state_dir or os.path.join(
            os.path.expanduser("~"), ".trinity", "evolution"
        )
        os.makedirs(self.state_dir, exist_ok=True)

    def save(self, evolution: MetaEvolution, name: str = "default") -> str:
        """Serialize evolution state to JSON file."""
        state = evolution.state
        data = {
            "version": state.version,
            "total_cycles": state.total_cycles,
            "last_cycle_id": state.last_cycle_id,
            "active_preferences": state.active_preferences,
            "active_patterns": state.active_patterns,
            "corrections_log": state.corrections_log[-200:],
            "skill_scores": state.skill_scores,
            "cycle_history": state.cycle_history[-100:],
            "created_at": state.created_at,
            "updated_at": time.time(),
            "_meta": {
                "serialized_at": time.time(),
                "source": "trinity.evolution",
                "version": "1.0",
            },
        }

        path = os.path.join(self.state_dir, f"evolution_{name}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return path

    def load(self, name: str = "default") -> Optional[EvolutionState]:
        """Deserialize evolution state from JSON file."""
        path = os.path.join(self.state_dir, f"evolution_{name}.json")
        if not os.path.exists(path):
            return None

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        return EvolutionState(**{k: v for k, v in data.items() if k != "_meta"})

    def list_snapshots(self) -> list:
        """List all available evolution snapshots."""
        snapshots = []
        for fn in os.listdir(self.state_dir):
            if fn.startswith("evolution_") and fn.endswith(".json"):
                path = os.path.join(self.state_dir, fn)
                snapshots.append({
                    "name": fn.replace("evolution_", "").replace(".json", ""),
                    "path": path,
                    "size": os.path.getsize(path),
                    "modified": os.path.getmtime(path),
                })
        return snapshots

    def export_for_cross_platform(self, evolution: MetaEvolution) -> Dict:
        """Export minimal state for cross-platform transfer."""
        state = evolution.state
        return {
            "_format": "trinity_evolution_v1",
            "total_cycles": state.total_cycles,
            "preferences": list(state.active_preferences.keys()),
            "patterns": list(state.active_patterns.keys()),
            "skill_scores": state.skill_scores,
            "corrections_count": len(state.corrections_log),
        }
