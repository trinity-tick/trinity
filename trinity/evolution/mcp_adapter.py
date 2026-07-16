"""
MCP Adapter — Exposes evolution as MCP tools and resources.
Enables any MCP-compatible agent to interact with the evolution system.
"""

from __future__ import annotations

from typing import Any, Dict

from trinity.evolution.core import MetaEvolution


def create_mcp_tools(evolution: MetaEvolution) -> list:
    """Generate MCP tool definitions for evolution system."""
    return [
        {
            "name": "evolution_tick",
            "description": "Execute one tick of the evolution loop",
            "input_schema": {
                "type": "object",
                "properties": {
                    "context": {
                        "type": "object",
                        "description": "Current session context for observation",
                    }
                },
            },
        },
        {
            "name": "evolution_diagnostics",
            "description": "Get evolution system diagnostics",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "evolution_save_state",
            "description": "Save evolution state for cross-session persistence",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "evolution_prepare_handoff",
            "description": "Prepare handoff for another agent/window",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]


def create_mcp_resources(evolution: MetaEvolution) -> list:
    """Generate MCP resource definitions."""
    return [
        {
            "uri": "evolution://state",
            "name": "Evolution State",
            "description": "Current evolution system state",
            "mime_type": "application/json",
        },
        {
            "uri": "evolution://diagnostics",
            "name": "Evolution Diagnostics",
            "description": "Full diagnostics of the evolution system",
            "mime_type": "application/json",
        },
    ]
