"""
Trinity VMS — SQLite Backend.

Thin wrapper around Trinity's existing SQLiteAdapter to satisfy the
MemoryStore protocol.  No code duplication — delegates all operations
to the canonical SQLiteAdapter.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from trinity.adapters.sqlite import SQLiteAdapter

logger = logging.getLogger(__name__)


class SQLiteVMSBackend:
    """SQLite backend conforming to the MemoryStore protocol.

    This is a thin bridge that maps the MemoryStore protocol methods
    onto the existing SQLiteAdapter API so that VMS consumers can use
    the backend without knowing about internal implementation details.
    """

    def __init__(self, db_path: Optional[str] = None):
        """
        Parameters
        ----------
        db_path : Optional[str]
            Path to the SQLite database file.  Defaults to
            ~/.trinity/trinity.db.
        """
        from pathlib import Path

        self._db_path = db_path or str(Path.home() / ".trinity" / "trinity.db")
        self._adapter = SQLiteAdapter(db_path=self._db_path)

    def connect(self) -> None:
        self._adapter.connect()

    def disconnect(self) -> None:
        self._adapter.disconnect()

    # ── MemoryStore Protocol Methods ──────────────────────────────────

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
        return self._adapter.store_memory(
            content=content,
            agent_id=agent_id,
            persona_id=persona_id,
            session_id=session_id,
            tenant_id=tenant_id,
            role=role,
            importance=importance,
            tags=tags,
            category=category,
        )

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        try:
            return self._adapter.get_memory(memory_id)
        except Exception:
            return None

    def search(
        self,
        query: str,
        top_k: int = 10,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self._adapter.search_memories(
            query=query,
            top_k=top_k,
            agent_id=agent_id,
            persona_id=persona_id,
            tenant_id=tenant_id,
        )

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        try:
            self._adapter.delete_memory(memory_id, soft=soft)
            return True
        except Exception:
            return False

    def count(
        self,
        agent_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> int:
        try:
            return self._adapter.get_memory_count(
                agent_id=agent_id,
                tenant_id=tenant_id,
            )
        except Exception:
            return 0
