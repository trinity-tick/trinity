"""SQLite adapter - diagnostics mixin (split from sqlite.py, 2026-08-17).

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


class _DiagnosticsMixin:
    def diagnostics(self) -> Dict[str, Any]:
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories")
        total = cursor.fetchone()["c"]

        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE status = 'active'"
        )
        active = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(DISTINCT persona_id) as c FROM memories")
        personas = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(DISTINCT agent_id) as c FROM memories")
        agents = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM memory_versions")
        versions = cursor.fetchone()["c"]

        # 检查 FTS5 状态
        fts_ok = self._fts_available()

        # TTL 统计
        cursor = conn.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'expired'")
        expired = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT AVG(access_count) as a FROM memories WHERE status = 'active'")
        avg_access = cursor.fetchone()["a"] or 0

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "adapter": "sqlite",
            "db_path": self.db_path,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "total_memories": total,
            "active_memories": active,
            "expired_memories": expired,
            "total_personas": personas,
            "total_agents": agents,
            "total_versions": versions,
            "fts5_enabled": fts_ok,
            "avg_access_count": round(avg_access, 2),
            "journal_mode": "wal",
            "read_conn_pool": {
                "active": len(self._read_conns),
                "max": self._read_conn_max,
                "overflow": self._read_conn_overflow,
            },
            "agent_weights_configured": len(self.get_agent_weights()),
            "memory_links_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM memory_links"
            ).fetchone()["c"],
            "entity_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM entities"
            ).fetchone()["c"],
            "relation_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM relations"
            ).fetchone()["c"],
            "audit_log_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM audit_log"
            ).fetchone()["c"],
            "identity_anchor_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM identity_anchors"
            ).fetchone()["c"],
            "audit_run_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM audit_runs"
            ).fetchone()["c"],
            "violation_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM constitutional_violations"
            ).fetchone()["c"],
            "a2a_task_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM a2a_tasks"
            ).fetchone()["c"],
            "agent_registry_count": self._conn.execute(
                "SELECT COUNT(*) as c FROM agent_registry"
            ).fetchone()["c"],
        }
