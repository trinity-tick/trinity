# -*- coding: utf-8 -*-
"""Fable 5.1 对照审计 P1-④：条目级 expires_at + 过期复核队列（2026-09-02）。

覆盖：
- ingest 写入即到期（expires_at 过去时刻）→ 立即归档 + 审计 EXPIRED_AT(source=ingest)
- run_expiry_review（sqlite 路径）：expired/due 分组、horizon 边界、dry-run 出队列文件
- --apply-expired：status→expired + 链式审计 EXPIRED_AT(source=expiry-review)
"""
import datetime as dt
import json
import sqlite3

import pytest

AGENT = "fable-expiry-test"


def _db_path(tmp_path):
    return str(tmp_path / "trinity_store.db")


def _client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORAGE_BACKEND", "sqlite")
    from trinity.core.client import Trinity
    return Trinity(store_path=str(tmp_path), adapter="sqlite",
                   evolution_enabled=False)


def _audit_actions(db, action=None):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT action, details, memory_id FROM audit_log ORDER BY timestamp, id").fetchall()
    con.close()
    if action is not None:
        return [dict(r) for r in rows if r["action"] == action]
    return [dict(r) for r in rows]


def _row(db, memory_id):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT memory_id, status, metadata FROM memories WHERE memory_id=?",
                    (memory_id,)).fetchone()
    con.close()
    return dict(r) if r else None


def _iso(offset_minutes):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=offset_minutes)).isoformat()


def test_ingest_past_expiry_archives_immediately(tmp_path, monkeypatch):
    cli = _client(tmp_path, monkeypatch)
    res = cli.ingest("临时验证码 A1B2C3 明天过期",
                     agent_id=AGENT, postprocess=False,
                     metadata={"expires_at": _iso(-30)})
    assert res.get("memory_id")
    row = _row(_db_path(tmp_path), res["memory_id"])
    assert row["status"] == "archived"
    acts = _audit_actions(_db_path(tmp_path), "EXPIRED_AT")
    assert len(acts) == 1 and "ingest" in acts[0]["details"]


def test_ingest_future_expiry_stays_active(tmp_path, monkeypatch):
    cli = _client(tmp_path, monkeypatch)
    res = cli.ingest("临时安排：周五前有效",
                     agent_id=AGENT, postprocess=False,
                     metadata={"expires_at": _iso(60 * 24 * 5)})
    assert res.get("memory_id")
    assert _row(_db_path(tmp_path), res["memory_id"])["status"] == "active"


def test_review_due_and_boundary(tmp_path, monkeypatch):
    cli = _client(tmp_path, monkeypatch)
    due_mid = cli.ingest("一周内要复核的临时口令 8899",
                         agent_id=AGENT, postprocess=False,
                         metadata={"expires_at": _iso(60 * 24 * 3)})["memory_id"]
    far_mid = cli.ingest("很久以后才过期的长期偏好",
                         agent_id=AGENT, postprocess=False,
                         metadata={"expires_at": _iso(60 * 24 * 30)})["memory_id"]
    from scripts.run_expiry_review import run_review
    report = run_review(store="sqlite", db_path=_db_path(tmp_path),
                        horizon_days=7, limit=100, out_dir=str(tmp_path))
    assert report["counts"]["expired"] == 0
    due_ids = [x["memory_id"] for x in report["due"]]
    assert due_mid in due_ids and far_mid not in due_ids
    assert report["expired"] == []
    # 队列报告落盘可读
    with open(report["out_file"], encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["counts"]["due"] >= 1


def test_review_apply_expired(tmp_path, monkeypatch):
    # 直插一条"写入后过期"的记忆（绕过 ingest 即时归档，模拟时间流逝）
    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=_db_path(tmp_path))
    ad.connect()
    try:
        stored = ad.store_memory("昨日到期的验证令牌 777",
                                 agent_id=AGENT,
                                 metadata={"expires_at": _iso(-60 * 24)})
    finally:
        ad.disconnect()
    mid = stored["memory_id"]
    assert _row(_db_path(tmp_path), mid)["status"] == "active"

    from scripts.run_expiry_review import run_review
    report = run_review(store="sqlite", db_path=_db_path(tmp_path),
                        horizon_days=7, limit=100, apply_expired=True,
                        out_dir=str(tmp_path))
    assert report["counts"]["expired"] >= 1
    assert report.get("applied", {}).get("count", 0) >= 1
    assert _row(_db_path(tmp_path), mid)["status"] == "expired"
    acts = _audit_actions(_db_path(tmp_path), "EXPIRED_AT")
    assert any("expiry-review" in a["details"] for a in acts)
