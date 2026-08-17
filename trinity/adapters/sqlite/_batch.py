"""SQLite adapter - batched ingestion mixin (split from sqlite.py, 2026-08-17).

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


_BATCH_SIZE = 100       # 攒够 100 条
_BATCH_TIMEOUT = 5.0    # 或 5 秒

class _BatchMixin:
    def _fts_available(self) -> bool:
        """检查 FTS5 是否可用（2026-08-15 v2：线程本地只读连接）。"""
        try:
            conn = self._get_read_conn()
            if not conn:
                return False
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='memories_fts'"
            )
            return cursor.fetchone() is not None
        except Exception:
            return False
    @_safe_write
    def ingest_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量写入记忆记录。

        攒够 100 条或 5 秒后统一 commit。如果 records 为空，
        仅做 flush 检查。

        Args:
            records: 要写入的记录列表，每项包含 store_memory 参数。

        Returns:
            每条记录的写入结果列表。
        """
        with self._write_lock:

            results = []
            for rec in records:
                content = rec.get("content", "")
                result = self.store_memory(
                    content=content,
                    persona_id=rec.get("persona_id", "default"),
                    session_id=rec.get("session_id"),
                    tenant_id=rec.get("tenant_id", "default"),
                    agent_id=rec.get("agent_id", "default"),
                    role=rec.get("role", "user"),
                    importance=rec.get("importance", 0.5),
                    tags=rec.get("tags"),
                    category=rec.get("category", "general"),
                    ttl_seconds=rec.get("ttl_seconds"),
                    modality=rec.get("modality", "text"),
                    metadata=rec.get("metadata"),
                )
                results.append(result)

            # 检查是否需要 flush
            self._maybe_flush()
            return results
    def _maybe_flush(self) -> None:
        """如果达到批量条件则 flush。同时确保每次写入后立即 commit。"""
        now = time.time()
        if (len(self._batch_buffer) >= _BATCH_SIZE or
                (self._batch_buffer and now - self._batch_last_flush >= _BATCH_TIMEOUT)):
            self._flush_batch()
        else:
            # 确保每次写入都 commit，防止进程退出时数据丢失
            try:
                self._conn.commit()
                self._batch_last_flush = time.time()
            except Exception:
                pass
    def _flush_batch(self) -> None:
        """提交所有缓冲写入。"""
        if not self._batch_buffer:
            return
        try:
            self._conn.commit()
        except Exception:
            self._conn.rollback()
        finally:
            self._batch_buffer = []
            self._batch_last_flush = time.time()
