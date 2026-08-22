# -*- coding: utf-8 -*-
"""白盒 Persona 画像层单元测试（2026-08-22）。全部使用临时 SQLite 库，绝不动运行时大库。"""
import os
import sys
import re

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from trinity.memory.persona import (  # noqa: E402
    PersonaEngine,
    persona_enabled,
    maybe_persona_after_store,
    PERSONA_PROPOSITION_TYPES,
)


@pytest.fixture
def adapter(tmp_path):
    from trinity.adapters.sqlite import SQLiteAdapter

    a = SQLiteAdapter(db_path=str(tmp_path / "t.db"))
    a.connect()
    yield a
    a.disconnect()


@pytest.fixture
def personas_dir(tmp_path):
    return str(tmp_path / "personas")


def _store_proposition(
    adapter,
    text,
    persona_id="alice",
    ptype="user_preference",
    source="src1",
    temporal=None,
):
    """按 proposition_extractor.extract_and_store 的落盘格式写入一条命题记忆。"""
    return adapter.store_memory(
        content=f"[命题:{ptype}] {text}",
        persona_id=persona_id,
        role="user",
        importance=0.75,
        tags=["proposition", ptype],
        category="proposition",
        metadata={
            "proposition_type": ptype,
            "temporal": temporal,
            "source_memory_id": source,
            "extractor": "proposition_v2",
        },
        agent_id="t-test",
    )


def _make_engine(adapter, personas_dir, **kw):
    return PersonaEngine(adapter=adapter, personas_dir=personas_dir, **kw)


def test_enabled_default_off(monkeypatch):
    monkeypatch.delenv("TRINITY_PERSONA", raising=False)
    assert persona_enabled() is False
    for on in ("on", "1", "true", "yes"):
        monkeypatch.setenv("TRINITY_PERSONA", on)
        assert persona_enabled() is True
    monkeypatch.setenv("TRINITY_PERSONA", "off")
    assert persona_enabled() is False


def test_types_defined():
    assert "user_preference" in PERSONA_PROPOSITION_TYPES
    assert "user_fact" in PERSONA_PROPOSITION_TYPES


def test_aggregation_scoped_by_persona(adapter, personas_dir):
    _store_proposition(adapter, "用户喜欢深色模式", persona_id="alice")
    _store_proposition(adapter, "用户喜欢清晨写作", persona_id="alice")
    _store_proposition(adapter, "用户负责供应链", persona_id="bob")
    engine = _make_engine(adapter, personas_dir)
    alice = engine.collect_entries("alice")
    bob = engine.collect_entries("bob")
    # 多 persona 隔离：alice 只见自身偏好（顺序不要求）
    assert {e["proposition"] for e in alice} == {"用户喜欢深色模式", "用户喜欢清晨写作"}
    assert [e["proposition"] for e in bob] == ["用户负责供应链"]


def test_markdown_contains_memory_id_and_text(adapter, personas_dir):
    # 来源 memory_id 即命题记忆自身的 memory_id（聚合入画像的白盒来源）
    r = _store_proposition(adapter, "用户偏好用命令行走流程", source="mem_pref_1")
    src_mid = r["memory_id"]
    engine = _make_engine(adapter, personas_dir)
    count = engine.rebuild("alice")
    assert count == 1
    text, _rcount = engine.read_persona("alice")
    assert "用户偏好用命令行走流程" in text
    assert src_mid in text  # 来源 memory_id 出现在每条条目中
    assert f"memory_id: `{src_mid}`" in text
    assert "importance" in text
    assert "persona_id: alice" in text


def test_incremental_dedup_by_text(adapter, personas_dir):
    engine = _make_engine(adapter, personas_dir)
    _store_proposition(adapter, "用户喜欢深色模式", source="s1")
    engine.rebuild("alice")
    # 同原文不同来源 → 增量合并不重复追加
    n2 = engine.merge_persona("alice", {
        "proposition": "用户喜欢深色模式",
        "memory_id": "mem_dup",
        "time": "2026-08-22",
        "importance": 0.8,
    })
    # 再次全量重建，应仍只有 1 条（库中只有一条）
    assert engine.rebuild("alice") == 1
    assert "用户喜欢深色模式" in engine.read_persona("alice")[0]


def test_rebuild_idempotent(adapter, personas_dir):
    engine = _make_engine(adapter, personas_dir)
    for i in range(3):
        _store_proposition(adapter, f"用户偏好条目{i}", source=f"s{i}")
    assert engine.rebuild("alice") == 3
    assert engine.rebuild("alice") == 3  # 幂等：再次重建结果一致
    text, count = engine.read_persona("alice")
    assert count == 3


def test_no_preference_empty(adapter, personas_dir):
    # 只有 user_done 命题或空 → 不产生条目
    _store_proposition(adapter, "用户完成了对标", ptype="user_done", source="s1")
    engine = _make_engine(adapter, personas_dir)
    assert engine.collect_entries("alice") == []
    assert engine.rebuild("alice") == 0
    text, count = engine.read_persona("alice")
    assert count == 0
    assert "entry_count: 0" in text


def test_user_fact_excluded_by_default_included_when_requested(adapter, personas_dir):
    _store_proposition(adapter, "用户是项目经理", ptype="user_fact", source="f1")
    default_engine = _make_engine(adapter, personas_dir)  # 默认不含 user_fact
    assert default_engine.collect_entries("alice") == []
    inc_engine = _make_engine(adapter, personas_dir, include_user_fact=True)
    entries = inc_engine.collect_entries("alice")
    assert [e["proposition"] for e in entries] == ["用户是项目经理"]


def test_maybe_after_store_off_writes_nothing(adapter, personas_dir, monkeypatch):
    monkeypatch.delenv("TRINITY_PERSONA", raising=False)
    r = _store_proposition(adapter, "用户喜欢离线调参", source="s_off")
    n = maybe_persona_after_store(
        adapter, {"content": "[命题:user_preference] 用户喜欢离线调参",
                  "persona_id": "alice", "metadata": {"proposition_type": "user_preference"}},
        r, personas_dir=personas_dir,
    )
    assert n == 0
    assert os.path.isdir(personas_dir) is False or os.listdir(personas_dir) == []


def test_maybe_after_store_on_builds_persona(adapter, personas_dir, monkeypatch):
    monkeypatch.setenv("TRINITY_PERSONA", "on")
    r = _store_proposition(adapter, "用户偏好自动保存", source="s_on")
    n = maybe_persona_after_store(
        adapter, {"content": "[命题:user_preference] 用户偏好自动保存",
                  "persona_id": "alice", "importance": 0.7,
                  "metadata": {"proposition_type": "user_preference", "temporal": "2026-08-22"}},
        r, personas_dir=personas_dir,
    )
    assert n >= 1
    assert os.path.exists(os.path.join(personas_dir, "alice.md"))
    assert "用户偏好自动保存" in open(os.path.join(personas_dir, "alice.md"),
                                    encoding="utf-8").read()


def test_list_personas(adapter, personas_dir):
    engine = _make_engine(adapter, personas_dir)
    assert engine.list_personas() == []
    _store_proposition(adapter, "用户喜欢 A", persona_id="alice", source="sa")
    _store_proposition(adapter, "用户喜欢 B", persona_id="bob", source="sb")
    engine.rebuild("alice")
    engine.rebuild("bob")
    assert engine.list_personas() == ["alice", "bob"]


def test_read_missing_persona(adapter, personas_dir):
    engine = _make_engine(adapter, personas_dir)
    text, count = engine.read_persona("ghost")
    assert text == "" and count == 0
