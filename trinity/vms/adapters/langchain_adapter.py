"""
Trinity VMS — LangChain Adapter.

Bridges LangChain Agent actions and memory to Trinity's VMS.
Supports BaseChatMemory → Trinity MemoryStore integration.

LangChain AgentAction  {"tool": "...", "tool_input": "...", "log": "..."}
LangChain AgentFinish  {"return_values": {"output": "..."}, "log": "..."}
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


class LangChainAdapter(FrameworkAdapter):
    """Adapter for LangChain agent frameworks.

    Usage::

        from langchain.agents import AgentExecutor
        from trinity.vms import VMS
        from trinity.vms.adapters import LangChainAdapter

        vms = VMS.from_defaults()
        adapter = vms.connect_adapter(framework="langchain")

        # Convert a LangChain action to a Trinity task
        action = AgentAction(tool="search", tool_input="weather",
                             log="Searching...")
        task = adapter.to_trinity_format("main-agent", action)

        # After VMS processing, convert back
        lc_result = adapter.from_trinity_format(trinity_result)
    """

    framework_name: str = "langchain"

    # ── Format Translation ────────────────────────────────────────────

    def to_trinity_format(self, agent_name: str, framework_task: Any) -> TrinityTask:
        """Convert a LangChain AgentAction / dict to TrinityTask.

        Parameters
        ----------
        agent_name : str
            The agent name in Trinity's namespace.
        framework_task : Any
            AgentAction instance or dict with tool/tool_input/log keys.
        """
        import uuid
        from datetime import datetime, timezone

        if hasattr(framework_task, "tool"):
            # AgentAction namedtuple
            desc = getattr(framework_task, "tool", "")
            payload = {"tool_input": getattr(framework_task, "tool_input", ""),
                       "log": getattr(framework_task, "log", "")}
        elif isinstance(framework_task, dict):
            desc = framework_task.get("tool", "")
            payload = {
                "tool_input": framework_task.get("tool_input", ""),
                "log": framework_task.get("log", ""),
            }
        else:
            desc = str(framework_task)
            payload = {}

        return TrinityTask(
            task_id=str(uuid.uuid4()),
            description=desc,
            from_agent=agent_name,
            to_agent="trinity",
            payload=payload,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def from_trinity_format(self, trinity_result: TrinityResult) -> Dict[str, Any]:
        """Convert a TrinityResult to a LangChain-compatible AgentFinish.

        Returns a dict with return_values and log keys, matching
        LangChain's AgentFinish structure.
        """
        output = trinity_result.output
        if isinstance(output, dict):
            return_values = output
        elif isinstance(output, str):
            return_values = {"output": output}
        else:
            return_values = {"output": str(output)}

        return {
            "return_values": return_values,
            "log": f"Trinity VMS task {trinity_result.task_id}: {trinity_result.status}",
        }

    # ── LangChain Memory Integration ──────────────────────────────────

    def to_trinity_memory(
        self,
        lc_memory: Any,
        agent_id: str = "default",
    ) -> List[Dict[str, Any]]:
        """Extract messages from a LangChain BaseChatMemory and store in Trinity.

        Parameters
        ----------
        lc_memory : Any
            A LangChain memory object (e.g. ConversationBufferMemory).
            Must have a `chat_memory` attribute with `messages` list.
        agent_id : str
            Trinity agent ID to associate memories with.

        Returns
        -------
        List of stored memory metadata dicts.
        """
        results: List[Dict[str, Any]] = []

        try:
            messages = getattr(lc_memory, "chat_memory", None)
            if messages is None or not hasattr(messages, "messages"):
                logger.debug("LangChainAdapter: memory has no chat_memory.messages")
                return results

            from trinity.vms.registry import get_registry
            store = get_registry().get("memory_store")
            if store is None:
                return results

            for msg in messages.messages:
                role = getattr(msg, "type", "human")
                content = getattr(msg, "content", "")
                result = store.add(
                    content=content,
                    agent_id=agent_id,
                    role=role,
                    category="langchain_chat",
                )
                results.append(result)
        except Exception as exc:
            logger.warning("LangChainAdapter: memory extraction failed: %s", exc)

        return results
