"""SQLite adapter - connection lifecycle mixin (split from sqlite.py, 2026-08-17).

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


class _ConnectionMixin:
    def __init__(self, db_path: str = "trinity_store.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

        # ── 存储加密（B5, 2026-08-15）──────────────────────────────────
        # TRINITY_STORAGE_ENCRYPTION=on 时启用 AES-256-GCM：
        #   - memories.content / memory_versions.content 落盘为密文
        #   - tokenized_content 保持明文 → FTS5 检索不受影响
        #   - sha256_hash/content_hash 基于明文计算 → 去重/一致性链不变
        self._cipher: Optional[StorageCipher] = get_storage_cipher()
        if self._cipher:
            logger.info("storage encryption ON (AES-256-GCM) for %s", db_path)
        # 2026-08-24（R9 P0-1）：建表/迁移写锁失败时进入只读模式
        # （检索可用、写操作报错），见 connect()。
        self._readonly_mode: bool = False

        # ── 批量写入缓冲区 ──────────────────────────────────────────
        self._batch_buffer: List[Dict[str, Any]] = []
        self._batch_last_flush = time.time()

        # ── 写锁（2026-08-15，根治 mcp-stdio 持锁）──────────────────
        # SQLite 单连接同一时刻只能有一个写事务；ingest 主线程与
        # _postprocess_memory 后台线程并发写同一 conn，交错会导致
        # 未提交写事务悬挂、永久占库锁（database is locked）。
        # RLock：同一线程可重入（嵌套写方法），跨线程 serialize。
        self._write_lock = threading.RLock()

        # ── 线程本地只读连接池（2026-08-15 压测修复）──────────────
        # 读路径（search/get/query）每线程独立只读连接：WAL 下多读
        # 并行、零锁竞争（实测 8 线程 p50 115ms→25ms）。写路径仍走
        # 主连接 self._conn + _write_lock（WAL 单写者语义）。
        # 生命周期管理（2026-08-15 二轮）：注册表 + 上限 + 超限回退，
        # 防线程频繁重建时连接句柄滞留（见 _get_read_conn / disconnect）。
        self._read_local = threading.local()
        self._read_conns: set = set()          # 全部线程本地读连接（强引用）
        self._read_conns_lock = threading.Lock()
        self._read_conn_max = 64               # 读连接上限（防无界增长）
        self._read_conn_overflow = 0           # 超限回退计数（诊断）

        # ── 异步 touch 队列（2026-08-15，压测修复）────────────────
        # 检索命中即 touch 是隐藏写放大（实测占读延迟 ~40%）：每次
        # search 同步 UPDATE+commit。改为内存累积 + 后台线程定期批量
        # flush（一次 UPDATE…IN + 一次 commit），读路径零写阻塞。
        self._touch_queue: Dict[str, int] = {}  # memory_id -> 命中次数
        self._touch_pending = threading.Event()
        self._touch_stop = threading.Event()
    def connect(self) -> None:
        """Connect to SQLite database and create tables if needed.

        2026-08-24（R9 P0-1）：建表/迁移写操作**容错降级**——此前建表
        写锁失败（database is locked）会整体抛异常 → Trinity 上层静默
        adapter=None → 引擎检索全 0（健康假象）。现在：只读连接先建立，
        建表/迁移失败时进入只读模式（_readonly_mode=True，检索/读取正常，
        写操作报错），不再让初始化写操作阻断只读检索。
        """
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        # check_same_thread=False：允许 FastAPI/HTTP 线程池等跨线程复用同一连接
        # （SQLite 连接默认线程绑定，多线程服务调用 search/store 会抛
        #  "SQLite objects created in a thread can only be used in that same thread"）
        self._conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._apply_pragmas()
        try:
            self._create_tables()
            self._readonly_mode = False
        except Exception as exc:
            # 建表写锁失败（其他进程持写锁）→ 只读模式：检索仍可用，
            # 写操作走 _write_guard 报错；不再向上抛（R9 P0-1 解耦）。
            logger.warning(
                "SQLite schema init failed (%s) — entering READONLY mode for %s; "
                "retrieval remains available, writes will fail",
                exc, self.db_path,
            )
            self._readonly_mode = True
        # 性能（2026-08-15）：jieba 词典冷启动约 1.4s（首查被拖慢）——
        # 后台线程预热，把开销移到进程启动而非首次搜索。
        threading.Thread(target=self._prewarm_tokenizer, daemon=True).start()
        # 异步 touch flush 线程（2026-08-15 压测修复）
        threading.Thread(target=self._touch_flush_loop, daemon=True,
                         name="touch-flush").start()
    def _prewarm_tokenizer(self) -> None:
        """后台预热 jieba 分词词典（非阻塞；失败静默）。"""
        try:
            import jieba
            jieba.initialize()
            list(jieba.cut("Trinity 记忆系统分词预热"))
        except Exception:  # noqa: BLE001
            pass
    def _apply_pragmas(self) -> None:
        """应用性能优化 PRAGMA。"""
        cursor = self._conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.execute("PRAGMA cache_size=-8000;")   # 8MB cache
        # 2026-08-17（worker 锁争用根治）: busy_timeout 环境可配——
        # 高并发写库时 15s×多步写入可叠加 >60s 工具超时；短超时(如 3s)
        # 快速失败由上层自动重试，避免"60s 卡死"。默认 15000 兼容既有行为。
        try:
            busy_ms = int(os.environ.get("TRINITY_SQLITE_BUSY_TIMEOUT_MS", "15000"))
        except ValueError:
            busy_ms = 15000
        cursor.execute(f"PRAGMA busy_timeout={busy_ms};")
        self._conn.commit()
    def disconnect(self) -> None:
        # 断开前 flush 批处理缓冲区 + touch 队列
        self._flush_batch()
        self._touch_stop.set()
        self._flush_touch_queue()
        if self._conn:
            self._conn.close()
            self._conn = None
        # 关闭全部线程本地只读连接（注册表强引用保证不滞留）
        with self._read_conns_lock:
            conns = list(self._read_conns)
            self._read_conns.clear()
        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass
        # 当前线程的 thread-local 引用清空
        try:
            self._read_local.conn = None
        except Exception:
            pass
    def _get_read_conn(self) -> Optional[sqlite3.Connection]:
        """返回当前线程的只读连接（WAL 多读并行，2026-08-15）。

        读路径用 per-thread 只读连接：WAL 下并发读不阻塞、无锁竞争，
        替代此前"单连接 + RLock 串行化"（8 线程 p50 115ms→25ms）。
        只读模式（mode=ro）保证不会误写；写路径仍走主连接+_write_lock。

        生命周期（2026-08-15 二轮）：连接注册进 _read_conns（disconnect
        全量关闭）；超过 _read_conn_max 时创建临时只读连接（不注册、不缓存，
        GC 兜底关闭）并计 _read_conn_overflow——防线程频繁重建时句柄滞留、
        防无界增长，同时避免回退主连接导致调用方锁外 execute 的游标竞态。
        """
        conn = getattr(self._read_local, "conn", None)
        if conn is not None:
            return conn
        # 上限检查：满则用临时只读连接（不注册，本线程下次仍走主路径）
        with self._read_conns_lock:
            over = len(self._read_conns) >= self._read_conn_max
        if over:
            self._read_conn_overflow += 1
            try:
                conn = sqlite3.connect(
                    f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0,
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=15000;")
                return conn
            except Exception:
                return None
        try:
            conn = sqlite3.connect(
                f"file:{self.db_path}?mode=ro", uri=True, timeout=10.0,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000;")
            self._read_local.conn = conn
            with self._read_conns_lock:
                self._read_conns.add(conn)
            return conn
        except Exception:
            return None
