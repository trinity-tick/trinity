#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split trinity/adapters/sqlite.py into a package of domain mixins.

Behavior-preserving: every method body is moved verbatim (byte-identical
source lines) into a mixin class; SQLiteAdapter(StorageAdapter, *mixins)
rebuilds the same class. Public import surface unchanged.
"""
import ast, os

SRC = r"trinity/adapters/sqlite.py"
PKG = r"trinity/adapters/sqlite"
os.makedirs(PKG, exist_ok=True)

src = open(SRC, encoding="utf-8").read()
tree = ast.parse(src)

cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SQLiteAdapter")
methods = [n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
by_name = {m.name: m for m in methods}
lines = src.splitlines(keepends=True)
NL = chr(10)

def slice_method(name):
    m = by_name[name]
    start = m.lineno - 1
    # decorators live above the def line; include them (Python 3.8+: lineno is def line)
    if getattr(m, "decorator_list", None):
        start = min(d.lineno for d in m.decorator_list) - 1
    text = "".join(lines[start:m.end_lineno])
    if not text.endswith(NL):
        text += NL
    return text

GROUPS = {
    "connection": ["__init__", "connect", "_prewarm_tokenizer", "_apply_pragmas",
                   "disconnect", "_get_read_conn"],
    "schema":     ["_create_tables", "_create_fts5"],
    "crypto":     ["_compute_sha256", "_encrypt_content", "_decrypt_content",
                   "_tokenized_for_storage", "_detect_pii"],
    "audit":      ["write_audit_log", "_write_audit_log", "export_user_data",
                   "forget_user"],
    "batch":      ["_fts_available", "ingest_batch", "_maybe_flush", "_flush_batch"],
    "crud":       ["store_memory", "get_memory", "get_memory_owners",
                   "get_persona_memories", "delete_memory", "update_memory",
                   "archive_memories", "get_version_chain", "get_all_memories",
                   "touch_memory", "_touch_batch", "_touch_flush_loop",
                   "_flush_touch_queue", "age_memories"],
    "search":     ["search_memories", "_tokenize_fts_query",
                   "_tokenize_content_for_fts", "_search_fts", "_search_like"],
    "stats":      ["get_memory_stats", "get_modality_stats",
                   "check_content_hash_collision", "get_conflicts",
                   "resolve_conflict", "dedup_stats", "set_agent_weight",
                   "get_agent_weights", "delete_agent_weight"],
    "graph":      ["create_memory_link", "get_linked_memories", "strengthen_link",
                   "weaken_link", "delete_memory_link", "get_all_links",
                   "_parse_entity_properties", "upsert_entity", "get_entity",
                   "search_entities", "create_relation", "query_relations",
                   "query_relations_at", "traverse", "create_entity",
                   "get_entity_by_name", "get_neighbors", "query_graph"],
    "anchors":    ["upsert_anchor", "get_anchors", "get_all_anchors",
                   "get_latest_anchor_version", "register_agent_card",
                   "get_agent_card", "create_a2a_task", "update_a2a_task",
                   "list_a2a_tasks", "update_agent_heartbeat"],
    "diagnostics": ["diagnostics"],
}

all_assigned = [n for names in GROUPS.values() for n in names]
missing = [n for n in by_name if n not in all_assigned]
dup = [n for n in set(all_assigned) if all_assigned.count(n) > 1]
if missing or dup:
    raise SystemExit("grouping error: missing=%s dup=%s" % (missing, dup))

HEADER = """SQLite adapter - {title} mixin (split from sqlite.py, 2026-08-17).

Part of the SQLiteAdapter package decomposition. Behavior identical to the
pre-split single-file implementation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import functools
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ...security.crypto import get_storage_cipher, StorageCipher  # type: ignore[attr-defined]
from .._util import _safe_write

logger = logging.getLogger("trinity.adapters.sqlite")

"""

TITLES = {
    "connection": "connection lifecycle",
    "schema": "table & FTS5 schema",
    "crypto": "encryption & PII",
    "audit": "audit log & GDPR",
    "batch": "batched ingestion",
    "crud": "memory CRUD & lifecycle",
    "search": "search & FTS",
    "stats": "stats, conflicts, agent weights",
    "graph": "links, entities, relations",
    "anchors": "anchors, agent cards, A2A",
    "diagnostics": "diagnostics",
}

# module-level constants used by methods (kept module-scope for batch flush)
CONSTS = "_BATCH_SIZE = 100       # 攒够 100 条\n_BATCH_TIMEOUT = 5.0    # 或 5 秒\n\n"

for group, names in GROUPS.items():
    body = "".join(slice_method(n) for n in names)
    mod = HEADER.format(title=TITLES[group])
    if group == "batch":
        mod += CONSTS
    mod += "class _" + group.title().replace("_", "") + "Mixin:" + NL
    # methods were sliced at original 4-space indent; keep as-is inside the mixin class
    mod += body
    if not mod.endswith(NL):
        mod += NL
    with open(os.path.join(PKG, "_" + group + ".py"), "w", encoding="utf-8", newline="") as f:
        f.write(mod)
    print("wrote _" + group + ".py (" + str(len(names)) + " methods)")

init = """SQLite storage adapter - single-tenant default backend.

Package decomposition (2026-08-17): the former monolith sqlite.py was split
into domain mixins (_connection/_schema/_crypto/_audit/_batch/_crud/_search/
_stats/_graph/_anchors/_diagnostics). Public API unchanged:
``SQLiteAdapter`` and ``_safe_write`` are re-exported here.

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


class SQLiteAdapter(StorageAdapter, _ConnectionMixin, _SchemaMixin, _CryptoMixin,
                    _AuditMixin, _BatchMixin, _CrudMixin, _SearchMixin, _StatsMixin,
                    _GraphMixin, _AnchorsMixin, _DiagnosticsMixin):
    """SQLite-based storage adapter.

    Default backend for single-tenant deployments.
    Supports persona_id and session_id scoping.
    """
    pass
"""
with open(os.path.join(PKG, "__init__.py"), "w", encoding="utf-8", newline="") as f:
    f.write(init)
print("wrote __init__.py")

util = """Shared helpers for the sqlite adapter package."""

from __future__ import annotations

import functools


def _safe_write(fn):
    """write-path guard: rollback on any exception to avoid dangling write tx."""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except Exception:
            try:
                self._conn.rollback()
            except Exception:
                pass
            raise
    return wrapper
"""
with open(r"trinity/adapters/_util.py", "w", encoding="utf-8", newline="") as f:
    f.write(util)
print("wrote _util.py")

os.replace(SRC, os.path.join(PKG, "_monolith_backup.py"))
print("moved sqlite.py -> sqlite/_monolith_backup.py")