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

from ._base import _SQLiteMixinBase
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 兜底：退回原静默行为
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

logger = logging.getLogger("trinity.adapters.sqlite")


_BATCH_SIZE = 100       # 攒够 100 条
_BATCH_TIMEOUT = 5.0    # 或 5 秒


class BatchResults(list):
    """`ingest_batch` 的返回值：**仍然是 list**（`len`/索引/迭代/切片全部照旧），
    但额外挂**表级真实计数** —— t59/H3：此前只返回一个裸 list，**没有任何表级计数**，
    于是 200 条里只落了 50 行时，调用方**从返回值完全看不出**（`len()` = 200，条条"成功"）。

    属性（全部来自**实测**，不是推断）：
      · `sent`           送进去的记录数（= `len(self)`，便于与其他计数对齐）
      · `rows_added`     **真实新增行数**（写入前后 `count(*)` 之差 —— 这才是"真话"）
      · `inserted_count` / `deduped_count`  逐条 `inserted`/`deduped` 标记的计数
      · `failed_count`   既没 inserted 也没 deduped 的条数（例如被策略拒存的高危条）
      · `silent_drop`    `rows_added != sent` —— **诚实标记**：去重本身是设计，
        但"去重被静默"不是；调用方据此可直接断言/告警，不必自己数库。
    """

    def __init__(self, rows=None, *, sent: int = 0, rows_added: int = 0,
                 inserted_count: int = 0, deduped_count: int = 0, failed_count: int = 0):
        super().__init__(rows or [])
        self.sent = int(sent)
        self.rows_added = int(rows_added)
        self.inserted_count = int(inserted_count)
        self.deduped_count = int(deduped_count)
        self.failed_count = int(failed_count)
        self.silent_drop = (self.rows_added != self.sent)

    def counts(self) -> Dict[str, int]:
        return {"sent": self.sent, "rows_added": self.rows_added,
                "inserted": self.inserted_count, "deduped": self.deduped_count,
                "failed": self.failed_count, "silent_drop": int(self.silent_drop)}


class _BatchMixin(_SQLiteMixinBase):
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

            # t59/H3：**写入前后各数一次行数** ⇒ `rows_added` 是**实测真值**（不是逐条推断）。
            def _rows() -> int:
                try:
                    cur = self._conn.execute("SELECT count(*) FROM memories")
                    return int(cur.fetchone()[0])
                except Exception as _e:  # noqa: BLE001 — 数不出来不能让批量写失败
                    swallow(__name__, _e)
                    return -1

            _before = _rows()
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
                    # 2026 优化轮 B6：与 PG 批量通道同签名（`rec["status"]`），
                    # 调用方可在同一条 INSERT 内指定落库状态（只允许下调为 archived）。
                    status=rec.get("status"),
                )
                results.append(result)

            # 检查是否需要 flush
            self._maybe_flush()
            _after = _rows()
            _inserted = sum(1 for r in results if isinstance(r, dict) and r.get("inserted") is True)
            _deduped = sum(1 for r in results if isinstance(r, dict) and r.get("deduped") is True)
            return BatchResults(
                results,
                sent=len(records),
                rows_added=(_after - _before) if (_before >= 0 and _after >= 0) else -1,
                inserted_count=_inserted,
                deduped_count=_deduped,
                failed_count=max(0, len(records) - _inserted - _deduped),
            )
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
            except Exception as _e:
                swallow(__name__, _e)
    def _flush_batch(self) -> None:
        """提交所有缓冲写入。"""
        if not self._batch_buffer:
            return
        try:
            self._conn.commit()
        except Exception:
            # 2026-09-29（外部审计修复，根因 D）：原为裸 `self._conn.rollback()`。
            # `_conn` 是 Optional（`disconnect()` 会置 None），于是当**未连接/已断开**
            # 而缓冲非空时，**except 分支自己会抛**
            # `AttributeError: 'NoneType' object has no attribute 'rollback'`
            # ⇒ 异常逃出 `_flush_batch()`（并因此逃出它的调用方 `disconnect()`）
            # ⇒ "已吞掉、继续走" 的语义**并不成立**。
            # 实测复现：`a = SQLiteAdapter(...)`（不 connect）+ `a._batch_buffer = [..]`
            # + `a._flush_batch()` ⇒ 抛 AttributeError。
            # 这条缺陷是**先由静态检查暴露的**：基类把 `_conn` 如实标为
            # `Optional[sqlite3.Connection]` 后，mypy 报 `union-attr`
            #（原来各 mixin 连 `_conn` 这个属性都看不见，报的是 attr-defined）。
            # 修法：回滚本身也做保护 —— 它失败不该让"已处理"的分支再炸一次。
            try:
                if self._conn is not None:
                    self._conn.rollback()
            except Exception as _e2:
                swallow(__name__, _e2)
        finally:
            self._batch_buffer = []
            self._batch_last_flush = time.time()
