"""Abstract storage adapter interface."""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional


class StorageAdapter(ABC):
    """Abstract base class for storage backends.

    Supports multi-tenant, multi-persona, multi-session memory storage.
    """

    @abstractmethod
    def connect(self) -> None:
        """Establish connection to the storage backend."""
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """Close the connection."""
        ...

    @abstractmethod
    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
    ) -> Dict[str, Any]:
        """Store a memory entry.

        Args:
            content: Memory text content.
            persona_id: User/profile identifier.
            session_id: Session identifier.
            tenant_id: Tenant/organization identifier.
            role: user/assistant/system.
            importance: 0-1 importance score.
            tags: List of tags.
            category: Memory category.

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp.
        """
        ...

    @abstractmethod
    def search_memories(
        self,
        query: str,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        """Search memories with optional persona/tenant scoping.

        Args:
            query: Search query.
            persona_id: Filter by persona (None = all).
            tenant_id: Filter by tenant (None = all).
            top_k: Max results.

        Returns:
            List of matching memory dicts.
        """
        ...

    @abstractmethod
    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        """Get a single memory by ID."""
        ...

    @abstractmethod
    def get_persona_memories(
        self, persona_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get all memories for a persona."""
        ...

    @abstractmethod
    def delete_memory(self, memory_id: str) -> bool:
        """Soft-delete a memory."""
        ...

    @abstractmethod
    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        """Get the full version/audit chain for a memory."""
        ...

    def get_all_memories(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Get all active memories across all personas.

        Args:
            limit: Max memories to return.

        Returns:
            List of memory dicts.
        """
        # Default implementation uses get_persona_memories with empty persona_id
        # Subclasses should override for better performance
        return []

    @abstractmethod
    def diagnostics(self) -> Dict[str, Any]:
        """Return storage diagnostics."""
        ...
