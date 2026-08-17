"""SQLite storage adapter - single-tenant default backend.

Package decomposition (2026-08-17): the former monolith sqlite.py was split
into domain mixins (_connection/_schema/_crypto/_audit/_batch/_crud/_search/
_stats/_graph/_anchors/_diagnostics). Public API unchanged:
SQLiteAdapter and _safe_write are re-exported here.

Supports WAL mode, FTS5 full-text search, batched writes.
"""

from __future__ import annotations

import logging

from ..base import StorageAdapter
from .._util import _safe_write

from ._connection import _ConnectionMixin
from ._schema import _SchemaMixin
from ._crypto import _CryptoMixin
from ._audit import _AuditMixin
from ._batch import _BatchMixin
from ._crud import _CrudMixin
from ._search import _SearchMixin
from ._stats import _StatsMixin
from ._graph import _GraphMixin
from ._anchors import _AnchorsMixin
from ._diagnostics import _DiagnosticsMixin

logger = logging.getLogger("trinity.adapters.sqlite")

__all__ = ["SQLiteAdapter", "_safe_write"]


class SQLiteAdapter(_ConnectionMixin, _SchemaMixin, _CryptoMixin, _AuditMixin,
                    _BatchMixin, _CrudMixin, _SearchMixin, _StatsMixin, _GraphMixin,
                    _AnchorsMixin, _DiagnosticsMixin, StorageAdapter):
    """SQLite-based storage adapter.

    Default backend for single-tenant deployments.
    Supports persona_id and session_id scoping.
    """
    pass

