"""
Trinity VMS — Main Entry Point.

The VMS class is the unified facade for Trinity's Virtual Memory System.
It composes all six provider types (memory / identity / audit / task /
search / compression) behind a single interface and supports hot-swapping
backends at runtime.

Usage::

    from trinity.vms import VMS

    # Quick start with defaults (SQLite + hybrid search)
    vms = VMS.from_defaults()

    # Custom configuration
    vms = VMS.from_config("vms_config.yaml")

    # Hot-swap backends
    vms.use_memory("postgres")
    vms.use_search("vector")

    # Attach framework adapter
    vms.connect_adapter(LangChainAdapter())

    # Graceful shutdown
    vms.shutdown()
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

from .registry import get_registry, VMRegistry
from .interfaces import (
    MemoryStore,
    IdentityProvider,
    Auditor as AuditorProtocol,
    TaskBroker,
    SearchEngine,
    CompressionEngine,
)

logger = logging.getLogger(__name__)


class VMS:
    """Virtual Memory System — Trinity's pluggable memory infrastructure.

    Six provider slots:
      - memory_store       → MemoryStore
      - identity_provider  → IdentityProvider
      - auditor            → Auditor
      - task_broker        → TaskBroker
      - search_engine      → SearchEngine
      - compression_engine → CompressionEngine
    """

    def __init__(self, registry: Optional[VMRegistry] = None):
        self._registry = registry or get_registry()
        self._adapters: List[Any] = []

    # ═══════════════════════════════════════════════════════════════════
    # Factory Methods
    # ═══════════════════════════════════════════════════════════════════

    @classmethod
    def from_defaults(
        cls,
        db_path: Optional[str] = None,
        use_ann: bool = False,
    ) -> "VMS":
        """Create a VMS instance with sensible defaults (SQLite + hybrid search).

        This is the simplest way to bootstrap Trinity VMS for development
        or single-tenant deployments.
        """
        vms = cls()
        vms._init_defaults(db_path=db_path, use_ann=use_ann)
        return vms

    @classmethod
    def from_config(cls, config_path: str) -> "VMS":
        """Create a VMS instance from a YAML / JSON configuration file.

        Parameters
        ----------
        config_path : str
            Path to a vms_config.yaml (or .json) file.
        """
        import json
        import os

        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"VMS config not found: {config_path}")

        raw = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
                cfg = yaml.safe_load(raw)
            except ImportError:
                # Minimal yaml parser for basic cases
                cfg = cls._parse_simple_yaml(raw)
        else:
            cfg = json.loads(raw)

        vms = cls()
        vms._init_from_config(cfg)
        return vms

    @staticmethod
    def _parse_simple_yaml(raw: str) -> Dict[str, Any]:
        """Minimal YAML parser (no pyyaml dependency)."""
        result: Dict[str, Any] = {}
        for line in raw.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if ":" in stripped:
                key, _, val = stripped.partition(":")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if val.lower() in ("true", "yes"):
                    val = True
                elif val.lower() in ("false", "no"):
                    val = False
                elif val == "[]":
                    val = []
                result[key] = val
        return result

    # ═══════════════════════════════════════════════════════════════════
    # Initialisation Helpers
    # ═══════════════════════════════════════════════════════════════════

    def _init_defaults(self, db_path: Optional[str] = None, use_ann: bool = False):
        """Wire up default backends."""
        # Memory store
        try:
            from trinity.adapters.sqlite import SQLiteAdapter
            _db_path = db_path or str(Path.home() / ".trinity" / "trinity.db")
            Path(_db_path).parent.mkdir(parents=True, exist_ok=True)
            adapter = SQLiteAdapter(db_path=_db_path)
            adapter.connect()
            self._registry.register("memory_store", "sqlite", adapter, set_default=True,
                                    aliases=["default"])
        except Exception as exc:
            logger.warning("VMS: SQLite adapter init failed (%s), using memory backend", exc)
            self._init_memory_backend()

        # Identity provider
        try:
            from trinity.identity.identity_manager import IdentityManager
            id_mgr = IdentityManager(adapter=adapter) if adapter else IdentityManager()
            self._registry.register("identity_provider", "default", id_mgr, set_default=True)
        except Exception as exc:
            logger.warning("VMS: identity provider init failed: %s", exc)

        # Auditor
        try:
            from trinity.audit.auditor import Auditor
            aud = Auditor(adapter=adapter) if adapter else Auditor()
            self._registry.register("auditor", "default", aud, set_default=True)
        except Exception as exc:
            logger.warning("VMS: auditor init failed: %s", exc)

        # Task broker
        try:
            from trinity.a2a.task_manager import TaskManager
            tm = TaskManager(adapter=adapter) if adapter else TaskManager()
            self._registry.register("task_broker", "default", tm, set_default=True)
        except Exception as exc:
            logger.warning("VMS: task broker init failed: %s", exc)

        # Search engine — wraps existing Trinity search
        self._init_search_engine(use_ann=use_ann)

        # Compression engine
        try:
            from trinity.memory.compression import MemoryCompressor
            compressor = MemoryCompressor(max_tokens=4096, compression_threshold=0.8)
            self._registry.register("compression_engine", "default", compressor, set_default=True)
        except Exception as exc:
            logger.warning("VMS: compression engine init failed: %s", exc)

    def _init_memory_backend(self):
        """Fallback: register in-memory backend."""
        from trinity.vms.backends.memory_backend import InMemoryBackend
        mem = InMemoryBackend()
        self._registry.register("memory_store", "memory", mem, set_default=True)

    def _init_search_engine(self, use_ann: bool = False):
        """Register the search engine by wrapping Trinity's HybridRetriever."""
        try:
            from trinity.retrieval.hybrid_retriever import HybridRetriever
            hr = HybridRetriever(use_ann=use_ann)
            self._registry.register("search_engine", "hybrid", hr, set_default=True,
                                    aliases=["default"])
            self._registry.register("search_engine", "vector", hr,
                                    aliases=["semantic", "embedding"])
        except Exception as exc:
            logger.warning("VMS: search engine init failed: %s", exc)

    def _init_from_config(self, cfg: Dict[str, Any]):
        """Bootstrap from a parsed configuration dict."""
        # Memory backend
        mem_type = cfg.get("memory_backend", "sqlite")
        if mem_type == "postgres":
            from trinity.vms.backends.postgres_backend import PostgresBackend
            conn_str = cfg.get("connection_string") or cfg.get("DATABASE_URL", "")
            pg = PostgresBackend(connection_string=conn_str)
            pg.connect()
            self._registry.register("memory_store", "postgres", pg, set_default=True)
        elif mem_type == "memory":
            self._init_memory_backend()
        else:
            self._init_defaults()

        # Search engine
        se_type = cfg.get("search_engine", "hybrid")
        if se_type != "hybrid":
            self._registry.switch_backend("search_engine", se_type)

        # Adapters
        adapter_names = cfg.get("adapters", [])
        for name in adapter_names:
            self.connect_adapter(framework=name)

    # ═══════════════════════════════════════════════════════════════════
    # Backend Switching
    # ═══════════════════════════════════════════════════════════════════

    def use_memory(self, backend: str = "sqlite") -> bool:
        """Hot-switch the memory store backend."""
        return self._registry.switch_backend("memory_store", backend)

    def use_search(self, backend: str = "hybrid") -> bool:
        """Hot-switch the search engine backend."""
        return self._registry.switch_backend("search_engine", backend)

    # ═══════════════════════════════════════════════════════════════════
    # Adapter Connection
    # ═══════════════════════════════════════════════════════════════════

    def connect_adapter(self, adapter: Any = None, framework: str = "") -> Any:
        """Attach a framework adapter to VMS.

        Parameters
        ----------
        adapter : Any
            Pre-constructed adapter instance.  If None, framework is used
            to auto-construct a built-in adapter.
        framework : str
            One of "langchain" / "crewai" / "autogen".  Ignored if adapter
            is provided.
        """
        if adapter is not None:
            self._adapters.append(adapter)
            return adapter

        # Auto-construct from framework name
        fw = framework.lower()
        if fw == "langchain":
            from trinity.vms.adapters.langchain_adapter import LangChainAdapter
            adapter = LangChainAdapter()
        elif fw == "crewai":
            from trinity.vms.adapters.crewai_adapter import CrewAIAdapter
            adapter = CrewAIAdapter()
        elif fw == "autogen":
            from trinity.vms.adapters.autogen_adapter import AutoGenAdapter
            adapter = AutoGenAdapter()
        else:
            raise ValueError(
                f"Unknown framework: {framework}. "
                f"Supported: langchain / crewai / autogen"
            )

        self._adapters.append(adapter)
        return adapter

    def list_adapters(self) -> List[str]:
        """Return list of attached adapter framework names."""
        return [a.framework_name for a in self._adapters]

    # ═══════════════════════════════════════════════════════════════════
    # Provider Accessors
    # ═══════════════════════════════════════════════════════════════════

    @property
    def memory_store(self) -> Optional[MemoryStore]:
        return self._registry.get("memory_store")

    @property
    def identity_provider(self) -> Optional[IdentityProvider]:
        return self._registry.get("identity_provider")

    @property
    def auditor(self) -> Optional[AuditorProtocol]:
        return self._registry.get("auditor")

    @property
    def task_broker(self) -> Optional[TaskBroker]:
        return self._registry.get("task_broker")

    @property
    def search_engine(self) -> Optional[SearchEngine]:
        return self._registry.get("search_engine")

    @property
    def compression_engine(self) -> Optional[CompressionEngine]:
        return self._registry.get("compression_engine")

    @property
    def registry(self) -> VMRegistry:
        return self._registry

    # ═══════════════════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════════════════

    def shutdown(self):
        """Gracefully shut down all backends."""
        for ptype in ("memory_store",):
            inst = self._registry.get(ptype)
            if inst and hasattr(inst, "disconnect"):
                try:
                    inst.disconnect()
                except Exception:
                    pass
        logger.info("VMS shutdown complete")

    def status(self) -> Dict[str, Any]:
        """Return a health-check / status snapshot."""
        return {
            "registry": self._registry.status(),
            "adapters": self.list_adapters(),
        }
