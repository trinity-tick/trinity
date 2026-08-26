"""P0-1: connect 失败不再静默 + 只读降级 (COMPARISON_VS_2026_SOTA_R9).

Verifies:
  - Trinity() connect 失败时记录 _engine_error（不再静默 None）
  - SQLiteAdapter 在 schema 写锁失败时进入只读模式（检索可用、写报错）
  - /health 在 engine 不可用时报 degraded 而非 ok
"""

import logging
import os
import sqlite3

import pytest

from trinity.adapters.sqlite import SQLiteAdapter
from trinity.core.client import Trinity


def test_trinity_readonly_degrade_on_lock(monkeypatch, tmp_path, caplog):
    """锁竞争 → connect 不再整体失败：adapter 进入只读模式 + WARN 日志。

    R9 P0-1c 解耦后：schema 写锁失败降级只读而非抛异常——检索可用、
    写操作明确报错、有日志（不再是静默 0 hits 健康假象）。
    """
    db = str(tmp_path / "store" / "trinity_store.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    # 先建好库表，再用持写锁的连接阻塞新连接建表
    a = SQLiteAdapter(db_path=db)
    a.connect()
    a.store_memory(content="seed", persona_id="p")
    a.disconnect()

    # 持写锁（未提交事务）
    locker = sqlite3.connect(db, timeout=1)
    locker.execute("BEGIN IMMEDIATE")

    monkeypatch.setenv("TRINITY_DB_PATH", db)
    monkeypatch.delenv("TRINITY_STORE", raising=False)
    with caplog.at_level(logging.WARNING, logger="trinity.adapters.sqlite"):
        mem = Trinity()
    assert mem._adapter is not None
    assert mem._adapter._readonly_mode is True
    assert mem._engine_error is None  # 降级而非失败
    assert any("READONLY mode" in r.message for r in caplog.records)
    # 检索可用
    hits = mem.search("seed", top_k=5)
    results = hits.get("results", []) if isinstance(hits, dict) else hits
    assert any("seed" in str(h.get("content", "")) for h in results)
    # 写操作明确报错
    r = mem.ingest("new", tags=["x"])
    assert r.get("error") or r.get("memory_id", "") == ""
    locker.close()


def test_adapter_readonly_mode_on_schema_lock(tmp_path):
    """schema 写锁失败 → 只读模式：检索可用、写报错。"""
    db = str(tmp_path / "store" / "trinity_store.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    a = SQLiteAdapter(db_path=db)
    a.connect()
    a.store_memory(content="seed data", persona_id="p")
    a.disconnect()

    # 持写锁模拟其他进程
    locker = sqlite3.connect(db, timeout=1)
    locker.execute("BEGIN IMMEDIATE")

    b = SQLiteAdapter(db_path=db)
    b.connect()
    assert b._readonly_mode is True  # 建表失败 → 只读降级

    # 检索仍可用（只读连接）
    hits = b.search_memories("seed", top_k=5)
    assert any("seed" in str(h.get("content", "")) for h in hits)

    # 写操作明确报错
    r = b.store_memory(content="x", persona_id="p")
    assert r.get("error") and "readonly" in r.get("error", "").lower()

    b.disconnect()
    locker.close()


def test_health_reports_engine_degraded(monkeypatch, tmp_path):
    """engine 不可用 → /health status=degraded + engine 组件 degraded。"""
    from fastapi.testclient import TestClient
    from trinity.api.server import app
    import trinity.api.server._routers_health as health_mod

    # 构造 adapter=None 的假 engine（patch health 模块使用的 get_memory）
    class _FakeMem:
        _adapter = None
        _engine_error = "OperationalError: database is locked"

    monkeypatch.setattr(health_mod, "get_memory", lambda: _FakeMem())

    with TestClient(app) as client:
        r = client.get("/health")
        body = r.json()
        assert body["status"] == "degraded"
        assert body["components"]["engine"] == "degraded"
        assert "database is locked" in (body.get("engine_error") or "")


def test_health_reports_ok_when_engine_healthy(monkeypatch, tmp_path):
    """engine 正常 → /health ok（回归保护）。"""
    from fastapi.testclient import TestClient
    from trinity.api.server import app
    import trinity.api.server._deps as deps

    db = str(tmp_path / "store" / "trinity_store.db")
    os.makedirs(os.path.dirname(db), exist_ok=True)
    a = SQLiteAdapter(db_path=db)
    a.connect()
    a.store_memory(content="seed", persona_id="p")

    class _FakeMem:
        _adapter = a
        _engine_error = None

    monkeypatch.setattr(deps, "get_memory", lambda: _FakeMem())

    with TestClient(app) as client:
        r = client.get("/health")
        body = r.json()
        assert body["status"] in ("ok", "degraded")  # aggregator 可能 unavailable
        assert body["components"]["engine"] == "healthy"
    a.disconnect()
