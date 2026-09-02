# -*- coding: utf-8 -*-
"""产品层关键路径测试（2026-09-01，短板"验证贫困"补强第二批 v2）

覆盖 adapter CRUD + 审计链（SQLite 本地临时库 / PG 线上库带清理）。
API 实况（2026-09-01 探明）：search_memories 返回 List[Dict]，无 mode 参数；
update_memory 用关键字参数；SQLiteAdapter 属性为 db_path。
"""
import hashlib
import os
import sqlite3

import pytest

TEST_TAG = "ops_test_20260901"


@pytest.fixture()
def sq_adapter(tmp_path):
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    from trinity.adapters.sqlite import SQLiteAdapter
    db = str(tmp_path / "store.db")
    a = SQLiteAdapter(db_path=db)
    a.connect()
    yield a
    try:
        a.disconnect()
    except Exception:
        pass


def test_sqlite_store_encrypts_content(sq_adapter):
    content = TEST_TAG + " encryption verify"
    r = sq_adapter.store_memory(content=content, category="general", tags=["t"])
    mid = r["memory_id"]
    assert mid
    raw = sqlite3.connect(sq_adapter.db_path).execute(
        "SELECT content, sha256_hash FROM memories WHERE memory_id=?", (mid,)).fetchone()
    assert raw is not None
    stored, sha = raw
    if sq_adapter._cipher is not None:
        assert content not in stored, "content 必须密文落盘"
    assert sha == hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_sqlite_dedup_by_content_hash(sq_adapter):
    content = TEST_TAG + " dedup target"
    r1 = sq_adapter.store_memory(content=content, persona_id="p", agent_id="a", category="general")
    r2 = sq_adapter.store_memory(content=content, persona_id="p", agent_id="a", category="general")
    assert r1["memory_id"] == r2["memory_id"], "同内容同租户应幂等去重"
    assert r2.get("dedup") is True


def test_sqlite_search_hits(sq_adapter):
    content = TEST_TAG + " search WMS ASN preadvice"
    sq_adapter.store_memory(content=content, category="general")
    res = sq_adapter.search_memories(query="preadvice", top_k=5)
    assert isinstance(res, list), "search_memories 返回列表"
    assert any(TEST_TAG in (r.get("content") or "") for r in res), "检索应命中刚写入内容"


def test_sqlite_update_and_archive(sq_adapter):
    r = sq_adapter.store_memory(content=TEST_TAG + " update target", category="general")
    mid = r["memory_id"]
    upd = sq_adapter.update_memory(mid, importance=0.9)
    assert upd is not None
    n = sq_adapter.archive_memories([mid])
    assert n >= 1
    st = sqlite3.connect(sq_adapter.db_path).execute(
        "SELECT status FROM memories WHERE memory_id=?", (mid,)).fetchone()[0]
    assert st == "archived"


def test_sqlite_audit_chain(sq_adapter):
    r = sq_adapter.store_memory(content=TEST_TAG + " audit target", category="general")
    mid = r["memory_id"]
    sq_adapter.write_audit_log(memory_id=mid, action="TEST_AUDIT", agent_id="ops-test",
                               details={"k": "v"})
    n = sqlite3.connect(sq_adapter.db_path).execute(
        "SELECT COUNT(*) FROM audit_log WHERE memory_id=? AND action='TEST_AUDIT'", (mid,)).fetchone()[0]
    assert n == 1


@pytest.fixture()
def pg_adapter():
    from trinity.adapters.postgresql import PostgreSQLAdapter
    a = PostgreSQLAdapter(host="127.0.0.1", port=5432, dbname="trinity",
                          user="trinity", password="trinity", auto_connect=True)
    yield a
    try:
        a.disconnect()
    except Exception:
        pass


def _pg_cleanup(mid):
    import psycopg2
    c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                         user="trinity", password="trinity")
    cur = c.cursor()
    cur.execute("DELETE FROM memories WHERE memory_id=%s", (mid,))
    cur.execute("DELETE FROM audit_log WHERE memory_id=%s", (mid,))
    c.commit()
    c.close()


def test_pg_store_search_archive_roundtrip(pg_adapter):
    import uuid
    token = "pgrt_" + uuid.uuid4().hex[:8]  # 唯一 token 避免被既有结果挤出 top-k
    content = TEST_TAG + " " + token + " content"
    r = pg_adapter.store_memory(content=content, category="general", tags=["ops_test"])
    mid = r["memory_id"]
    try:
        res = pg_adapter.search_memories(query=token, top_k=5)
        assert isinstance(res, list)
        assert any(x.get("memory_id") == mid for x in res), "PG 检索应命中（按 memory_id 断言）"
        n = pg_adapter.archive_memories([mid])
        assert n >= 1
    finally:
        _pg_cleanup(mid)


def test_pg_audit_write(pg_adapter):
    r = pg_adapter.store_memory(content=TEST_TAG + " PG audit target", category="general")
    mid = r["memory_id"]
    try:
        pg_adapter.write_audit_log(memory_id=mid, action="TEST_PG_AUDIT", agent_id="ops-test",
                                   details={"k": "v"})
        import psycopg2
        c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                             user="trinity", password="trinity")
        cur2 = c.cursor()
        cur2.execute("SELECT COUNT(*) FROM audit_log WHERE memory_id=%s AND action='TEST_PG_AUDIT'", (mid,))
        n = cur2.fetchone()[0]
        c.close()
        assert n == 1
    finally:
        _pg_cleanup(mid)
