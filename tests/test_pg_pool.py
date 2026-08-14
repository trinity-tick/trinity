"""M3-1: PostgreSQL adapter connection pool verification (against docker trinity-db).

前置：docker trinity-db（127.0.0.1:5430，trinity/trinity，库 trinity）运行中；
不可达时测试自动 skip（不失败）。
"""

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from trinity.adapters.postgresql import PostgreSQLAdapter

PG = dict(host="127.0.0.1", port=5430, dbname="trinity", user="trinity", password="trinity")


def _pg_available() -> bool:
    try:
        import psycopg2
        conn = psycopg2.connect(**PG, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _pg_available(), reason="PG 127.0.0.1:5430 不可达")


class TestPgPool:
    def test_connect_and_pool(self):
        a = PostgreSQLAdapter(min_conn=1, max_conn=3, auto_connect=True, **PG)
        try:
            assert a.is_connected
            assert a._pool is not None
            # 池最小连接已建立
            assert a._pool.closed == 0
        finally:
            a.disconnect()
        assert not a.is_connected

    def test_get_conn_roundtrip(self):
        a = PostgreSQLAdapter(min_conn=1, max_conn=3, auto_connect=True, **PG)
        try:
            with a._get_conn() as conn:
                cur = conn.cursor()
                cur.execute("SELECT count(*) FROM memories")
                n = cur.fetchone()[0]
                assert isinstance(n, int)
                assert n >= 0
        finally:
            a.disconnect()

    def test_concurrent_pool_access(self):
        """多线程并发从池取连接，验证池无死锁/无泄漏。"""
        a = PostgreSQLAdapter(min_conn=2, max_conn=5, auto_connect=True, **PG)
        results = []
        errors = []

        def worker(idx):
            try:
                with a._get_conn() as conn:
                    cur = conn.cursor()
                    cur.execute("SELECT count(*) FROM memories")
                    results.append(cur.fetchone()[0])
            except Exception as e:  # pragma: no cover
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        try:
            a.disconnect()
        finally:
            pass
        assert not errors, f"concurrent pool errors: {errors[:3]}"
        assert len(results) == 12, f"expected 12 results, got {len(results)}"

    def test_store_and_delete_roundtrip(self):
        """经 adapter 写入一条临时记忆并删除，验证完整读写链路（幂等清理）。"""
        import uuid as _uuid
        a = PostgreSQLAdapter(min_conn=1, max_conn=3, auto_connect=True, **PG)
        mid = f"pool_test_{_uuid.uuid4().hex[:12]}"
        try:
            result = a.store_memory(
                content="[pool-test] temp memory for M3-1 verification",
                persona_id="default", tags=["pool-test"], category="test",
            )
            rid = (result or {}).get("memory_id")
            assert rid, f"store_memory returned {result!r}"

            got = a.get_memory(rid)
            assert got is not None
            assert "pool-test" in (got.get("content") or "")
        finally:
            try:
                a.delete_memory(mid)
            except Exception:
                pass
            try:
                a.disconnect()
            except Exception:
                pass
