"""
Trinity VMS (Virtual Memory System) — Pluggable memory infrastructure.

Provides standardised interfaces (Protocol classes) and a global
registry so that **any** Agent framework can connect to Trinity's
memory, identity, audit, task, search, and compression services.

Quick start::

    from trinity.vms import VMS

    vms = VMS.from_defaults()          # SQLite + hybrid search
    vms.connect_adapter(framework="langchain")

    store = vms.memory_store
    store.add("remember this", agent_id="agent-1")
    results = store.search("remember", top_k=5)

    vms.shutdown()
"""

from trinity.vms.core import VMS
from trinity.vms.registry import VMRegistry, get_registry
from trinity.vms.interfaces import (
    MemoryStore,
    IdentityProvider,
    Auditor,
    TaskBroker,
    SearchEngine,
    CompressionEngine,
)
from trinity.vms.adapter_base import (
    FrameworkAdapter,
    TrinityTask,
    TrinityResult,
)

__all__ = [
    # Core
    "VMS",
    "VMRegistry",
    "get_registry",
    # Protocols
    "MemoryStore",
    "IdentityProvider",
    "Auditor",
    "TaskBroker",
    "SearchEngine",
    "CompressionEngine",
    # Adapter base
    "FrameworkAdapter",
    "TrinityTask",
    "TrinityResult",
]
