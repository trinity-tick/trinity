"""Trinity — DSH 结构层 goal 同步单元测试（2026-08-15）。

覆盖 structure_store._sync_goal_schedule_from_event：
- create_goal（DSH 内置工具，无 goal_id）→ 用 objective 哈希生成 id 并写入
- update_goal（带 goal_id）→ 状态更新
- schedule_create → 写入 dsh_schedules
- 非 goal/schedule 事件不产生副作用
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from trinity import structure_store


@pytest.fixture()
def conn(tmp_path: Path):
    db = tmp_path / "t.db"
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    c.executescript(structure_store._STRUCTURE_DDL)
    yield c
    c.close()


def _tool_call(name: str, args: dict, seq: int = 1) -> dict:
    return {"seq": seq, "type": "tool/call", "time": 1000.0,
            "data": {"name": name, "arguments": json.dumps(args)}}


def test_create_goal_without_id_writes_objective(conn) -> None:
    """DSH create_goal 无 goal_id → 哈希 id + objective 入库。"""
    ev = _tool_call("create_goal", {"objective": "优化检索质量", "max_goal_rounds": 5})
    structure_store._sync_goal_schedule_from_event(conn, ev, 1000.0)
    rows = conn.execute("SELECT * FROM dsh_goals").fetchall()
    assert len(rows) == 1
    assert rows[0]["objective"] == "优化检索质量"
    assert rows[0]["status"] == "active"
    assert rows[0]["goal_id"].startswith("goal-")


def test_create_goal_id_stable(conn) -> None:
    """同一 objective → 同一哈希 id（幂等）。"""
    ev = _tool_call("create_goal", {"objective": "相同目标"})
    structure_store._sync_goal_schedule_from_event(conn, ev, 1000.0)
    ev2 = _tool_call("create_goal", {"objective": "相同目标"})
    structure_store._sync_goal_schedule_from_event(conn, ev2, 1001.0)
    rows = conn.execute("SELECT goal_id FROM dsh_goals").fetchall()
    assert len(rows) == 1  # 幂等合并


def test_update_goal_status(conn) -> None:
    ev = _tool_call("update_goal", {"action": "complete", "goal_id": "g-1"})
    structure_store._sync_goal_schedule_from_event(conn, ev, 1000.0)
    row = conn.execute("SELECT * FROM dsh_goals WHERE goal_id='g-1'").fetchone()
    assert row["status"] == "completed"


def test_update_goal_edit_with_objective(conn) -> None:
    """update_goal(action=edit) 携带真实 UUID + objective → 完整写入（DSH 实际格式）。"""
    ev = _tool_call("update_goal", {
        "action": "edit", "goal_id": "goal-45b85d3c-8b43-4e0c-b2e8-ca885ef9a94d",
        "revision": 1, "objective": "全方位执行 Trinity 优化方向",
    })
    structure_store._sync_goal_schedule_from_event(conn, ev, 1000.0)
    row = conn.execute("SELECT * FROM dsh_goals WHERE goal_id='goal-45b85d3c-8b43-4e0c-b2e8-ca885ef9a94d'").fetchone()
    assert row is not None
    assert row["objective"] == "全方位执行 Trinity 优化方向"
    assert row["status"] == "active"  # edit 保留现有状态


def test_goal_edit_then_complete_merges(conn) -> None:
    """同一 UUID 的 edit→complete 合并到一行（状态更新，objective 保留）。"""
    ev1 = _tool_call("update_goal", {"action": "edit", "goal_id": "g-9",
                                     "objective": "目标文本"})
    structure_store._sync_goal_schedule_from_event(conn, ev1, 1000.0)
    ev2 = _tool_call("update_goal", {"action": "complete", "goal_id": "g-9"})
    structure_store._sync_goal_schedule_from_event(conn, ev2, 2000.0)
    rows = conn.execute("SELECT * FROM dsh_goals WHERE goal_id='g-9'").fetchall()
    assert len(rows) == 1  # 合并为一行
    assert rows[0]["status"] == "completed"
    assert rows[0]["objective"] == "目标文本"  # objective 不被 complete 覆盖


def test_schedule_create(conn) -> None:
    ev = _tool_call("schedule_create", {"schedule_id": "s-1", "prompt": "reminder",
                                        "after_seconds": 3600})
    structure_store._sync_goal_schedule_from_event(conn, ev, 1000.0)
    row = conn.execute("SELECT * FROM dsh_schedules WHERE schedule_id='s-1'").fetchone()
    assert row is not None
    assert row["prompt"] == "reminder"
    assert row["target"] == "3600s"


def test_unrelated_event_no_side_effect(conn) -> None:
    ev = _tool_call("trinity_search", {"query": "x"})
    structure_store._sync_goal_schedule_from_event(conn, ev, 1000.0)
    assert conn.execute("SELECT COUNT(*) FROM dsh_goals").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM dsh_schedules").fetchone()[0] == 0


def test_non_tool_event_ignored(conn) -> None:
    ev = {"seq": 1, "type": "user/message", "time": 1.0, "data": {"text": "hi"}}
    structure_store._sync_goal_schedule_from_event(conn, ev, 1.0)
    assert conn.execute("SELECT COUNT(*) FROM dsh_goals").fetchone()[0] == 0
