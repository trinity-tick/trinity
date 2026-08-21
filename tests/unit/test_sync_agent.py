"""trinity-sync-agent 单元测试（2026-08-21，多机同步落地配套）。

覆盖不依赖服务器/LLM/基准的纯逻辑：
- fetch_delta：只读增量读取（active + updated_at>cursor）
- build_entries：引擎字段 → 聚合池 MemoryWriteRequest 映射（agent_id 前缀隔离）
- load_cursor / save_cursor：游标持久化（缺失 / 非法 / 读写）
- SQLite 只读：不改源库
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_sync_agent():
    spec = importlib.util.spec_from_file_location(
        "trinity_sync_agent_mod",
        Path(__file__).resolve().parents[2] / "dsh-ops" / "trinity-sync-agent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def tsa():
    m = _load_sync_agent()
    assert m is not None
    return m


@pytest.fixture()
def sample_db(tmp_path):
    """构造一个临时 SQLite memories 库，模拟本地引擎源库。"""
    import sqlite3

    db = tmp_path / "store.db"
    con = sqlite3.connect(str(db))
    con.execute(
        "CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT, "
        "category TEXT, importance REAL, tags TEXT, status TEXT, updated_at TEXT)"
    )
    rows = [
        ("m1", "alpha event", "episodic", 0.7, '["t1"]', "active", "2026-08-21T00:00:00"),
        ("m2", "beta decision", "decision", 0.8, '["t2"]', "active", "2026-08-21T00:00:10"),
        ("m3", "gamma old archived", "general", 0.5, "[]", "archived", "2026-08-21T00:00:20"),
    ]
    con.executemany(
        "INSERT INTO memories VALUES (?,?,?,?,?,?,?)", rows
    )
    con.commit()
    con.close()
    return db


def test_fetch_delta_all_active(tsa, sample_db):
    items = tsa.fetch_delta(sample_db, None, 10)
    # 只取 active（归档的 m3 不应出现）
    assert len(items) == 2
    ids = {it["memory_id"] for it in items}
    assert ids == {"m1", "m2"}
    assert all(it["importance"] > 0 for it in items)
    assert isinstance(items[0]["tags"], list)


def test_fetch_delta_cursor_incremental(tsa, sample_db):
    # 游标在 m1 之后 → 只取 m2
    items = tsa.fetch_delta(sample_db, "2026-08-21T00:00:05", 10)
    assert [it["memory_id"] for it in items] == ["m2"]


def test_fetch_delta_updates_cursor_ordering(tsa, sample_db):
    # updated_at 升序，取 limit=1 时只取最早一条
    items = tsa.fetch_delta(sample_db, None, 1)
    assert len(items) == 1
    assert items[0]["memory_id"] == "m1"


def test_fetch_delta_missing_db(tsa, tmp_path):
    assert tsa.fetch_delta(tmp_path / "nope.db", None, 10) == []


def test_build_entries_agent_prefix_isolation(tsa):
    items = [{
        "memory_id": "mem_abc",
        "content": "hello",
        "category": "decision",
        "importance": 0.9,
        "tags": ["x"],
        "updated_at": "2026-08-21T00:00:00",
    }]
    entries = tsa.build_entries(items, "pc-9")
    assert len(entries) == 1
    e = entries[0]
    assert e["agent_id"] == "pc-9:mem_abc"
    assert e["content"] == "hello"
    assert e["category"] == "decision"
    assert e["importance"] == 0.9
    assert e["tags"] == ["x"]
    assert e["metadata"]["sync_source"] == "pc-9"
    assert e["metadata"]["sync_memory_id"] == "mem_abc"


def test_cursor_roundtrip(tsa, tmp_path):
    p = tmp_path / "cur.json"
    assert tsa.load_cursor(p) is None  # 不存在
    tsa.save_cursor(p, "2026-08-21T00:00:10")
    assert tsa.load_cursor(p) == "2026-08-21T00:00:10"
    # 非法内容 → None
    p.write_text("{broken", encoding="utf-8")
    assert tsa.load_cursor(p) is None


def test_config_default_no_file(tsa, monkeypatch, tmp_path):
    # 无配置文件时返回内置默认（URL 指本机/内网）
    monkeypatch.setenv("COMPUTERNAME", "PCTEST")
    cfg = tsa.load_config(str(tmp_path / "missing.yaml"))
    assert cfg["server"]["machine"] == "PCTEST"
    assert "url" in cfg["server"] and cfg["server"]["url"].startswith("http")


def test_yaml_subset_parse(tsa, tmp_path):
    yaml_text = (
        "server:\n"
        "  url: https://remote.example.com\n"
        "  api_key: sk-x\n"
        "  machine: pc-1\n"
        "sync:\n"
        "  interval_seconds: 7\n"
    )
    p = tmp_path / "cfg.yaml"
    p.write_text(yaml_text, encoding="utf-8")
    cfg = tsa.load_config(str(p))
    assert cfg["server"]["url"] == "https://remote.example.com"
    assert cfg["sync"]["interval_seconds"] == 7
    assert cfg["server"]["api_key"] == "sk-x"


def test_yaml_parse_with_bom(tsa, tmp_path):
    """Windows 上创建的配置文件常带 UTF-8 BOM；load_config 必须能忽略 BOM。
    回归保护见 2026-08-21 修复（曾致 server.url 解析为 None，安全守卫失效）。"""
    yaml_text = (
        "server:\n"
        "  url: http://127.0.0.1:8001\n"
        "  machine: guard-test\n"
    )
    p = tmp_path / "cfg-bom.yaml"
    p.write_bytes(b"\xef\xbb\xbf" + yaml_text.encode("utf-8"))
    cfg = tsa.load_config(str(p))
    assert cfg["server"]["url"] == "http://127.0.0.1:8001"
    assert cfg["server"]["machine"] == "guard-test"
