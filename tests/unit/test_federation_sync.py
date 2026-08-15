"""Trinity — 联邦增量同步单元测试（2026-08-15, V2 动作 C ①）。

覆盖 scripts/federation_sync.py：
- 导出快照（增量 since 过滤）
- diff 冲突检测（同 hash 异内容）
- merge 三种策略（newer / keep-both / skip）
- 导入幂等（content_hash 去重）
"""

from __future__ import annotations

import json
import os
import time
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.federation_sync import (
    diff_snapshots, export_snapshot, import_snapshot, merge_snapshots,
)
from trinity.adapters.sqlite import SQLiteAdapter


@pytest.fixture()
def db_a(tmp_path: Path) -> str:
    d = str(tmp_path / "a.db")
    a = SQLiteAdapter(d)
    a.connect()
    a.store_memory("A1：PostgreSQL", persona_id="p1", agent_id="fed-a")
    a.store_memory("A2：Redis", persona_id="p1", agent_id="fed-a")
    a.disconnect()
    return d


@pytest.fixture()
def db_b(tmp_path: Path) -> str:
    d = str(tmp_path / "b.db")
    b = SQLiteAdapter(d)
    b.connect()
    b.store_memory("B1：Kafka", persona_id="p1", agent_id="fed-b")
    b.disconnect()
    return d


def test_export_and_import(db_a: str, db_b: str, tmp_path: Path) -> None:
    fa = str(tmp_path / "a.json")
    export_snapshot(db_a, fa)
    ri = import_snapshot(db_b, fa)
    assert ri["imported"] == 2
    assert ri["skipped"] == 0


def test_import_idempotent(db_a: str, tmp_path: Path) -> None:
    fa = str(tmp_path / "a.json")
    export_snapshot(db_a, fa)
    import_snapshot(db_a, fa)  # 导到自身
    ri = import_snapshot(db_a, fa)
    assert ri["imported"] == 0
    assert ri["skipped"] == 2


def test_incremental_export_only_new(db_a: str, tmp_path: Path) -> None:
    cutoff = datetime.now(timezone.utc).isoformat()
    time.sleep(1.1)
    a = SQLiteAdapter(db_a)
    a.connect()
    a.store_memory("新增：Docker", persona_id="p1", agent_id="fed-a")
    a.disconnect()
    f = str(tmp_path / "inc.json")
    res = export_snapshot(db_a, f, since=cutoff)
    assert res["count"] == 1  # 只含新增


def test_diff_conflicts(tmp_path: Path) -> None:
    """同 memory_id 不同 content_hash → 冲突。"""
    fa = tmp_path / "a.json"
    fb = tmp_path / "b.json"
    mk = lambda p, content, h: p.write_text(json.dumps({"memories": [
        {"memory_id": "m1", "content": content, "content_hash": h}]
    }), encoding="utf-8")
    mk(fa, "orig content", "hash-a")
    mk(fb, "modified content", "hash-b")
    d = diff_snapshots(str(fa), str(fb))
    assert len(d["conflicts"]) == 1


def test_merge_newer_strategy(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    mk = lambda p, content, h, ts: p.write_text(json.dumps({"memories": [
        {"memory_id": "m1", "content": content, "content_hash": h,
         "updated_at": ts}]
    }), encoding="utf-8")
    mk(base, "OLD_CONTENT", "h-old", "2026-08-14T00:00:00")
    mk(other, "NEW_CONTENT", "h-new", "2026-08-15T00:00:00")
    out = tmp_path / "m.json"
    res = merge_snapshots(str(base), str(other), str(out), "newer")
    merged = json.loads(out.read_text(encoding="utf-8"))
    assert merged["memories"][0]["content"] == "NEW_CONTENT"  # 较新胜出


def test_merge_keep_both(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    base.write_text(json.dumps({"memories": [
        {"memory_id": "m1", "content": "A", "content_hash": "h1"}]}), encoding="utf-8")
    other.write_text(json.dumps({"memories": [
        {"memory_id": "m1", "content": "B", "content_hash": "h2"}]}), encoding="utf-8")
    out = tmp_path / "m.json"
    res = merge_snapshots(str(base), str(other), str(out), "keep-both")
    merged = json.loads(out.read_text(encoding="utf-8"))
    assert len(merged["memories"]) == 2  # 双方保留


def test_merge_skip_conflict(tmp_path: Path) -> None:
    base = tmp_path / "base.json"
    other = tmp_path / "other.json"
    base.write_text(json.dumps({"memories": [
        {"memory_id": "m1", "content": "A", "content_hash": "h1"}]}), encoding="utf-8")
    other.write_text(json.dumps({"memories": [
        {"memory_id": "m1", "content": "B", "content_hash": "h2"}]}), encoding="utf-8")
    out = tmp_path / "m.json"
    res = merge_snapshots(str(base), str(other), str(out), "skip")
    merged = json.loads(out.read_text(encoding="utf-8"))
    assert merged["memories"][0]["content"] == "A"  # 保留 base
    assert res["conflicts_skipped"] == 1
