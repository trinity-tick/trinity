"""compact_structure token 预算模式单元测试（2026-08-21，P0-3）。

覆盖（临时 SQLite 库）：
- 预算内会话不动（不压缩、不标记）
- 超预算：更早 turn 聚合摘要 + 尾部按 turn 边界保留
- 裁剪优先级：tool/result 先于 tool/call，消息永不裁
- 候选会话：budget 模式忽略 min-days；时效模式尊重 min-days
- main() dry-run 不落库；正式运行标记 compacted 并删除明细
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "compact_structure_mod", REPO_ROOT / "scripts" / "compact_structure.py"
)
cs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cs)


@pytest.fixture()
def db(tmp_path):
    d = tmp_path / "store.db"
    con = sqlite3.connect(str(d))
    con.execute(
        "CREATE TABLE dsh_sessions (session_id TEXT PRIMARY KEY, status TEXT, updated_at REAL)"
    )
    con.execute(
        "CREATE TABLE dsh_events (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
        "seq INTEGER, type TEXT, turn INTEGER, step INTEGER, time REAL, payload TEXT)"
    )
    con.execute("CREATE INDEX idx_ev_sess ON dsh_events(session_id, seq)")
    con.commit()
    con.close()
    return str(d)


def _connect(db):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    return con


def _seed(con, session_id, turns, status="closed", updated_at=0.0):
    con.execute("INSERT INTO dsh_sessions VALUES (?,?,?)", (session_id, status, updated_at))
    seq = 1
    for turn, evs in turns.items():
        for etype, payload in evs:
            con.execute(
                "INSERT INTO dsh_events (session_id, seq, type, turn, step, time, payload) "
                "VALUES (?,?,?,?,?,?,?)",
                (session_id, seq, etype, turn, 0, 1000.0 + turn, json.dumps(payload)),
            )
            seq += 1
    con.commit()


def _counts(con, session_id):
    rows = con.execute(
        "SELECT type, COUNT(*) FROM dsh_events WHERE session_id=? GROUP BY type", (session_id,)
    ).fetchall()
    return dict(rows)


def test_within_budget_untouched(db):
    con = _connect(db)
    _seed(con, "s1", {1: [("user/message", {"content": "hi"})]})
    summaries, delete_ids, kept, dropped, kept_tokens, cut_turn = cs.plan_budget_compaction(
        con, "s1", 100000
    )
    assert summaries == [] and delete_ids == []
    assert kept == 1 and dropped == {"tool/result": 0, "tool/call": 0, "other": 0}
    con.close()


def test_over_budget_compacts_early_turns_keeps_tail(db):
    con = _connect(db)
    _seed(con, "s2", {
        1: [("user/message", {"content": "old question"}), ("tool/result", {"o": "x" * 400})],
        2: [("assistant/message", {"content": "answer two"})],
        3: [("user/message", {"content": "recent ask"}), ("assistant/message", {"content": "recent ans"})],
    })
    summaries, delete_ids, kept, dropped, kept_tokens, cut_turn = cs.plan_budget_compaction(
        con, "s2", 200
    )
    assert len(summaries) == 1  # 只有 turn1 被聚合
    assert summaries[0]["turn"] == 1
    assert cut_turn == 2  # 切点对齐到 turn2 边界
    assert kept >= 2  # 尾部保留
    # 尾部（turn>=2）事件未被删
    tail_ids = {r["id"] for r in con.execute(
        "SELECT id FROM dsh_events WHERE session_id='s2' AND turn >= 2")}
    assert not (tail_ids & set(delete_ids))
    # 被删的明细 = 被聚合的 turn1
    turn1_ids = {r["id"] for r in con.execute(
        "SELECT id FROM dsh_events WHERE session_id='s2' AND turn = 1")}
    assert turn1_ids <= set(delete_ids)
    con.close()


def test_trim_priority_tool_result_before_tool_call(db):
    con = _connect(db)
    # 尾部含巨大 tool/result 与 tool/call，预算极小 → 先裁 tool/result 再 tool/call
    _seed(con, "s3", {
        1: [("user/message", {"content": "a" * 500})],
        2: [("user/message", {"content": "b"}), ("tool/call", {"name": "bash", "args": "x" * 300}),
            ("tool/result", {"output": "y" * 400})],
    })
    summaries, delete_ids, kept, dropped, kept_tokens, cut_turn = cs.plan_budget_compaction(
        con, "s3", 60
    )
    assert dropped["tool/result"] == 1
    assert dropped["tool/call"] == 1
    assert kept >= 1
    # 保留尾部（turn2）的用户消息永不裁
    tail_msgs = {r["id"] for r in con.execute(
        "SELECT id FROM dsh_events WHERE session_id='s3' AND turn >= 2 "
        "AND type IN ('user/message','assistant/message')")}
    assert not (tail_msgs & set(delete_ids))
    con.close()


def test_candidate_budget_mode_ignores_min_days(db):
    con = _connect(db)
    _seed(con, "recent", {1: [("user/message", {"content": "hi"})]}, updated_at=9999999999.0)
    # 时效模式：近期更新 → 不候选
    age = cs.candidate_sessions(con, min_days=1, force=False, session_id=None, budget_mode=False)
    assert age == []
    # 预算模式：忽略 min-days → 候选
    budget = cs.candidate_sessions(con, min_days=1, force=False, session_id=None, budget_mode=True)
    assert [c["session_id"] for c in budget] == ["recent"]
    con.close()


def test_main_dry_run_and_real_run(db, monkeypatch):
    con = _connect(db)
    _seed(con, "s4", {
        1: [("user/message", {"content": "q1"}), ("tool/result", {"o": "z" * 300})],
        2: [("assistant/message", {"content": "a2"})],
        3: [("user/message", {"content": "tail keep"})],
    })
    con.close()
    monkeypatch.setattr(sys, "argv", ["compact_structure", "--sqlite-path", db, "--budget-tokens", "80"])
    assert cs.main() == 0
    con = _connect(db)
    st = con.execute("SELECT status FROM dsh_sessions WHERE session_id='s4'").fetchone()
    assert st[0] == "compacted"
    events = con.execute("SELECT type FROM dsh_events WHERE session_id='s4'").fetchall()
    types = [r[0] for r in events]
    assert "compacted_turn" in types
    assert "user/message" in types  # 尾部消息保留
    con.close()
