"""
Trinity VMS — In-Memory Backend (for testing / rapid prototyping).

All data lives in a Python dict.  No persistence, but zero dependencies
and instant startup.  Ideal for unit tests and quick experiments.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional


class InMemoryBackend:
    """Pure in-memory MemoryStore implementation.

    Not suitable for production (no persistence, no vector search).
    """

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    # ── MemoryStore Protocol ──────────────────────────────────────────

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
        memory_id = str(uuid.uuid4())
        now = time.time()
        entry = {
            "memory_id": memory_id,
            "content": content,
            "agent_id": agent_id,
            "persona_id": persona_id,
            "session_id": session_id,
            "tenant_id": tenant_id,
            "role": role,
            "importance": importance,
            "tags": tags or [],
            "category": category,
            "created_at": now,
            "updated_at": now,
            "is_deleted": False,
        }
        self._store[memory_id] = entry
        return {
            "memory_id": memory_id,
            "created_at": now,
            "agent_id": agent_id,
            "category": category,
        }

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        entry = self._store.get(memory_id)
        if entry and not entry.get("is_deleted"):
            return entry
        return None

    def search(
        self,
        query: str,
        top_k: int = 10,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        results = []
        q_lower = query.lower()
        for entry in self._store.values():
            if entry.get("is_deleted"):
                continue
            if agent_id and entry.get("agent_id") != agent_id:
                continue
            if persona_id and entry.get("persona_id") != persona_id:
                continue
            if tenant_id and entry.get("tenant_id") != tenant_id:
                continue
            # Simple substring match
            if q_lower in entry.get("content", "").lower():
                results.append(entry)
        return results[:top_k]

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        if memory_id not in self._store:
            return False
        if soft:
            self._store[memory_id]["is_deleted"] = True
        else:
            del self._store[memory_id]
        return True

    def count(
        self,
        agent_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> int:
        total = 0
        for entry in self._store.values():
            if entry.get("is_deleted"):
                continue
            if agent_id and entry.get("agent_id") != agent_id:
                continue
            if tenant_id and entry.get("tenant_id") != tenant_id:
                continue
            total += 1
        return total
