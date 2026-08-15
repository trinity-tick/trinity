"""Trinity — 记忆可迁移标准单元测试（2026-08-15, V2 动作 A）。

覆盖 scripts/memory_portability.py：
- 导出标准格式（核心字段/标签解析）
- JSON/NDJSON 写入与回读
- 导入幂等（content_hash 去重）
- Mem0 / Zep 格式转换
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from scripts.memory_portability import (
    convert_mem0, convert_zep, export_memories, import_memories,
    load_standard_file, write_export,
)


@pytest.fixture()
def src_db(tmp_path: Path) -> str:
    from trinity.adapters.sqlite import SQLiteAdapter
    db = str(tmp_path / "src.db")
    a = SQLiteAdapter(db)
    a.connect()
    a.store_memory("源记忆一：PostgreSQL", persona_id="p1", agent_id="a1", tags=["db"])
    a.store_memory("源记忆二：Redis", persona_id="p1", agent_id="a1", tags=["cache"])
    a.disconnect()
    return db


@pytest.fixture()
def empty_db(tmp_path: Path) -> str:
    return str(tmp_path / "dst.db")


def test_export_core_fields(src_db: str) -> None:
    items = export_memories(src_db, persona_id="p1")
    assert len(items) == 2
    first = items[0]
    assert first["content"].startswith("源记忆")
    assert first["tags"] == ["db"]  # JSON 字符串已解析为列表
    assert first["persona_id"] == "p1"
    assert first["agent_id"] == "a1"


def test_export_json_roundtrip(src_db: str, tmp_path: Path) -> None:
    items = export_memories(src_db)
    f = str(tmp_path / "exp.json")
    write_export(items, f, "json")
    loaded = load_standard_file(f)
    assert len(loaded) == 2
    assert loaded[0]["content"].startswith("源记忆")


def test_export_ndjson_roundtrip(src_db: str, tmp_path: Path) -> None:
    items = export_memories(src_db)
    f = str(tmp_path / "exp.ndjson")
    write_export(items, f, "ndjson")
    loaded = load_standard_file(f)
    assert len(loaded) == 2


def test_import_idempotent(src_db: str, empty_db: str, tmp_path: Path) -> None:
    items = export_memories(src_db)
    f = str(tmp_path / "m.json")
    write_export(items, f)
    loaded = load_standard_file(f)
    r1 = import_memories(loaded, empty_db)
    assert r1["imported"] == 2
    assert r1["skipped"] == 0
    r2 = import_memories(loaded, empty_db)
    assert r2["imported"] == 0
    assert r2["skipped"] == 2  # 幂等


def test_convert_mem0() -> None:
    items = [{"memory": "mem0 记忆", "user_id": "u1",
              "metadata": {"tags": ["x"], "importance": 0.7, "extra": "keep"}}]
    out = convert_mem0(items, "u1")
    assert out[0]["content"] == "mem0 记忆"
    assert out[0]["tags"] == ["x"]
    assert out[0]["importance"] == 0.7
    assert out[0]["metadata"] == {"extra": "keep"}  # tags/importance 已剥离


def test_convert_zep() -> None:
    items = [{"content": "zep 记忆", "type": "fact", "metadata": {"tags": ["y"]}}]
    out = convert_zep(items, "u1")
    assert out[0]["content"] == "zep 记忆"
    assert out[0]["category"] == "fact"
    assert out[0]["tags"] == ["y"]


def test_convert_skips_empty() -> None:
    assert convert_mem0([{"memory": ""}], "u1") == []
    assert convert_zep([{"content": ""}], "u1") == []
