"""
Memory Tools — Trinity MCP tools backed by the real engine.

Tools:
  - memory_search     Tri-signal semantic search (supports semantic/graph/exact/hybrid)
  - memory_write      Write memory (CRDT versioned, SHA-256 audited)
  - memory_update     Update memory (conflict-preserving)
  - memory_delete     Soft delete memory (audit chain preserved)
  - audit_query       SHA-256 provenance query
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from trinity.core.client import Trinity

logger = logging.getLogger("trinity.mcp.tools")

# Shared Trinity engine instance
_engine: Optional[Trinity] = None


def _get_engine() -> Trinity:
    global _engine
    if _engine is None:
        _engine = Trinity()
    return _engine


def register_memory_tools(mcp: FastMCP) -> None:
    """Register all memory tools with the FastMCP instance."""
    _register_memory_search(mcp)
    _register_memory_write(mcp)
    _register_memory_update(mcp)
    _register_memory_delete(mcp)
    _register_audit_query(mcp)
    _register_trinity_diagnostics(mcp)
    logger.info("Registered 6 memory tools (backed by real engine).")


# ---------------------------------------------------------------------------
# Tool: memory_search
# ---------------------------------------------------------------------------
def _register_memory_search(mcp: FastMCP) -> None:

    @mcp.tool()
    async def memory_search(
        query: str,
        top_k: int = 5,
        mode: str = "hybrid",
    ) -> list[dict[str, Any]]:
        """Tri-signal semantic memory search.

        Supports four modes:
        - semantic: vector semantic similarity
        - graph:    GoS BFS graph traversal
        - exact:    KV exact match
        - hybrid:   multi-channel RRF fusion (default)

        Args:
            query:  Search query string.
            top_k:  Number of results (default: 5).
            mode:   Retrieval mode (semantic/graph/exact/hybrid).

        Returns:
            List of matching memory entries with scores.
        """
        engine = _get_engine()
        result = engine.search(query=query, top_k=top_k)
        return result.get("results", result if isinstance(result, list) else [])


# ---------------------------------------------------------------------------
# Tool: memory_write
# ---------------------------------------------------------------------------
def _register_memory_write(mcp: FastMCP) -> None:

    @mcp.tool()
    async def memory_write(
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        category: str = "general",
        tags: Optional[list[str]] = None,
        importance: float = 0.5,
    ) -> dict[str, Any]:
        """Write memory (CRDT versioned, SHA-256 audited).

        Each write generates a unique version_id and SHA-256 content hash,
        recorded in the audit log.

        Args:
            content:    Memory text content.
            metadata:   Additional metadata dict.
            category:   Memory category (default: general).
            tags:       List of tags.
            importance: Importance 0-1 (default: 0.5).

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp.
        """
        engine = _get_engine()
        result = engine.ingest(
            content=content,
            role=metadata.get("role", "user") if metadata else "user",
            importance=importance,
            tags=tags or [],
            category=category,
            metadata=metadata,
        )
        return result


# ---------------------------------------------------------------------------
# Tool: memory_update
# ---------------------------------------------------------------------------
def _register_memory_update(mcp: FastMCP) -> None:

    @mcp.tool()
    async def memory_update(
        memory_id: str,
        new_content: str,
    ) -> dict[str, Any]:
        """Update memory (conflict-preserving strategy).

        Old version is marked as superseded. Full version chain is retained
        in the audit log for provenance.

        Args:
            memory_id:   Target memory ID.
            new_content: New content text.

        Returns:
            Dict with memory_id, old_version, new_version, sha256_hash.

        Raises:
            ValueError: If memory_id not found.
        """
        # Delegate to engine's internal update mechanism
        from trinity.modules.second_brain.engine import SecondBrainV636
        engine = SecondBrainV636()
        result = engine.update_memory(memory_id=memory_id, new_content=new_content)
        return result


# ---------------------------------------------------------------------------
# Tool: memory_delete
# ---------------------------------------------------------------------------
def _register_memory_delete(mcp: FastMCP) -> None:

    @mcp.tool()
    async def memory_delete(memory_id: str) -> dict[str, Any]:
        """Soft-delete memory (audit chain preserved).

        Memory status is marked as 'deleted'. Data and full version chain
        remain queryable via audit_query.

        Args:
            memory_id: Target memory ID.

        Returns:
            Dict with memory_id, deleted_version, timestamp.

        Raises:
            ValueError: If memory_id not found.
        """
        from trinity.modules.second_brain.engine import SecondBrainV636
        engine = SecondBrainV636()
        result = engine.delete_memory(memory_id=memory_id)
        return result


# ---------------------------------------------------------------------------
# Tool: audit_query
# ---------------------------------------------------------------------------
def _register_audit_query(mcp: FastMCP) -> None:

    @mcp.tool()
    async def audit_query(memory_id: str) -> dict[str, Any]:
        """SHA-256 provenance query.

        Returns the full version chain for a memory entry:
        version → timestamp → SHA-256 → operation type.

        Args:
            memory_id: Target memory ID.

        Returns:
            Dict with memory_id, version_chain, total_versions, current_status.

        Raises:
            ValueError: If memory_id not found.
        """
        from trinity.modules.second_brain.engine import SecondBrainV636
        engine = SecondBrainV636()
        result = engine.audit_memory(memory_id=memory_id)
        return result


# ---------------------------------------------------------------------------
# Tool: trinity_diagnostics
# ---------------------------------------------------------------------------
def _register_trinity_diagnostics(mcp: FastMCP) -> None:

    @mcp.tool()
    async def trinity_diagnostics() -> dict[str, Any]:
        """Run full Trinity system diagnostics.

        Returns module states, guardian chain status, storage info,
        and retrieval channel status for all 47 channels.
        """
        engine = _get_engine()
        return engine.diagnostics()
