"""
Trinity VMS — AutoGen Adapter.

Bridges AutoGen's agent messages and GroupChat coordination to Trinity VMS.

AutoGen Message   → TrinityTask
TrinityResult     → AutoGen Reply
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


class AutoGenAdapter(FrameworkAdapter):
    """Adapter for Microsoft AutoGen conversational agents.

    Usage::

        from autogen import ConversableAgent
        from trinity.vms import VMS
        from trinity.vms.adapters import AutoGenAdapter

        vms = VMS.from_defaults()
        adapter = vms.connect_adapter(framework="autogen")

        # Convert AutoGen message dict to Trinity task
        msg = {"content": "What is the capital of France?",
               "role": "user", "name": "user"}
        task = adapter.to_trinity_format("assistant", msg)

        # After VMS processing, convert back
        reply = adapter.from_trinity_format(trinity_result)
    """

    framework_name: str = "autogen"

    def to_trinity_format(self, agent_name: str, framework_task: Any) -> TrinityTask:
        """Convert an AutoGen message dict to TrinityTask.

        Parameters
        ----------
        agent_name : str
            The Trinity agent name.
        framework_task : Any
            AutoGen message dict with content/role/name keys, or a
            ConversableAgent message object.
        """
        import uuid
        from datetime import datetime, timezone

        if isinstance(framework_task, dict):
            content = framework_task.get("content", "")
            role = framework_task.get("role", "user")
            sender = framework_task.get("name", agent_name)
            payload = {
                "role": role,
                "sender": sender,
            }
        elif hasattr(framework_task, "content"):
            content = str(getattr(framework_task, "content", ""))
            role = getattr(framework_task, "role", "user")
            sender = getattr(framework_task, "name", agent_name)
            payload = {"role": role, "sender": sender}
        else:
            content = str(framework_task)
            payload = {}

        return TrinityTask(
            task_id=str(uuid.uuid4()),
            description=content[:200],
            from_agent=agent_name,
            to_agent="autogen_group",
            payload=payload,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def from_trinity_format(self, trinity_result: TrinityResult) -> Dict[str, Any]:
        """Convert TrinityResult to an AutoGen-compatible reply dict.

        Returns a dict with content and role keys matching AutoGen's
        expected message format.
        """
        output = trinity_result.output

        content = ""
        if isinstance(output, str):
            content = output
        elif isinstance(output, dict):
            content = output.get("content", output.get("result", str(output)))
        elif output is not None:
            content = str(output)

        return {
            "content": content,
            "role": "assistant",
            "name": "trinity_vms",
            "trinity_task_id": trinity_result.task_id,
            "trinity_status": trinity_result.status,
        }

    # ── GroupChat Memory Coordination ─────────────────────────────────

    def register_groupchat(
        self,
        group_id: str,
        agent_names: List[str],
        shared_memory: bool = True,
    ) -> Dict[str, Any]:
        """Register a GroupChat with shared memory coordination.

        All agents in the group share a tenant-scoped memory pool.

        Parameters
        ----------
        group_id : str
            Group identifier (mapped to tenant_id).
        agent_names : List[str]
            Names of agents in this group.
        shared_memory : bool
            Enable shared memory pool for the group.
        """
        for name in agent_names:
            self.register_agent(name, {"group_id": group_id})

        return {
            "group_id": group_id,
            "agent_count": len(agent_names),
            "agents": agent_names,
            "shared_memory": shared_memory,
            "adapter": "autogen",
        }

    def record_group_message(
        self,
        group_id: str,
        sender: str,
        content: str,
    ) -> Optional[Dict[str, Any]]:
        """Record a GroupChat message into the shared Trinity memory pool.

        Parameters
        ----------
        group_id : str
            AutoGen group ID (tenant_id).
        sender : str
            Agent name that sent the message.
        content : str
            Message content.

        Returns
        -------
        Memory metadata or None.
        """
        from trinity.vms.registry import get_registry
        store = get_registry().get("memory_store")
        if store is None:
            return None
        return store.add(
            content=content,
            agent_id=sender,
            tenant_id=group_id,
            category="autogen_groupchat",
        )
