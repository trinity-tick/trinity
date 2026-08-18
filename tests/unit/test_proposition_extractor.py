# -*- coding: utf-8 -*-
"""命题化 v2 提取器单元测试（M2 原型，2026-08-18）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from trinity.memory.proposition_extractor import (
    extract_propositions_mock,
    extract_enabled,
    extract_propositions,
    maybe_extract_after_store,
    PROPOSITION_TYPES,
    IMPORTANCE_BY_TYPE,
)


@pytest.fixture
def adapter(tmp_path):
    from trinity.adapters.sqlite import SQLiteAdapter
    a = SQLiteAdapter(db_path=str(tmp_path / "t.db"))
    a.connect()
    yield a
    a.disconnect()


def test_types_defined():
    assert set(PROPOSITION_TYPES) == {"user_preference", "user_fact", "user_done", "agent_done"}
    assert IMPORTANCE_BY_TYPE["user_preference"] >= 0.7


def test_enabled_default_off(monkeypatch):
    monkeypatch.delenv("TRINITY_PROPOSITION_EXTRACT", raising=False)
    assert extract_enabled() is False
    monkeypatch.setenv("TRINITY_PROPOSITION_EXTRACT", "on")
    assert extract_enabled() is True


def test_mock_extracts_four_categories():
    p = extract_propositions_mock("我是项目经理，我喜欢深色模式，我完成了对标分析", "user")
    types = {x["type"] for x in p}
    assert "user_fact" in types
    assert "user_preference" in types
    assert "user_done" in types
    p2 = extract_propositions_mock("我已为你生成需求文档", "assistant")
    assert any(x["type"] == "agent_done" for x in p2)


def test_mock_empty_content():
    assert extract_propositions_mock("", "user") == []
    assert extract_propositions_mock("   ", "user") == []


def test_extract_dispatch_without_key(monkeypatch):
    monkeypatch.delenv("TRINITY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    p = extract_propositions("我是项目经理，喜欢深色模式", "user")
    assert len(p) >= 1


def test_maybe_extract_off_writes_nothing(adapter, monkeypatch):
    monkeypatch.delenv("TRINITY_PROPOSITION_EXTRACT", raising=False)
    r = adapter.store_memory(content="我是项目经理", agent_id="t1")
    n = maybe_extract_after_store(adapter, {"content": "我是项目经理", "agent_id": "t1", "role": "user"}, r)
    assert n == 0
    cur = adapter._conn.cursor()
    assert cur.execute("SELECT COUNT(*) FROM memories WHERE category=?", ("proposition",)).fetchone()[0] == 0


def test_maybe_extract_on_writes_propositions(adapter, monkeypatch):
    monkeypatch.setenv("TRINITY_PROPOSITION_EXTRACT", "on")
    r = adapter.store_memory(content="我是项目经理，我喜欢深色模式，我完成了对标", agent_id="t1", role="user")
    n = maybe_extract_after_store(adapter, {"content": "我是项目经理，我喜欢深色模式，我完成了对标", "agent_id": "t1", "role": "user"}, r)
    assert n >= 1
    cur = adapter._conn.cursor()
    cnt = cur.execute("SELECT COUNT(*) FROM memories WHERE category=?", ("proposition",)).fetchone()[0]
    assert cnt == n
    # verbatim 不受影响
    verb = cur.execute("SELECT COUNT(*) FROM memories WHERE category=?", ("conversation",)).fetchone()[0]
    assert verb == 0  # store_memory 默认 category=general
    # 命题带 source 引用
    row = cur.execute("SELECT metadata FROM memories WHERE category=? LIMIT 1", ("proposition",)).fetchone()
    assert row is not None and "source_memory_id" in str(row[0])


def test_parse_llm_markdown_output():
    from trinity.memory.proposition_extractor import _parse_propositions
    raw = "```json\n[{\"type\": \"user_fact\", \"proposition\": \"用户是经理\"} ]\n```"
    props = _parse_propositions(raw)
    assert len(props) == 1
    assert props[0]["type"] == "user_fact"


def test_parse_invalid():
    from trinity.memory.proposition_extractor import _parse_propositions
    assert _parse_propositions("") == []
    assert _parse_propositions("not json") == []
    assert _parse_propositions("[{\"type\": \"bogus\", \"proposition\": \"x\"}]") == []
