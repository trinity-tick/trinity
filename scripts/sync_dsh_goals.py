#!/usr/bin/env python3
"""
Trinity — DSH goal 回填器（2026-08-15）
========================================
从 DSH 的 session_projcache.json（会话投影缓存，含 goal 快照）提取
goal（goal_id/objective/phase/maxGoalRounds），回填到 Trinity dsh_goals。

背景：DSH 会话 jsonl（.jsonl.zstd）只存会话头（web 部署事件不落盘），
goal 数据唯一可恢复来源是 projcache（48 槽位，当前 7 个有值）。
本脚本幂等：已带 objective 的 goal 跳过。

用法：
    python scripts/sync_dsh_goals.py
    python scripts/sync_dsh_goals.py --projcache <path> --db <path>   # 自定义路径
    python scripts/sync_dsh_goals.py --dry-run                        # 预览
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

_TRINITY_ROOT = Path(__file__).resolve().parent.parent
if str(_TRINITY_ROOT) not in sys.path:
    sys.path.insert(0, str(_TRINITY_ROOT))

_PHASE_TO_STATUS = {
    "active": "active", "complete": "completed",
    "paused": "paused", "blocked": "blocked",
}


def extract_goals_from_projcache(projcache: Path) -> list[dict]:
    """从 session_projcache.json 提取所有非空 goal 快照。"""
    data = json.loads(projcache.read_text(encoding="utf-8"))
    sessions = data.get("tables", {}).get("sessions", {})
    goals = []
    for sid, entry in sessions.items():
        g = (entry or {}).get("rows", {}).get("goal") if isinstance(entry, dict) else None
        if not g or not g.get("val"):
            continue
        val = g["val"]
        goal = val.get("goal") if isinstance(val, dict) and "goal" in val else val
        if not isinstance(goal, dict) or not goal.get("id"):
            continue
        goals.append({
            "goal_id": goal["id"],
            "objective": goal.get("objective", ""),
            "phase": goal.get("phase", "active"),
            "max_goal_rounds": goal.get("maxGoalRounds"),
            "session_id": sid,
        })
    return goals


def backfill_goals(goals: list[dict], db_path: str, dry_run: bool = False) -> dict:
    """幂等回填 goal 到 dsh_goals。返回统计。"""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    now = time.time()
    backfilled = skipped = 0
    for g in goals:
        gid = g["goal_id"]
        obj = g.get("objective") or ""
        status = _PHASE_TO_STATUS.get(g.get("phase", "active"), g.get("phase"))
        existing = conn.execute(
            "SELECT objective FROM dsh_goals WHERE goal_id=?", (gid,)
        ).fetchone()
        if existing and existing["objective"]:
            skipped += 1
            continue
        if dry_run:
            print(f"  [dry] {gid[:20]} phase={g['phase']} obj={obj[:40]!r}")
            backfilled += 1
            continue
        conn.execute(
            """INSERT INTO dsh_goals(goal_id, objective, status, phase, round,
                                     max_rounds, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(goal_id) DO UPDATE SET
                 objective=COALESCE(NULLIF(excluded.objective,''), dsh_goals.objective),
                 status=excluded.status, phase=excluded.phase,
                 max_rounds=COALESCE(excluded.max_rounds, dsh_goals.max_rounds),
                 updated_at=excluded.updated_at""",
            (gid, obj, status, g.get("phase"), 0, g.get("max_goal_rounds"), now, now),
        )
        backfilled += 1
    if not dry_run:
        conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM dsh_goals").fetchone()[0]
    withobj = conn.execute(
        "SELECT COUNT(*) FROM dsh_goals WHERE objective != ''"
    ).fetchone()[0]
    conn.close()
    return {
        "backfilled": backfilled,
        "skipped_existing": skipped,
        "total_goals": total,
        "with_objective": withobj,
        "objective_rate": f"{withobj * 100 // total}%" if total else "n/a",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="DSH goal backfill")
    parser.add_argument("--projcache", default=str(
        Path.home() / ".dsh" / "storages" / "session_projcache.json"))
    parser.add_argument("--db", default=os.path.expanduser(
        "~/.trinity/store/trinity_store.db"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    proj = Path(args.projcache)
    if not proj.exists():
        print(f"projcache not found: {proj}")
        return 1
    goals = extract_goals_from_projcache(proj)
    print(f"== DSH goal 回填（projcache: {len(goals)} goals）==")
    stats = backfill_goals(goals, args.db, dry_run=args.dry_run)
    print(f"   回填: {stats['backfilled']} / 跳过(已有): {stats['skipped_existing']}")
    print(f"   dsh_goals: {stats['total_goals']} total, "
          f"{stats['with_objective']} with objective ({stats['objective_rate']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
