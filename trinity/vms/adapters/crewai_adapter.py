"""
Trinity VMS — CrewAI Adapter.

Bridges CrewAI tasks and crew-level shared memory to Trinity's VMS.

CrewAI Task      → TrinityTask
TrinityResult    → CrewAI TaskOutput
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from trinity.vms.adapter_base import (
    FrameworkAdapter,
    TrinityTask,
    TrinityResult,
)

logger = logging.getLogger(__name__)


class CrewAIAdapter(FrameworkAdapter):
    """Adapter for CrewAI multi-agent orchestration.

    Usage::

        from crewai import Task, Crew
        from trinity.vms import VMS
        from trinity.vms.adapters import CrewAIAdapter

        vms = VMS.from_defaults()
        adapter = vms.connect_adapter(framework="crewai")

        # Convert CrewAI task to Trinity task
        crew_task = Task(description="Analyze market data",
                         agent=analyst_agent)
        t_task = adapter.to_trinity_format("analyst", crew_task)

        # After VMS processing, convert back
        crew_output = adapter.from_trinity_format(trinity_result)
    """

    framework_name: str = "crewai"

    def to_trinity_format(self, agent_name: str, framework_task: Any) -> TrinityTask:
        """Convert a CrewAI Task to TrinityTask.

        Parameters
        ----------
        agent_name : str
            The Trinity agent name.
        framework_task : Any
            CrewAI Task object with description / expected_output fields.
        """
        import uuid
        from datetime import datetime, timezone

        if hasattr(framework_task, "description"):
            desc = getattr(framework_task, "description", "")
            expected = getattr(framework_task, "expected_output", "")
            payload = {"expected_output": expected}
        elif isinstance(framework_task, dict):
            desc = framework_task.get("description", "")
            payload = {"expected_output": framework_task.get("expected_output", "")}
        else:
            desc = str(framework_task)
            payload = {}

        return TrinityTask(
            task_id=str(uuid.uuid4()),
            description=desc,
            from_agent=agent_name,
            to_agent="crew",
            payload=payload,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def from_trinity_format(self, trinity_result: TrinityResult) -> Dict[str, Any]:
        """Convert TrinityResult to CrewAI TaskOutput shape.

        Returns a dict compatible with CrewAI's task output expectations.
        """
        output = trinity_result.output
        result_text = ""

        if isinstance(output, str):
            result_text = output
        elif isinstance(output, dict):
            result_text = output.get("result", output.get("output", str(output)))
        elif output is not None:
            result_text = str(output)

        return {
            "result": result_text,
            "status": trinity_result.status,
            "task_id": trinity_result.task_id,
            "memory_updates": len(trinity_result.memory_updates),
        }

    # ── Shared Memory Pool ────────────────────────────────────────────

    def share_memory(
        self,
        crew_id: str,
        agent_name: str,
        content: str,
        category: str = "crew_shared",
    ) -> Optional[Dict[str, Any]]:
        """Write to the crew-level shared memory pool.

        All agents in the crew can retrieve these memories via search
        with the same crew_id as tenant_id.

        Parameters
        ----------
        crew_id : str
            Crew identifier, mapped to Trinity tenant_id.
        agent_name : str
            Originating agent.
        content : str
            Memory content.
        category : str
            Memory category (default "crew_shared").

        Returns
        -------
        Memory metadata dict or None.
        """
        from trinity.vms.registry import get_registry
        store = get_registry().get("memory_store")
        if store is None:
            return None
        return store.add(
            content=content,
            agent_id=agent_name,
            tenant_id=crew_id,
            category=category,
        )

    def recall_shared_memories(
        self,
        crew_id: str,
        query: str,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search the crew-level shared memory pool.

        Parameters
        ----------
        crew_id : str
            Crew identifier (tenant_id).
        query : str
            Search query.
        top_k : int
            Max results.

        Returns
        -------
        List of memory dicts.
        """
        from trinity.vms.registry import get_registry
        store = get_registry().get("memory_store")
        if store is None:
            return []
        return store.search(query=query, tenant_id=crew_id, top_k=top_k)
