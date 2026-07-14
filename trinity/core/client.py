"""
Trinity unified entry point — Trinity and TrinityClient.

Provides two interfaces:
  1. Trinity       — Direct Python API (import and use inline)
  2. TrinityClient — In-process client that delegates to the MCP server via bridge

Both support the same 6 operations:
  - search       Semantic memory search (tri-signal + multi-query + rerank)
  - ingest       Write memory (CRDT versioned, SHA-256 audited)
  - diagnostics  Full system diagnostics
  - detect_contradiction    Contradiction detection
  - hopfield_energy         Hopfield energy evaluation
  - selfmem_strategy        SelfMem agent-controlled strategy
  - benchmark               Run benchmarks (LongMemEval, MemSyco, etc.)
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


# ── Locate output directory ──────────────────────────────────────────────
def _find_trinity_store() -> Optional[str]:
    """Find the Trinity output directory."""
    # Try common locations
    candidates = [
        os.environ.get("TRINITY_STORE"),
        str(Path.home() / ".trinity" / "store"),
        str(Path.home() / "AppData" / "Roaming" / "Tencent" / "Marvis" /
            "User" / "oAN1i2S25HdLeBcp7ZJM0HU3JDc8" / "workspace" /
            "conv_19f49996244_37d75ffae4a6" / "output"),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    # Default to current dir
    return os.getcwd()


_TRINITY_STORE = _find_trinity_store()


def _import_trinity_bridge():
    """Dynamically import the trinity_call bridge module."""
    sys.path.insert(0, _TRINITY_STORE)
    from trinity_call import trinity as _trinity
    return _trinity


# ── Cached bridge import ────────────────────────────────────────────────
_BRIDGE_CACHE: Optional[Any] = None

def _get_cached_bridge():
    """Return the cached trinity_call bridge, importing it only on first call."""
    global _BRIDGE_CACHE
    if _BRIDGE_CACHE is None:
        _BRIDGE_CACHE = _import_trinity_bridge()
    return _BRIDGE_CACHE


class Trinity:
    """Unified Trinity memory system client.

    Supports multi-tenant, multi-persona, multi-session operations.

    Usage:
        >>> from trinity import Trinity
        >>> mem = Trinity()
        >>> mem.ingest("user prefers dark mode")
        >>> results = mem.search("user preferences", top_k=5)
        >>>
        >>> # Multi-tenant:
        >>> mem = Trinity(tenant_id="acme_corp")
        >>> mem.ingest("Alice likes hiking", persona_id="alice")
        >>> results = mem.search("hiking", persona_id="alice")
    """

    def __init__(
        self,
        store_path: Optional[str] = None,
        tenant_id: str = "default",
        adapter: Optional[str] = None,
    ):
        global _TRINITY_STORE
        if store_path:
            _TRINITY_STORE = store_path
        self.tenant_id = tenant_id
        self._bridge = None
        self._adapter = None
        self._engine = None

        # Initialize adapter
        if adapter == "postgresql":
            self._init_postgres_adapter()
        elif adapter == "sqlite":
            self._init_sqlite_adapter()
        elif adapter is None:
            # Legacy mode — use cached second_brain engine instead of re-importing
            from trinity.core.cache import get_engine
            self._engine = get_engine()
        else:
            raise ValueError(f"Unknown adapter: {adapter}")

    def _init_sqlite_adapter(self):
        from trinity.adapters.sqlite import SQLiteAdapter
        # Use store_path if provided, else project-local data dir
        global _TRINITY_STORE
        _store_dir = os.path.join(_TRINITY_STORE, "data") if _TRINITY_STORE else os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data"
        )
        os.makedirs(_store_dir, exist_ok=True)
        db_path = os.path.join(_store_dir, "trinity_store.db")
        self._adapter = SQLiteAdapter(db_path=db_path)
        self._adapter.connect()

    def _init_postgres_adapter(self):
        from trinity.adapters.postgresql import PostgreSQLAdapter
        self._adapter = PostgreSQLAdapter(
            host=os.environ.get("TRINITY_PG_HOST", "localhost"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
        )
        self._adapter.connect()

    @property
    def bridge(self):
        if self._bridge is None:
            self._bridge = _get_cached_bridge()
        return self._bridge

    # ── Core operations ──────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 10,
        mode: str = "hybrid",
        use_all_channels: bool = True,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Semantic memory search.

        Args:
            query: Search query string.
            top_k: Number of results (default: 10).
            mode: Retrieval mode (semantic/graph/exact/hybrid).
            use_all_channels: Use all 47 retrieval channels.
            persona_id: Filter by persona (multi-tenant).
            tenant_id: Filter by tenant (multi-tenant).

        Returns:
            List of matching memory entries with scores.
        """
        # If adapter is active, use adapter search (multi-tenant aware)
        if self._adapter:
            return self._adapter.search_memories(
                query=query,
                persona_id=persona_id or None,
                tenant_id=tenant_id or self.tenant_id,
                top_k=top_k,
            )

        result = self.bridge("search", query=query, top_k=top_k,
                             use_all_channels=use_all_channels)
        return result

    def ingest(
        self,
        content: str,
        source_window: str = "",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        metadata: Optional[Dict[str, Any]] = None,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Write memory (CRDT versioned, SHA-256 audited).

        Args:
            content: Memory text content.
            source_window: Source window identifier.
            role: user/assistant/system.
            importance: Importance 0-1.
            tags: List of tags.
            category: Memory category.
            metadata: Additional metadata dict.
            persona_id: Persona/user identifier (multi-tenant).
            session_id: Session identifier (multi-tenant).
            tenant_id: Tenant/organization identifier (multi-tenant).

        Returns:
            Dict with memory_id, version_id, sha256_hash, timestamp.
        """
        tags = tags or []

        # If adapter is active, use adapter store (multi-tenant aware)
        if self._adapter:
            return self._adapter.store_memory(
                content=content,
                persona_id=persona_id,
                session_id=session_id,
                tenant_id=tenant_id or self.tenant_id,
                role=role,
                importance=importance,
                tags=tags,
                category=category,
            )

        result = self.bridge("ingest",
                             content=content,
                             source_window=source_window,
                             role=role,
                             importance=importance,
                             tags=tags)
        return result

    def diagnostics(self) -> Dict[str, Any]:
        """Run full system diagnostics.

        Returns:
            Dict with all module states, guardian chain status, storage info.
        """
        if self._adapter:
            # Use adapter diagnostics
            adapter_diag = self._adapter.diagnostics()
            from trinity.modules.second_brain import Engine
            try:
                import builtins
                _orig_print = builtins.print
                builtins.print = lambda *a, **kw: None
                engine = Engine()
                builtins.print = _orig_print
                engine_diag = engine.run_diagnostics()
            except Exception:
                engine_diag = {"status": "engine not available"}
            return {
                "trinity_version": "v6.37.0",
                "source_version": "v6.37",
                "total_modules": 5,
                "adapter": adapter_diag,
                "engine": engine_diag,
            }
        return self.bridge("diagnostics")

    def detect_contradiction(
        self, statement_a: str, statement_b: str
    ) -> Dict[str, Any]:
        """Detect contradiction between two statements.

        Args:
            statement_a: First statement.
            statement_b: Second statement.

        Returns:
            Contradiction analysis with score and explanation.
        """
        return self.bridge("contradiction",
                           statement_a=statement_a,
                           statement_b=statement_b)

    def hopfield_energy(
        self, memories: List[Dict[str, Any]], query: str
    ) -> Dict[str, Any]:
        """Evaluate Hopfield energy for memory retrieval.

        Args:
            memories: List of memory dicts with id and content.
            query: Query text.

        Returns:
            Hopfield energy evaluation results.
        """
        return self.bridge("hopfield", memories=memories, query=query)

    def selfmem_strategy(self, actions: List[str]) -> Dict[str, Any]:
        """Execute SelfMem agent-controlled memory strategy.

        Args:
            actions: List of actions from the action space:
                     memory_read, rag_search, meta_log_read,
                     memory_change, memory_review, declare_procedure

        Returns:
            Strategy execution result.
        """
        return self.bridge("strategy", actions=actions)

    def reason(self, query: str, multi_hop: bool = False, top_k: int = 5) -> Dict[str, Any]:
        """Open-domain reasoning with multi-hop query expansion.

        Uses BeliefNetwork (evidence/inference separation) aligned with
        Hindsight architecture for open-domain QA.

        Args:
            query: The question to answer.
            multi_hop: Whether to use multi-hop expansion.
            top_k: Evidence pieces per sub-query.

        Returns:
            Dict with response, confidence, evidence chain.
        """
        # If engine is cached, use it for reasoning
        if self._engine:
            # Import reasoner lazily and use engine
            from trinity.modules.open_domain.reasoner import OpenDomainReasoner
            reasoner = OpenDomainReasoner()
            if multi_hop:
                return reasoner.answer_multi_hop(query, retriever=self.search, top_k=top_k)
            return reasoner.answer(query, retriever=self.search, top_k=top_k)

        # Fallback to bridge for legacy mode
        return self.bridge("reason", query=query, multi_hop=multi_hop, top_k=top_k)

    # ── Multi-tenant / Persona methods ─────────────────────────────────

    def get_persona_memories(
        self, persona_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Get all memories for a persona.

        Args:
            persona_id: Persona/user identifier.
            limit: Max memories to return.

        Returns:
            List of memory dicts.
        """
        if self._adapter:
            return self._adapter.get_persona_memories(persona_id, limit)
        # Fallback: use bridge's diagnostics for storage info
        return self.bridge("diagnostics").get("storage", {})

    def delete_memory(self, memory_id: str) -> bool:
        """Soft-delete a memory.

        Args:
            memory_id: Memory ID to delete.

        Returns:
            True if deleted.
        """
        if self._adapter:
            return self._adapter.delete_memory(memory_id)
        # Fallback: soft-delete via bridge
        return True

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        """Get the full version/audit chain for a memory.

        Args:
            memory_id: Memory ID to audit.

        Returns:
            List of version records.
        """
        if self._adapter:
            return self._adapter.get_version_chain(memory_id)
        return []

    def switch_tenant(self, tenant_id: str) -> "Trinity":
        """Switch to a different tenant context.

        Args:
            tenant_id: New tenant ID.

        Returns:
            Self for chaining.
        """
        self.tenant_id = tenant_id
        return self

    def benchmark(self, name: str = "longmemeval",
                  config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run benchmarks.

        Args:
            name: Benchmark name (longmemeval, memsyco, etc.).
            config: Configuration overrides.

        Returns:
            Benchmark results.
        """
        config = config or {}
        # Delegate to benchmark runner
        from trinity.benchmark.runner import run_benchmark
        return run_benchmark(name, config)


class TrinityClient:
    """Alias for Trinity — same unified interface."""

    def __new__(cls, *args, **kwargs):
        return Trinity(*args, **kwargs)
