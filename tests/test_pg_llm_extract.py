"""Trinity — PG 适配器 + LLM 抽取异步开关回归（2026-08-16 深挖建议①④）。

覆盖：
1. PostgreSQLAdapter 读写路径（若原生 PG :5432 可用；不可用则 skip）
2. TRINITY_LLM_EXTRACT 默认异步（2026-08-16 优化：真实 LLM 提取 ~4.5s/条，同步阻塞写路径）
3. TRINITY_LLM_EXTRACT_SYNC=on 强制同步（调用方期望返回时实体已入库）
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.core.client import Trinity

_PROD_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")


def _vacuum_copy(src: str) -> str:
    tmp = tempfile.mkdtemp(prefix="llmpg_")
    db = os.path.join(tmp, "c.db")
    conn = sqlite3.connect(src)
    conn.execute("VACUUM INTO '" + db.replace("\\", "\\\\") + "'")
    conn.close()
    return db


class TestLLMExtractAsync:
    def test_default_async_semantics(self, monkeypatch):
        """TRINITY_LLM_EXTRACT=on 默认异步（2026-08-16 优化）：ingest 即时返回，后台完成。"""
        monkeypatch.setenv("TRINITY_LLM_EXTRACT", "on")
        db = _vacuum_copy(_PROD_DB)
        mem = Trinity(store_path=db)
        t0 = time.time()
        r = mem.ingest("Alice 和 Bob 在 Trinity 项目上协作", persona_id="p",
                       metadata={"category": "x"})
        # 冷启动（jieba 词典首次加载 ~1.1s）容忍：<3s 仍远低于真实 LLM 提取 4.5s/条
        assert time.time() - t0 < 3.0, f"异步应即时返回，实际 {time.time()-t0:.2f}s"
        assert r.get("postprocess") in ("pending", "done")
        deadline = time.time() + 30
        while time.time() < deadline and r.get("postprocess") == "pending":
            time.sleep(0.5)
        assert r.get("postprocess") == "done", "后台加工应完成"

    def test_sync_optin(self, monkeypatch):
        """TRINITY_LLM_EXTRACT=on + TRINITY_LLM_EXTRACT_SYNC=on：返回时 done。"""
        monkeypatch.setenv("TRINITY_LLM_EXTRACT", "on")
        monkeypatch.setenv("TRINITY_LLM_EXTRACT_SYNC", "on")
        db = _vacuum_copy(_PROD_DB)
        mem = Trinity(store_path=db)
        r = mem.ingest("Alice 和 Bob 在 Trinity 项目上协作", persona_id="p",
                       metadata={"category": "x"})
        assert r.get("postprocess") == "done", \
            f"同步 opt-in 应返回 done，实际 {r.get('postprocess')}"

    def test_async_switch_returns_immediately(self, monkeypatch):
        """TRINITY_LLM_EXTRACT_ASYNC=on：ingest 即时返回 pending，后台完成。"""
        monkeypatch.setenv("TRINITY_LLM_EXTRACT", "on")
        monkeypatch.setenv("TRINITY_LLM_EXTRACT_ASYNC", "on")
        db = _vacuum_copy(_PROD_DB)
        mem = Trinity(store_path=db)
        t0 = time.time()
        r = mem.ingest("Carol 和 Dave 在 GraphRAG 项目上协作", persona_id="p",
                       metadata={"category": "x"})
        elapsed = time.time() - t0
        assert elapsed < 1.0, f"异步应即时返回，实际 {elapsed:.2f}s"
        assert r.get("postprocess") in ("pending", "done")
        # 等后台收敛
        deadline = time.time() + 30
        while time.time() < deadline and r.get("postprocess") == "pending":
            time.sleep(0.5)
        assert r.get("postprocess") == "done", "后台加工应完成"

    def test_no_llm_switch_falls_to_default_async(self, monkeypatch):
        """无 LLM 开关时走默认后台异步（与二轮行为一致）。"""
        monkeypatch.delenv("TRINITY_LLM_EXTRACT", raising=False)
        db = _vacuum_copy(_PROD_DB)
        mem = Trinity(store_path=db)
        t0 = time.time()
        r = mem.ingest("默认路径记忆 数据库", persona_id="p",
                       metadata={"category": "x"})
        assert time.time() - t0 < 1.0, "默认应异步返回"
        assert r.get("postprocess") == "pending"


def _maintenance_pg_available(host: str = "127.0.0.1", port: int = 5430) -> bool:
    """维护 PG（docker trinity-db :5430）可用性探测；不可用返回 False（测试 skip）。

    2026-08-16（价值评估收敛三库拓扑）：原生 PG16 :5432 已停用，PG 适配器
    测试改指维护 PG :5430；探测必须容错（create_connection 在端口关闭时会
    抛 OSError/TimeoutError，不能让 skipif 条件在收集期抛异常）。
    """
    import socket
    try:
        s = socket.create_connection((host, port), 2)
        s.close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(
    not _maintenance_pg_available(),
    reason="maintenance PG (docker trinity-db :5430) not available")
class TestPostgresAdapter:
    def test_read_write_rollback_roundtrip(self):
        """PG 适配器读路径 + 显式事务写回滚（零污染）。"""
        from trinity.adapters.postgresql import PostgreSQLAdapter
        adapter = PostgreSQLAdapter(
            host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5430")),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
        adapter.connect()
        try:
            hits = adapter.search_memories("数据库", top_k=5)
            assert isinstance(hits, list)
            # 写路径（显式事务 ROLLBACK 不落库）
            import psycopg2
            conn = adapter._pool.getconn()
            try:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO memories (memory_id, persona_id, content, "
                    "status, version, created_at, updated_at) "
                    "VALUES (%s, %s, %s, 'active', 1, now(), now())",
                    ("mem_pgtest_rollback", "pg-test-rollback",
                     "回滚验证记忆"))
                conn.rollback()
                cur.close()
            finally:
                adapter._pool.putconn(conn)
            conn2 = adapter._pool.getconn()
            try:
                cur = conn2.cursor()
                cur.execute(
                    "SELECT COUNT(*) FROM memories "
                    "WHERE persona_id='pg-test-rollback'")
                n = cur.fetchone()[0]
                cur.close()
            finally:
                adapter._pool.putconn(conn2)
            assert n == 0, f"回滚应不落库，实际 {n}"
        finally:
            adapter.disconnect()
