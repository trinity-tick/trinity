"""Trinity — DSH goal 回填单元测试（2026-08-15）。

覆盖 scripts/sync_dsh_goals.py：
- extract_goals_from_projcache：解析 projcache 的 goal 快照（含 val 包装）
- backfill_goals：幂等回填（已有 objective 跳过）
- phase→status 映射
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from scripts.sync_dsh_goals import backfill_goals, extract_goals_from_projcache


@pytest.fixture()
def projcache(tmp_path: Path) -> Path:
    p = tmp_path / "session_projcache.json"
    data = {
        "tables": {
            "sessions": {
                "s1": {"rows": {"goal": {"ver": 4, "seq": 1, "val": {
                    "goal": {"id": "goal-a1", "objective": "目标A", "phase": "active",
                             "maxGoalRounds": 5},
                    "roundsStarted": 0, "createdAt": 1, "updatedAt": 2}}}},
                "s2": {"rows": {"goal": {"ver": 4, "seq": 2, "val": {
                    "goal": {"id": "goal-b2", "objective": "目标B", "phase": "complete",
                             "maxGoalRounds": None}}}}},
                "s3": {"rows": {"goal": {"ver": 4, "seq": 3, "val": None}}},  # 无值
                "s4": {"rows": {}},  # 无 goal 键
            }
        }
    }
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


@pytest.fixture()
def db(tmp_path: Path) -> str:
    d = tmp_path / "t.db"
    c = sqlite3.connect(d)
    c.executescript("""
        CREATE TABLE dsh_goals (
            goal_id TEXT PRIMARY KEY, objective TEXT NOT NULL, status TEXT,
            phase TEXT, round INTEGER, max_rounds INTEGER,
            created_at REAL, updated_at REAL
        );
    """)
    c.close()
    return str(d)


def test_extract_goals(projcache: Path) -> None:
    goals = extract_goals_from_projcache(projcache)
    assert len(goals) == 2  # 排除 val:null 和无 goal 键
    ids = {g["goal_id"] for g in goals}
    assert ids == {"goal-a1", "goal-b2"}
    a1 = next(g for g in goals if g["goal_id"] == "goal-a1")
    assert a1["objective"] == "目标A"
    assert a1["max_goal_rounds"] == 5


def test_backfill_idempotent(db: str, projcache: Path) -> None:
    goals = extract_goals_from_projcache(projcache)
    r1 = backfill_goals(goals, db)
    assert r1["backfilled"] == 2
    assert r1["total_goals"] == 2
    assert r1["objective_rate"] == "100%"
    # 再跑一次 → 全部跳过
    r2 = backfill_goals(goals, db)
    assert r2["backfilled"] == 0
    assert r2["skipped_existing"] == 2


def test_phase_to_status(db: str, projcache: Path) -> None:
    goals = extract_goals_from_projcache(projcache)
    backfill_goals(goals, db)
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    b2 = c.execute("SELECT status, phase FROM dsh_goals WHERE goal_id='goal-b2'").fetchone()
    assert b2["status"] == "completed"  # phase=complete → completed
    assert b2["phase"] == "complete"
    c.close()


def test_empty_projcache(tmp_path: Path) -> None:
    p = tmp_path / "empty.json"
    p.write_text(json.dumps({"tables": {"sessions": {}}}), encoding="utf-8")
    assert extract_goals_from_projcache(p) == []
