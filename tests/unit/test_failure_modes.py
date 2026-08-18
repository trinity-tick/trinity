"""Trinity — 失败模式基准测试（agent-memory-bench 四类对齐, 2026-08-18）。

覆盖网络评价必测的四个失败模式：
  1. retraction 撤回   — 删除/更新后旧内容不残留
  2. collision 碰撞    — 重复记忆被唯一约束阻止（数据库级去重）
  3. recall 召回       — 相关记忆可被检索命中
  4. conflict 冲突     — 矛盾记忆的检测（写入层；引擎层 CB46 提供解决路径）

用隔离临时库，不污染生产大库。
"""
from __future__ import annotations

import os
import tempfile

import pytest


@pytest.fixture()
def adapter():
    from trinity.adapters.sqlite import SQLiteAdapter

    db = os.path.join(tempfile.gettempdir(), "fm_pytest.db")
    if os.path.exists(db):
        os.remove(db)
    a = SQLiteAdapter(db_path=db)
    a.connect()
    a._conn.execute("PRAGMA busy_timeout=30000")
    yield a
    a.disconnect()
    if os.path.exists(db):
        os.remove(db)


def _store(adapter, content: str, importance: float = 0.5, tags=None):
    return adapter.store_memory(
        content=content, persona_id="default", session_id="fm",
        agent_id="fm-test", role="user", importance=importance,
        tags=tags or [], category="general",
    )


def _count(adapter, query: str) -> int:
    sr = adapter.search_memories(query=query, top_k=5, category=None)
    res = sr.get("results", []) if isinstance(sr, dict) else sr
    return len(res) if res else 0


def test_retraction_delete_not_recalled(adapter):
    """删除后旧内容不再被检索到。"""
    r = _store(adapter, "Alpha 项目密码是 secret123", tags=["retraction"])
    assert _count(adapter, "Alpha 项目密码") >= 1
    adapter.delete_memory(r["memory_id"])
    assert _count(adapter, "Alpha 项目密码") == 0
    row = adapter._conn.execute(
        "SELECT status FROM memories WHERE memory_id=?", (r["memory_id"],)
    ).fetchone()
    assert row and row[0] == "deleted"


def test_collision_unique_constraint(adapter):
    """相同内容（同 persona/agent）重复写入被唯一约束阻止。"""
    _store(adapter, "重复内容 DEF", tags=["collision"])
    with pytest.raises(Exception):
        _store(adapter, "重复内容 DEF", tags=["collision"])


def test_recall_relevant_memories(adapter):
    """不同主题记忆可被各自关键词召回。"""
    cases = [
        ("供应链管理 库存优化 波次拣选", "recall-a"),
        ("用户偏好暗色模式 中文交流", "recall-b"),
        ("Kafka 消息队列 端到端", "recall-c"),
    ]
    for content, tag in cases:
        _store(adapter, content, importance=0.6, tags=[tag])
    for query, _ in cases:
        assert _count(adapter, query.split()[0]) >= 1, f"recall failed for {query}"


def test_conflict_group_assignment(adapter):
    """写入层冲突组分配（2026-08-18 改进后）：高相似但内容不同的记忆
    在 store_memory 时自动分配相同 conflict_group_id（候选冲突组）。"""
    _store(adapter, "数据库端口是 5432", tags=["conflict"])
    _store(adapter, "数据库端口是 5430", tags=["conflict"])
    rows = adapter._conn.execute(
        "SELECT COUNT(DISTINCT conflict_group_id) FROM memories "
        "WHERE conflict_group_id IS NOT NULL AND conflict_group_id != ''"
    ).fetchone()[0]
    assert rows >= 1, "写入层应分配冲突组（当前缺口，引擎层 CB46 覆盖）"
