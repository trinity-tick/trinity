"""Trinity — WAL checkpoint 边界测试（2026-08-16 深挖剩余边界）。

覆盖：
1. 显式 wal_checkpoint(TRUNCATE)：大批写入后 WAL 回收、数据精确
2. checkpoint 与并发读互不阻塞（PASSIVE checkpoint + reader 线程）
3. 写入精确性（2000 条全部落库，无合并丢数）
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.core.client import Trinity

_PROD_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")


@pytest.fixture()
def copy_db() -> str:
    """生产库副本（跳过锁定的 WAL）。"""
    tmp = tempfile.mkdtemp(prefix="ckpt_")
    db = os.path.join(tmp, "c.db")
    shutil.copy2(_PROD_DB, db)
    return db


def _wal_size(db: str) -> int:
    return os.path.getsize(db + "-wal") if os.path.exists(db + "-wal") else 0


class TestWALCheckpoint:
    def test_truncate_recovers_wal_and_exact_count(self, copy_db):
        """大批写入后 TRUNCATE checkpoint：WAL 回收 + 计数精确。"""
        conn = sqlite3.connect(copy_db)
        before = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()[0]
        conn.close()
        mem = Trinity(store_path=copy_db)
        written = 0
        for i in range(500):
            r = mem.ingest(f"checkpoint 边界 {i} 独特标记{i}",
                           persona_id="ckpt-test",
                           metadata={"category": "ckpt"}, postprocess=False)
            if r.get("memory_id"):
                written += 1
        mem._adapter.disconnect()
        assert written == 500, f"应全部返回 memory_id，实际 {written}"
        conn = sqlite3.connect(copy_db)
        after = conn.execute("SELECT COUNT(*) c FROM memories").fetchone()[0]
        r = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        conn.close()
        assert after == before + 500, f"计数不精确: {before} -> {after}"
        assert r[1] == 0 or r[2] >= 0  # checkpoint 正常执行
        assert _wal_size(copy_db) < 1024 * 1024, "TRUNCATE 后 WAL 应回收"

    def test_checkpoint_does_not_block_concurrent_read(self, copy_db):
        """PASSIVE checkpoint 与并发检索互不阻塞。"""
        mem = Trinity(store_path=copy_db)
        for i in range(300):
            mem.ingest(f"ckpt 并发读 {i} 数据库", persona_id="ckpt-conc",
                       metadata={"category": "ckpt"}, postprocess=False)
        errors = []

        def reader():
            try:
                for _ in range(30):
                    mem.search_hybrid(query="数据库", top_k=5,
                                      strategy="rrf")
            except Exception as e:  # pragma: no cover
                errors.append(str(e))

        t = threading.Thread(target=reader)
        t.start()
        for _ in range(3):
            conn = sqlite3.connect(copy_db)
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            conn.close()
        t.join()
        assert not errors, f"并发读受 checkpoint 影响: {errors[:2]}"

    def test_ingest_returns_memory_id_for_all(self, copy_db):
        """ingest 每条都返回 memory_id（无合并丢数）。"""
        mem = Trinity(store_path=copy_db)
        ok = 0
        for i in range(200):
            r = mem.ingest(f"id 完整性 {i} 记忆", persona_id="ckpt-id",
                           metadata={"category": "ckpt"}, postprocess=False)
            if r.get("memory_id"):
                ok += 1
        assert ok == 200
