"""job_lease 治理任务租约单元测试（2026-08-21，P0-1 Codex job claim 借鉴）。

覆盖纯逻辑（临时 SQLite 库，不触碰生产库）：
- 认领 → 释放 → 再认领
- 租约有效期内并发重复认领 → SKIP（held_by 保持原 owner）
- 租约过期 → steal 接管
- job_key 隔离（不同 key 互不影响）
- list_jobs 诊断输出
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from trinity.governance.job_lease import (
    acquire,
    default_owner,
    list_jobs,
    release,
)


@pytest.fixture()
def lease_db(tmp_path):
    db = tmp_path / "lease.db"
    sqlite3.connect(str(db)).close()  # 确保文件存在
    yield str(db)


def test_claim_release_reclaim(lease_db):
    r1 = acquire("decay", db_path=lease_db)
    assert r1["acquired"] and r1["reason"] == "claimed"
    ok = release("decay", status="completed", detail="ok", db_path=lease_db)
    assert ok
    r2 = acquire("decay", db_path=lease_db)
    # 释放后租约已过期 → 再认领走 steal 语义（acquired=True 即可）
    assert r2["acquired"] and r2["reason"] in ("claimed", "stolen")


def test_skip_while_held(lease_db):
    a = acquire("tiers", lease_seconds=3600, db_path=lease_db)
    assert a["acquired"]
    b = acquire("tiers", lease_seconds=3600, db_path=lease_db)
    assert not b["acquired"]
    assert b["reason"] == "skipped"
    assert b["held_by"] == a["owner"]


def test_steal_after_expiry(lease_db):
    a = acquire("mirror", lease_seconds=1, db_path=lease_db)
    assert a["acquired"]
    b = acquire("mirror", lease_seconds=3600, db_path=lease_db, now=a["expires_at"] + 1)
    assert b["acquired"]
    assert b["reason"] == "stolen"
    assert b["held_by"] == a["owner"]
    assert b["previous_status"] == "running"


def test_skip_does_not_change_owner(lease_db):
    a = acquire("compact", lease_seconds=3600, db_path=lease_db)
    b = acquire("compact", lease_seconds=3600, db_path=lease_db)
    assert b["held_by"] == a["owner"]
    rows = list_jobs(lease_db)
    assert len(rows) == 1
    assert rows[0]["owner"] == a["owner"]


def test_job_key_isolation(lease_db):
    a = acquire("sync", job_key="global", db_path=lease_db)
    b = acquire("sync", job_key="agent-a", db_path=lease_db)
    assert a["acquired"] and b["acquired"]
    c = acquire("sync", job_key="global", db_path=lease_db)
    assert not c["acquired"]


def test_release_records_status(lease_db):
    acquire("dedup", db_path=lease_db)
    release("dedup", status="failed", detail="boom", db_path=lease_db)
    rows = list_jobs(lease_db)
    assert rows[0]["status"] == "failed"
    assert rows[0]["detail"] == "boom"
    assert rows[0]["finished_at"] is not None
    # 释放后租约立即过期 → 可再认领
    r = acquire("dedup", db_path=lease_db)
    assert r["acquired"]


def test_default_owner_shape(lease_db):
    owner = default_owner()
    assert ":" in owner and str(os.getpid()) in owner


def test_locked_reason_on_held_transaction(lease_db, monkeypatch):
    """持有写事务时 acquire 不崩溃，返回 locked（模拟锁竞争降级）。"""
    monkeypatch.setenv("TRINITY_SQLITE_BUSY_TIMEOUT_MS", "200")
    con = sqlite3.connect(lease_db, timeout=1)
    con.execute("BEGIN IMMEDIATE")
    con.execute(
        "CREATE TABLE IF NOT EXISTS governance_jobs ("
        "job_kind TEXT NOT NULL, job_key TEXT NOT NULL DEFAULT 'global', owner TEXT NOT NULL, "
        "lease_expires_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'running', "
        "started_at REAL NOT NULL, finished_at REAL, detail TEXT DEFAULT '', "
        "PRIMARY KEY (job_kind, job_key))"
    )
    con.execute(
        "INSERT INTO governance_jobs (job_kind, job_key, owner, lease_expires_at, status, started_at) "
        "VALUES ('consolidate', 'global', 'other', 9999999999, 'running', 0)"
    )
    try:
        r = acquire("consolidate", lease_seconds=1, db_path=lease_db)
        assert not r["acquired"]
        assert r["reason"] in ("locked", "skipped")
    finally:
        con.rollback()
        con.close()
