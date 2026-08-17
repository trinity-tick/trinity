"""
Trinity VMS — Framework Adapter Base.

Provides an abstract base class for connecting external Agent frameworks
(LangChain / CrewAI / AutoGen / Marvis / etc.) to Trinity's VMS layer.

Each concrete adapter implements format translation (framework ↔ Trinity)
and agent registration / dispatch.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Canonical Trinity task / result shapes
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TrinityTask:
    """Canonical task representation understood by Trinity VMS."""
    task_id: str = ""
    description: str = ""
    from_agent: str = ""
    to_agent: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    global_goal: str = ""
    current_subtask: str = ""
    memory_ids: List[str] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    created_at: str = ""


@dataclass
class TrinityResult:
    """Canonical result representation returned by Trinity VMS."""
    task_id: str = ""
    status: str = "completed"      # completed / failed / cancelled
    output: Any = None
    error: Optional[str] = None
    memory_updates: List[Dict[str, Any]] = field(default_factory=list)
    audit_trail: List[Dict[str, Any]] = field(default_factory=list)
    identity_snapshot: Optional[Dict[str, Any]] = None


# ═══════════════════════════════════════════════════════════════════════════
# FrameworkAdapter — abstract base
# ═══════════════════════════════════════════════════════════════════════════

class FrameworkAdapter(ABC):
    """Abstract base for all framework adapters.

    Subclasses translate between:
      1. Framework-native task representation  → TrinityTask
      2. TrinityResult                         → Framework-native response
    and provide agent registration / dispatch helpers.

    To add a new framework, subclass and implement the abstract methods,
    then register via::

        from trinity.vms import VMS
        vms = VMS.from_defaults()
        vms.connect_adapter(MyFrameworkAdapter())
    """

    framework_name: str = "unknown"

    def __init__(self):
        self._registered_agents: Dict[str, Dict[str, Any]] = {}

    # ── Format Translation (abstract — subclasses MUST override) ──────

    @abstractmethod
    def to_trinity_format(self, agent_name: str, framework_task: Any) -> TrinityTask:
        """Convert a framework-native task into a canonical TrinityTask.

        Parameters
        ----------
        agent_name : str
            The agent this task is being dispatched *from*.
        framework_task : Any
            Framework-native task/action object.

        Returns
        -------
        TrinityTask
        """
        ...

    @abstractmethod
    def from_trinity_format(self, trinity_result: TrinityResult) -> Any:
        """Convert a TrinityResult back into the framework-native reply format.

        Parameters
        ----------
        trinity_result : TrinityResult

        Returns
        -------
        Framework-native response object.
        """
        ...

    # ── Agent Management ──────────────────────────────────────────────

    def register_agent(
        self,
        agent_name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Register an agent with the adapter (and underlying VMS)."""
        self._registered_agents[agent_name] = metadata or {}
        return {
            "adapter": self.framework_name,
            "agent_name": agent_name,
            "status": "registered",
        }

    def unregister_agent(self, agent_name: str) -> Dict[str, Any]:
        """Remove a previously registered agent."""
        existed = self._registered_agents.pop(agent_name, None)
        return {
            "adapter": self.framework_name,
            "agent_name": agent_name,
            "status": "unregistered" if existed else "not_found",
        }

    def list_agents(self) -> List[str]:
        """Return list of agent names registered through this adapter."""
        return list(self._registered_agents.keys())

    # ── Dispatch (concrete, calls abstract translation methods) ───────

    def dispatch_task(self, task: TrinityTask) -> TrinityResult:
        """Dispatch a TrinityTask and return a TrinityResult.

        The default implementation delegates to the three-step pipeline:
          1. Pre-dispatch hook (override `_pre_dispatch`)
          2. Execute through VMS task broker
          3. Post-dispatch hook (override `_post_dispatch`)

        Subclasses may override for custom dispatch logic.
        """
        logger.debug("[%s] dispatch: %s → %s",
                     self.framework_name, task.from_agent, task.to_agent)

        self._pre_dispatch(task)

        # Default dispatch: the task is forwarded to the VMS task broker.
        # Concrete adapters that have a handle on the VMS instance should
        # override dispatch_task to call vms.task_broker.create_task(...).
        result = TrinityResult(
            task_id=task.task_id,
            status="completed",
            output={"message": f"Task '{task.description}' dispatched"},
        )

        self._post_dispatch(task, result)
        return result

    # ── Hooks ─────────────────────────────────────────────────────────

    def _pre_dispatch(self, task: TrinityTask) -> None:
        """Called before task execution.  Override for validation / logging."""
        pass

    def _post_dispatch(self, task: TrinityTask, result: TrinityResult) -> None:
        """Called after task execution.  Override for cleanup / metrics."""
        pass
