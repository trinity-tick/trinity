"""
MarvisDispatch — Marvis dispatch semantics to A2A protocol translation layer.

Handles the bidirectional translation between Marvis's orchestration
primitives (dispatch_task / present_result) and Trinity's A2A protocol
(JSON-RPC 2.0 task lifecycle).

Key translations:
  - Marvis dispatch_task  → A2A pending task (with global_goal / current_task)
  - A2A completed task    → Marvis present_result (with memory_ids / agent_name)
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Marvis Dispatch Data Models
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class MarvisTaskRequest:
    """Marvis orchestration-level task dispatch request.

    Carries the full context that Marvis's main agent uses
    to delegate work to sub-agents.
    """
    global_goal: str = ""
    current_task: str = ""
    from_agent: str = "marvis-main"
    to_agent: str = ""
    task_description: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    memory_ids: List[str] = field(default_factory=list)
    context_dict: Dict[str, Any] = field(default_factory=dict)
    priority: int = 5                   # 1 (lowest) – 10 (highest)
    deadline_seconds: Optional[int] = None

    def to_a2a_payload(self) -> Dict[str, Any]:
        """Convert to A2A task creation payload."""
        return {
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "task_description": self.task_description,
            "payload": {
                "marvis_global_goal": self.global_goal,
                "marvis_current_task": self.current_task,
                "marvis_memory_ids": self.memory_ids,
                "marvis_context": self.context_dict,
                "priority": self.priority,
                "deadline_seconds": self.deadline_seconds,
                "inner_payload": self.payload,
            },
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MarvisTaskRequest":
        return cls(
            global_goal=d.get("global_goal", ""),
            current_task=d.get("current_task", ""),
            from_agent=d.get("from_agent", "marvis-main"),
            to_agent=d.get("to_agent", ""),
            task_description=d.get("task_description", ""),
            payload=d.get("payload", {}),
            memory_ids=d.get("memory_ids", []),
            context_dict=d.get("context_dict", {}),
            priority=d.get("priority", 5),
            deadline_seconds=d.get("deadline_seconds"),
        )


@dataclass
class MarvisTaskResponse:
    """Marvis-level task response, translated from A2A task result.

    Maps back to Marvis's present_result semantics with enriched
    metadata suitable for the orchestrator's decision loop.
    """
    task_id: str = ""
    agent_name: str = ""
    status: str = "unknown"            # pending / in_progress / completed / failed / cancelled
    global_goal: str = ""
    current_task: str = ""
    result: Optional[Dict[str, Any]] = None
    memory_ids: List[str] = field(default_factory=list)
    audit_summary: Optional[Dict[str, Any]] = None
    duration_ms: Optional[int] = None
    created_at: str = ""
    completed_at: str = ""

    def to_present_result(self) -> Dict[str, Any]:
        """Convert to Marvis present_result format."""
        return {
            "type": "sub_agent_result",
            "task_id": self.task_id,
            "agent_name": self.agent_name,
            "status": self.status,
            "global_goal": self.global_goal,
            "current_task": self.current_task,
            "result": self.result,
            "memory_ids": self.memory_ids,
            "audit_summary": self.audit_summary,
            "duration_ms": self.duration_ms,
            "timestamp": self.completed_at or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_a2a_result(
        cls,
        task_id: str,
        agent_name: str,
        a2a_result: Dict[str, Any],
        global_goal: str = "",
        current_task: str = "",
    ) -> "MarvisTaskResponse":
        """Construct from a raw A2A task query result."""
        payload = a2a_result.get("payload", {})
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}

        return cls(
            task_id=task_id,
            agent_name=agent_name,
            status=a2a_result.get("status", "unknown"),
            global_goal=global_goal,
            current_task=current_task,
            result=a2a_result.get("result"),
            memory_ids=payload.get("marvis_memory_ids", []),
            duration_ms=_parse_duration(a2a_result.get("created_at"), a2a_result.get("updated_at")),
            created_at=a2a_result.get("created_at", ""),
            completed_at=a2a_result.get("updated_at", ""),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Translation Functions
# ═══════════════════════════════════════════════════════════════════════════

def translate_marvis_to_a2a(request: MarvisTaskRequest) -> Dict[str, Any]:
    """Translate a Marvis dispatch request into an A2A task creation payload.

    Preserves Marvis-specific fields (global_goal, current_task, memory_ids)
    inside the A2A payload envelope for downstream A2A→Marvis reconstruction.

    Args:
        request: MarvisTaskRequest with full orchestration context.

    Returns:
        Dict suitable for POST /a2a/marvis/dispatch or POST /a2a/tasks.
    """
    if not request.to_agent:
        raise ValueError("MarvisTaskRequest.to_agent must be set")

    logger.debug(
        "Translating Marvis dispatch: %s → %s (%s)",
        request.from_agent, request.to_agent, request.current_task,
    )

    return {
        "from_agent": request.from_agent,
        "to_agent": request.to_agent,
        "task_description": request.task_description,
        "payload": request.to_a2a_payload()["payload"],
        "priority": request.priority,
        "deadline_seconds": request.deadline_seconds,
    }


def translate_a2a_to_marvis(
    a2a_result: Dict[str, Any],
    global_goal: str = "",
    current_task: str = "",
) -> MarvisTaskResponse:
    """Translate an A2A task result back into Marvis present_result semantics.

    Extracts Marvis-specific fields from the A2A payload envelope
    and enriches with audit context.

    Args:
        a2a_result:  Raw response from GET /a2a/tasks/{task_id}.
        global_goal: Original Marvis global goal (for context reconstruction).
        current_task: Original Marvis current task.

    Returns:
        MarvisTaskResponse ready for present_result consumption.
    """
    task_id = a2a_result.get("task_id", "")
    to_agent = a2a_result.get("to_agent", a2a_result.get("agent_name", "unknown"))

    response = MarvisTaskResponse.from_a2a_result(
        task_id=task_id,
        agent_name=to_agent,
        a2a_result=a2a_result,
        global_goal=global_goal,
        current_task=current_task,
    )

    logger.debug(
        "Translated A2A → Marvis: task=%s status=%s agent=%s",
        task_id, response.status, to_agent,
    )
    return response


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _parse_duration(start_str: str, end_str: str) -> Optional[int]:
    """Parse duration in milliseconds between two ISO timestamps."""
    if not start_str or not end_str:
        return None
    try:
        fmt = "%Y-%m-%dT%H:%M:%S"
        # Strip timezone suffix for naive parsing
        start_clean = start_str[:19]
        end_clean = end_str[:19]
        start = datetime.strptime(start_clean, fmt)
        end = datetime.strptime(end_clean, fmt)
        return int((end - start).total_seconds() * 1000)
    except (ValueError, IndexError):
        return None
