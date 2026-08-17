#!/usr/bin/env python3
"""
Trinity — 结构层 Compaction（2026-08-15）
==========================================
控制 dsh_events 表增长：把**已结束/过期会话**的逐事件明细按 turn 聚合为
"compacted_turn" 摘要事件（保留轨迹统计 + 助手段落摘要），删除明细行，
并标记会话 status='compacted'。

幂等：已 compacted 的会话自动跳过；--dry-run 只预览。

用法：
    python scripts/compact_structure.py --dry-run
    python scripts/compact_structure.py                 # 处理全部非 active 会话
    python scripts/compact_structure.py --min-days 1    # 只处理 1 天前未更新的会话
    python scripts/compact_structure.py --session <id>  # 指定会话
    python scripts/compact_structure.py --force         # 包含 active 会话（测试用）
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("compact_structure")

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")


def _now() -> float:
    return time.time()


def candidate_sessions(conn, min_days: float, force: bool, session_id: Optional[str]) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT session_id, status, updated_at FROM dsh_sessions").fetchall()
    out = []
    cutoff = _now() - min_days * 86400.0
    for r in rows:
        sid, status, updated = r["session_id"], r["status"], r["updated_at"] or 0
        if status == "compacted":
            continue
        if session_id and sid != session_id:
            continue
        if status == "active" and not force:
            continue
        if status != "active" and updated and updated > cutoff:
            continue  # 近期仍有更新的非 active 会话暂不压缩
        out.append({"session_id": sid, "status": status, "updated_at": updated})
    return out


def aggregate_turns(conn, session_id: str) -> List[Dict[str, Any]]:
    """按 turn 聚合事件明细，生成摘要记录（含删除清单）。"""
    rows = conn.execute(
        "SELECT id, type, turn, time, payload FROM dsh_events "
        "WHERE session_id=? AND turn IS NOT NULL AND turn > 0 ORDER BY turn, seq",
        (session_id,),
    ).fetchall()
    turns: Dict[int, List[Dict[str, Any]]] = {}
    for r in rows:
        turns.setdefault(r["turn"], []).append(r)
    summaries: List[Dict[str, Any]] = []
    delete_ids: List[int] = []
    for turn in sorted(turns):
        evs = turns[turn]
        tool_calls = sum(1 for e in evs if e["type"] == "tool/call")
        messages = sum(1 for e in evs if e["type"] in ("user/message", "assistant/message"))
        times = [e["time"] or 0 for e in evs if e["time"]]
        # 助手段落摘要（最后一条 assistant 内容截断）
        summary = ""
        for e in reversed(evs):
            if e["type"] == "assistant/message":
                try:
                    p = json.loads(e["payload"]) if e["payload"] else {}
                    summary = (p.get("content") or "")[:200]
                except Exception:
                    summary = ""
                break
        summaries.append({
            "session_id": session_id,
            "type": "compacted_turn",
            "turn": turn,
            "time": (max(times) if times else _now()),
            "payload": {
                "event_count": len(evs),
                "tool_calls": tool_calls,
                "messages": messages,
                "first_time": (min(times) if times else None),
                "last_time": (max(times) if times else None),
                "assistant_summary": summary,
                "compacted_at": datetime.now(timezone.utc).isoformat(),
            },
        })
        delete_ids.extend(e["id"] for e in evs)
    return summaries, delete_ids


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity structure-layer compaction")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-days", type=float, default=1.0,
                        help="只处理 updated_at 早于 N 天的非 active 会话")
    parser.add_argument("--force", action="store_true", help="包含 active 会话（测试）")
    parser.add_argument("--session", default="", help="指定会话 id")
    args = parser.parse_args()

    import sqlite3
    conn = sqlite3.connect(args.sqlite_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        cands = candidate_sessions(conn, args.min_days, args.force, args.session or None)
        logger.info("candidate sessions: %d", len(cands))
        total_deleted = 0
        total_summaries = 0
        for c in cands:
            sid = c["session_id"]
            summaries, delete_ids = aggregate_turns(conn, sid)
            logger.info(
                "session %s: %d turns -> %d summaries, %d detail events",
                sid[:20], len(summaries), len(summaries), len(delete_ids),
            )
            if args.dry_run:
                total_summaries += len(summaries)
                total_deleted += len(delete_ids)
                continue
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq),0) FROM dsh_events WHERE session_id=?", (sid,)
            ).fetchone()[0]
            for i, s in enumerate(summaries, 1):
                conn.execute(
                    "INSERT INTO dsh_events (session_id, seq, type, turn, step, time, payload) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (sid, max_seq + i, s["type"], s["turn"], 0, s["time"],
                     json.dumps(s["payload"], ensure_ascii=False)),
                )
            if delete_ids:
                ph = ",".join("?" for _ in delete_ids)
                conn.execute(f"DELETE FROM dsh_events WHERE id IN ({ph})", delete_ids)
            conn.execute("UPDATE dsh_sessions SET status='compacted', updated_at=? WHERE session_id=?",
                         (_now(), sid))
            conn.commit()
            total_summaries += len(summaries)
            total_deleted += len(delete_ids)
        logger.info("compaction done: %d summaries written, %d detail events removed%s",
                    total_summaries, total_deleted, " (dry-run)" if args.dry_run else "")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
