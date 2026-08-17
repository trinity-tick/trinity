"""
Trinity VMS — Virtual Memory System Protocol Interfaces.

All interfaces are defined as typing.Protocol classes — any object that
implements the matching methods can be used as a provider, without
requiring explicit inheritance. This enables zero-coupling plug-and-play
for any Agent framework.

Based on:  Letta-style virtual context management + OMEGA multi-modal
           memory + MCP-style protocol abstraction.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


# ═══════════════════════════════════════════════════════════════════════════
# MemoryStore — 记忆存储协议
# ═══════════════════════════════════════════════════════════════════════════

@runtime_checkable
class MemoryStore(Protocol):
    """Pluggable memory persistence backend.

    Any storage engine (SQLite / PostgreSQL / Redis / in-memory dict)
    that implements these methods can serve as Trinity's memory backend.
    """

    def add(
        self,
        content: str,
        agent_id: str = "default",
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
    ) -> Dict[str, Any]:
        """Store a new memory and return metadata (memory_id, timestamp, hash)."""
        ...

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve a single memory by ID."""
        ...

    def search(
        self,
        query: str,
        top_k: int = 10,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic / keyword search over stored memories."""
        ...

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        """Delete (or soft-delete) a memory. Returns success."""
        ...

    def count(
        self,
        agent_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> int:
        """Return total number of stored memories matching filters."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# IdentityProvider — 身份协议
# ═══════════════════════════════════════════════════════════════════════════

@runtime_checkable
class IdentityProvider(Protocol):
    """Agent identity management — multi-anchor identity architecture.

    Distributes identity across independent memory anchors rather than
    a single centralized profile.
    """

    def register(
        self, agent_id: str, anchor_type: str, content: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Register an identity anchor for the given agent."""
        ...

    def get_profile(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve the reconstructed identity profile for an agent."""
        ...

    def detect_drift(
        self, agent_id: str, recent_actions: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Detect identity drift from recent behaviour vs stored anchors."""
        ...

    def rebuild(self, agent_id: str) -> Dict[str, Any]:
        """Rebuild identity from all available anchors."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# Auditor  — 审计协议
# ═══════════════════════════════════════════════════════════════════════════

@runtime_checkable
class Auditor(Protocol):
    """Constitutional audit trail — DCSA-EJP dual-loop self-audit.

    All agent actions are checked against constitutional invariants and
    logged for transparency.
    """

    def audit(
        self,
        action: Dict[str, Any],
        agent_id: str = "default",
    ) -> Dict[str, Any]:
        """Audit a single action against constitutional invariants.

        Returns dict with overall_result, violations, justification_packet.
        """
        ...

    def get_violations(
        self,
        agent_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Retrieve recent constitutional violations."""
        ...

    def get_trust_score(self, agent_id: str) -> float:
        """Return 0.0–1.0 trust score based on audit history."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# TaskBroker  — 任务调度协议
# ═══════════════════════════════════════════════════════════════════════════

@runtime_checkable
class TaskBroker(Protocol):
    """Agent-to-Agent task dispatch and lifecycle management."""

    def create_task(
        self,
        description: str,
        from_agent: str,
        to_agent: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: int = 0,
    ) -> Dict[str, Any]:
        """Create and register a new task between agents."""
        ...

    def query_task(self, task_id: str, agent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Query task status.  agent_id is used for permission check."""
        ...

    def cancel_task(self, task_id: str, agent_id: str) -> bool:
        """Cancel a pending or in-progress task (creator only)."""
        ...

    def list_tasks(
        self,
        agent_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """List tasks with optional filtering."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# SearchEngine  — 检索引擎协议
# ═══════════════════════════════════════════════════════════════════════════

@runtime_checkable
class SearchEngine(Protocol):
    """Pluggable retrieval strategy — vector / BM25 / graph / cross-modal."""

    def search(
        self,
        query: str,
        top_k: int = 10,
        agent_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Baseline semantic / vector search."""
        ...

    def hybrid_search(
        self,
        query: str,
        top_k: int = 10,
        agent_id: Optional[str] = None,
        strategy: str = "fusion",
    ) -> List[Dict[str, Any]]:
        """Multi-signal fusion search (vector + BM25 + graph)."""
        ...

    def cross_modal_search(
        self,
        query: Any,
        query_type: str = "auto",
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Cross-modal search (text↔image)."""
        ...


# ═══════════════════════════════════════════════════════════════════════════
# CompressionEngine  — 压缩协议
# ═══════════════════════════════════════════════════════════════════════════

@runtime_checkable
class CompressionEngine(Protocol):
    """Letta-style virtual context management — compression / restore."""

    def compress(
        self,
        agent_id: str,
        memories: List[Dict[str, Any]],
        max_tokens: int = 4096,
    ) -> Dict[str, Any]:
        """Run three-stage compression pipeline on agent memories.

        Returns dict with active_memories, summary, trimmed_ids, budget_usage.
        """
        ...

    def restore(
        self, agent_id: str, trimmed_ids: List[str]
    ) -> Dict[str, Any]:
        """Restore previously-trimmed memories back to active context."""
        ...

    def get_stats(self) -> Dict[str, Any]:
        """Return historical compression statistics."""
        ...
