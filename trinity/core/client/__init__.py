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

搜索管线（任务C）:
  - 支持 use_vector=True 启用向量语义搜索
  - 向量 + SQLite FTS 融合排序
  - 默认 use_vector=False 保持向后兼容
"""

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from trinity.telemetry import traced

from ._helpers import (
    _find_trinity_store,
    _get_embedding_engine,
    _get_vector_index,
    _fuse_results,
)
from ._construction import (
    _TRINITY_STORE,
    _BRIDGE_CACHE,
    _import_trinity_bridge,
    _get_cached_bridge,
    _ConstructionMixin,
)
from ._search import _SearchMixin
from ._vector import _VectorMixin
from ._ingestion import _IngestionMixin
from ._graph import _GraphMixin
from ._crud import _CrudMixin
from ._stats import _StatsMixin
from ._diagnostics import _DiagnosticsMixin
from ._multimodal import _MultimodalMixin
from ._a2a import _A2AMixin
from ._audit_identity import _AuditIdentityMixin
from ._advanced import _AdvancedMixin
from ._pagetree import _PagetreeMixin

__all__ = ["Trinity", "TrinityClient"]

class Trinity(_ConstructionMixin, _SearchMixin, _VectorMixin, _IngestionMixin, _GraphMixin, _CrudMixin, _StatsMixin, _DiagnosticsMixin, _MultimodalMixin, _A2AMixin, _AuditIdentityMixin, _AdvancedMixin, _PagetreeMixin):
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
    pass

class TrinityClient:
    """Alias for Trinity — same unified interface."""

    def __new__(cls, *args, **kwargs):
        return Trinity(*args, **kwargs)
