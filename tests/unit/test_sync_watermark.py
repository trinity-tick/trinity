"""sync_pool_from_db_v2 watermark 单元测试（2026-08-21，P0-2）。

覆盖（临时 SQLite 库，monkeypatch 模块级 SRC_DB，不触碰生产库）：
- sync_watermarks 表自动建表
- 初始水位 "0"，advance 后可读回
- rowid 单调性验证（memory_id 前缀混杂时 rowid 仍按插入序）
- 增量查询语义：rowid > watermark 只返回新增行
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "sync_pool_mod", REPO_ROOT / "benchmark" / "sync_pool_from_db_v2.py"
)
sp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sp)


@pytest.fixture()
def src_db(tmp_path, monkeypatch):
    d = tmp_path / "src.db"
    monkeypatch.setattr(sp, "SRC_DB", str(d))
    con = sqlite3.connect(str(d))
    con.execute(
        "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT, category TEXT, "
        "created_at TEXT, status TEXT)"
    )
    con.commit()
    con.close()
    return str(d)


def test_ensure_table_and_initial_watermark(src_db):
    con = sqlite3.connect(src_db)
    sp._ensure_watermark_table(con)
    assert sp._read_watermark(con) == "0"
    con.close()


def test_advance_and_read(src_db):
    con = sqlite3.connect(src_db)
    sp._ensure_watermark_table(con)
    sp._advance_watermark(42)
    assert sp._read_watermark(con) == "42"
    sp._advance_watermark(99)
    assert sp._read_watermark(con) == "99"
    con.close()


def test_rowid_monotonic_with_mixed_memory_ids(src_db):
    """memory_id 前缀/长度混杂（mem_*/sync_*）时 rowid 仍按插入序单调。"""
    con = sqlite3.connect(src_db)
    for i, mid in enumerate(["mem_aaaaaaaaaaaaaaaa", "sync_bbbbbbbbbbbbbb", "mem_cccccccccccccccc"]):
        con.execute(
            "INSERT INTO memories (memory_id, content, category, created_at, status) "
            "VALUES (?,?,?,?,?)",
            (mid, f"content-{i}", "general", "2026-08-21T00:00:00", "active"),
        )
    con.commit()
    rows = con.execute("SELECT rowid, memory_id FROM memories ORDER BY rowid").fetchall()
    assert [r[0] for r in rows] == [1, 2, 3]
    assert rows[1][1].startswith("sync_")  # 混杂前缀无碍 rowid 序
    con.close()


def test_incremental_query_semantics(src_db):
    con = sqlite3.connect(src_db)
    sp._ensure_watermark_table(con)
    for i in range(3):
        con.execute(
            "INSERT INTO memories (memory_id, content, category, created_at, status) "
            "VALUES (?,?,?,?,?)",
            (f"mem_{i:016x}", f"c{i}", "general", "2026-08-21T00:00:00", "active"),
        )
    con.commit()
    con.execute(
        "INSERT INTO memories (memory_id, content, category, created_at, status) "
        "VALUES ('mem_deadbeef', 'old', 'general', '2026-08-01T00:00:00', 'archived')"
    )
    con.commit()
    # 处理前 2 条 → 水位 rowid=2 → 增量查询只返回 rowid>2 的行
    # （rowid 3='mem_0000000000000002'、rowid 4='mem_deadbeef'；archived 也返回，
    #  脚本过滤条件与原文一致：status != 'deleted'）
    sp._advance_watermark(2)
    wm = sp._read_watermark(con)
    rows = con.execute(
        "SELECT rowid, memory_id FROM memories WHERE status != 'deleted' AND rowid > ? ORDER BY rowid",
        (wm,),
    ).fetchall()
    assert [r[1] for r in rows] == ["mem_0000000000000002", "mem_deadbeef"]
    con.close()
