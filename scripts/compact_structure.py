#!/usr/bin/env python3
"""
Trinity — 结构层 Compaction（2026-08-15；2026-08-21 新增 token 预算模式）
=====================================================================
控制 dsh_events 表增长：把**已结束/过期会话**的逐事件明细按 turn 聚合为
"compacted_turn" 摘要事件（保留轨迹统计 + 助手段落摘要），删除明细行，
并标记会话 status='compacted'。

两种模式（互斥语义，二选一）：
1. 时效模式（默认）：--min-days N（默认 1）——只处理 updated_at 早于 N 天
   的非 active 会话（旧行为，不动近期会话）。
2. **token 预算模式（P0-3，借鉴 codex RETAINED_MESSAGE_TOKEN_BUDGET）**：
   --budget-tokens N——处理全部非 active 非 compacted 会话，**为每个会话
   保留最近 N token 的明细原文**（按 turn 边界对齐），更早的明细按 turn
   聚合为 compacted_turn 摘要并删除；若保留尾部仍超预算，按裁剪优先级
   先裁 tool/result（最大噪音源）→ tool/call → 其他，**用户/助手段落永不裁**。

幂等：已 compacted 的会话自动跳过；--dry-run 只预览。

时间戳单位（2026-08-21 契约，见 trinity/structure_store.py 注释）：
- dsh_sessions.updated_at = epoch **秒**（本脚本 --min-days 时效判断只用它）
- dsh_events.time = epoch **毫秒**（事件源直传；本脚本写入 compacted_turn 时
  原样透传原事件的毫秒值，不做单位换算）

用法：
    python scripts/compact_structure.py --dry-run
    python scripts/compact_structure.py                 # 时效模式：1 天前
    python scripts/compact_structure.py --min-days 1    # 同上（显式）
    python scripts/compact_structure.py --budget-tokens 32768   # 预算模式
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
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("compact_structure")

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")

# 裁剪优先级（越小越先裁）：tool/result 是最大噪音源（工具输出通常最大）
_TRIM_RANK = {"tool/result": 0, "tool/call": 1}
_MSG_TYPES = ("user/message", "assistant/message")


def _now() -> float:
    return time.time()


def _est_tokens(payload: Any) -> int:
    """粗略 token 估算（CJK 密集启发式：约 1 token/4 字符 + 每条事件固定开销）。"""
    try:
        text = json.dumps(payload, ensure_ascii=False) if payload else ""
    except Exception:
        text = ""
    return max(1, len(text) // 4 + 24)


def _trim_rank(etype: str) -> int:
    if etype in _MSG_TYPES:
        return 3  # 永不裁
    return _TRIM_RANK.get(etype, 2)


def candidate_sessions(conn, min_days: float, force: bool, session_id: Optional[str],
                       budget_mode: bool = False) -> List[Dict[str, Any]]:
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
        if not budget_mode and status != "active" and updated and updated > cutoff:
            continue  # 时效模式：近期仍有更新的非 active 会话暂不压缩
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
        messages = sum(1 for e in evs if e["type"] in _MSG_TYPES)
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


def plan_budget_compaction(conn, session_id: str, budget: int
                           ) -> Tuple[List[Dict[str, Any]], List[int], int, Dict[str, int], int, int]:
    """token 预算模式：保留最近 budget token 明细（turn 边界对齐）+ 摘要更早部分。

    返回 (summaries, delete_ids, kept_count, dropped, kept_tokens, cut_turn)：
      dropped = {"tool/result": n, "tool/call": n, "other": n} 尾部超额兜底裁剪数
    """
    rows = conn.execute(
        "SELECT id, type, turn, time, payload FROM dsh_events "
        "WHERE session_id=? AND turn IS NOT NULL AND turn > 0 ORDER BY turn, seq",
        (session_id,),
    ).fetchall()
    if not rows:
        return [], [], 0, {"tool/result": 0, "tool/call": 0, "other": 0}, 0, 0

    tokens = [_est_tokens(r["payload"]) for r in rows]
    total = sum(tokens)
    if total <= budget:
        # 整会话在预算内 → 保留全部明细，不压缩、不标记
        return [], [], len(rows), {"tool/result": 0, "tool/call": 0, "other": 0}, total, 0

    # 1) 从尾部累计预算内的明细（保留最近上下文）
    used = 0
    keep_from = len(rows)
    for i in range(len(rows) - 1, -1, -1):
        if used + tokens[i] > budget:
            break
        used += tokens[i]
        keep_from = i
    if keep_from == len(rows):
        keep_from = len(rows) - 1

    # 2) 对齐 turn 边界：切点所在 turn 整体保留（避免半 turn 摘要/删除）
    cut_turn = rows[keep_from]["turn"]
    keep_start = 0
    for i, r in enumerate(rows):
        if r["turn"] >= cut_turn:
            keep_start = i
            break

    # 3) 尾部超预算兜底：按优先级裁 tool/result → tool/call → 其他（消息永不裁）
    dropped = {"tool/result": 0, "tool/call": 0, "other": 0}
    drop_ids: set = set()

    def _tail_tokens() -> int:
        return sum(tokens[i] for i in range(keep_start, len(rows)) if rows[i]["id"] not in drop_ids)

    while _tail_tokens() > budget:
        best: Optional[Tuple[Tuple[int, int], int]] = None  # ((rank, -tokens), idx)
        for i in range(keep_start, len(rows)):
            if rows[i]["id"] in drop_ids:
                continue
            rank = _trim_rank(rows[i]["type"])
            if rank == 3:
                continue
            key = (rank, -tokens[i])
            if best is None or key < best[0]:
                best = (key, i)
        if best is None:
            break
        idx = best[1]
        drop_ids.add(rows[idx]["id"])
        typ = rows[idx]["type"]
        if typ == "tool/result":
            dropped["tool/result"] += 1
        elif typ == "tool/call":
            dropped["tool/call"] += 1
        else:
            dropped["other"] += 1

    # 4) 更早部分（切点 turn 之前）按 turn 聚合摘要
    compacted_rows = rows[:keep_start]
    turns: Dict[int, List[Dict[str, Any]]] = {}
    for r in compacted_rows:
        turns.setdefault(r["turn"], []).append(r)
    summaries: List[Dict[str, Any]] = []
    delete_ids: List[int] = [r["id"] for r in compacted_rows]
    for turn in sorted(turns):
        evs = turns[turn]
        tool_calls = sum(1 for e in evs if e["type"] == "tool/call")
        messages = sum(1 for e in evs if e["type"] in _MSG_TYPES)
        times = [e["time"] or 0 for e in evs if e["time"]]
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

    delete_ids.extend(sorted(drop_ids))
    kept_count = len(rows) - len(compacted_rows) - len(drop_ids)
    kept_tokens = _tail_tokens()
    return summaries, delete_ids, kept_count, dropped, kept_tokens, cut_turn


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity structure-layer compaction")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-days", type=float, default=1.0,
                        help="时效模式：只处理 updated_at 早于 N 天的非 active 会话")
    parser.add_argument("--budget-tokens", type=int, default=0,
                        help="token 预算模式：每会话保留最近 N token 明细原文 + 更早部分聚合摘要；0 = 关闭（时效模式）")
    parser.add_argument("--force", action="store_true", help="包含 active 会话（测试）")
    parser.add_argument("--session", default="", help="指定会话 id")
    args = parser.parse_args()

    budget_mode = args.budget_tokens > 0
    import sqlite3
    conn = sqlite3.connect(args.sqlite_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        cands = candidate_sessions(conn, args.min_days, args.force, args.session or None,
                                   budget_mode=budget_mode)
        logger.info("candidate sessions: %d (mode=%s)", len(cands),
                    "budget" if budget_mode else "age")
        total_deleted = 0
        total_summaries = 0
        total_kept = 0
        total_dropped = {"tool/result": 0, "tool/call": 0, "other": 0}
        for c in cands:
            sid = c["session_id"]
            if budget_mode:
                summaries, delete_ids, kept, dropped, kept_tokens, cut_turn = \
                    plan_budget_compaction(conn, sid, args.budget_tokens)
                if not summaries and not delete_ids:
                    logger.info("session %s: within budget (%d tokens), untouched",
                                sid[:20], kept_tokens)
                    continue
                logger.info(
                    "session %s: %d summaries, %d detail events removed, %d events kept "
                    "(%d tokens, dropped=%s)",
                    sid[:20], len(summaries), len(delete_ids), kept, kept_tokens, dropped,
                )
                if args.dry_run:
                    total_summaries += len(summaries)
                    total_deleted += len(delete_ids)
                    total_kept += kept
                    for k in total_dropped:
                        total_dropped[k] += dropped[k]
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
                detail = json.dumps({
                    "mode": "budget", "budget_tokens": args.budget_tokens,
                    "kept_events": kept, "kept_tokens": kept_tokens,
                    "dropped": dropped, "cut_turn": cut_turn,
                }, ensure_ascii=False)
                conn.execute("UPDATE dsh_sessions SET status='compacted', updated_at=? "
                             "WHERE session_id=?", (_now(), sid))
                conn.commit()
                total_summaries += len(summaries)
                total_deleted += len(delete_ids)
                total_kept += kept
                for k in total_dropped:
                    total_dropped[k] += dropped[k]
            else:
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
        logger.info("compaction done: %d summaries written, %d detail events removed%s%s",
                    total_summaries, total_deleted,
                    ", %d events kept" % total_kept if budget_mode else "",
                    " (dry-run)" if args.dry_run else "")
        if budget_mode and any(total_dropped.values()):
            logger.info("budget trim drops: %s", total_dropped)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
