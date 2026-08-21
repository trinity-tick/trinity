
#!/usr/bin/env python
"""
backfill_dsh_sessions.py — 把本地 DSH(web) harness 历史聊天记录回填进 Trinity 结构层。

数据源：C:\\Users\\Administrator\\.dsh\\sessions\\--C-Users-Administrator--\\<id>\\session.jsonl.zstd
（由 dsh-session-persistence-jsonl 写成：多段独立 zstd frame，Node zlib 解码见配套 JS）。
本脚本读取 normalize 后的 JSON（见 tmp_session_decode.js），对"尚未在 dsh_sessions 中的会话"
调用 structure_store.structure_sync 落库（复用线上锁安全写入路径），并回填 created_at。

用法:
  python backfill_dsh_sessions.py --data-dir <normalized json dir> [--dry-run] [--only <session_id>]
"""
import argparse, json, os, sqlite3, sys, time

# 复用 structure_store 的写路径（锁 + commit 安全）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from trinity.structure_store import structure_sync, _STRUCTURE_DB  # noqa: E402

# 与 dsh-trinity 插件 lib/index.js toStructureEvent 一致的映射
DROP_TYPES = {
    "session", "permission/preset", "sandbox/mode", "approval/policy",
    "assistant/chunk", "reasoning-chunks", "text-chunks", "tool-call-chunks",
    "step/start", "step/end", "session/title", "session/title-llm-request",
    "request/context", "llm/retry", "llm/retry-started", "compaction/prune",
    "compaction/start", "compaction/summary", "compaction/end", "schedule/change",
    "web/deepseek-search-llm-request", "agent/inbox/spliced", "session/end-seed",
}


def extract_message_text(content, limit=8000):
    if isinstance(content, str):
        return content[:limit]
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") in ("text", "reasoning"):
                parts.append(str(b.get("text", "")))
        return "\n".join(parts)[:limit]
    return ""


def to_structure_event(ev):
    d = ev.get("data") or {}
    seq = ev.get("seq")
    etype = ev.get("type")
    if etype in DROP_TYPES:
        return None
    if seq is None:
        return None
    t = ev.get("time")
    if etype == "user/message":
        msg = d.get("message") or {}
        return {"seq": seq, "type": "user/message", "turn": d.get("turn"), "step": d.get("step"),
                "time": t, "data": {"content": extract_message_text(d.get("content", d.get("message", {}).get("content"))),
                                    "source": (d.get("message") or {}).get("source", {}).get("kind", "user")}}
    if etype == "assistant/message":
        msg = d.get("message") or {}
        src = msg.get("source") or {}
        return {"seq": seq, "type": "assistant/message", "turn": d.get("turn"), "step": d.get("step"),
                "time": t, "data": {"content": extract_message_text(d.get("content", msg.get("content"))),
                                    "provider": src.get("provider"), "model": src.get("model"),
                                    "usage": d.get("usage")}}
    if etype == "tool/call":
        return {"seq": seq, "type": "tool/call", "turn": d.get("turn"), "step": d.get("step"),
                "time": t, "data": {"name": d.get("name"), "callId": d.get("callId"),
                                    "arguments": str(d.get("arguments", ""))[:2000]}}
    if etype == "tool/result":
        msg = d.get("message") or {}
        return {"seq": seq, "type": "tool/result", "turn": d.get("turn"), "step": d.get("step"),
                "time": t, "data": {"callId": msg.get("source", {}).get("callId", d.get("callId")),
                                    "error": d.get("error"), "isError": msg.get("isError", False)}}
    if etype == "turn/start":
        return {"seq": seq, "type": "turn/start", "turn": d.get("turn"), "time": t, "data": {"turn": d.get("turn")}}
    if etype == "turn/end":
        return {"seq": seq, "type": "turn/end", "turn": d.get("turn"), "time": t, "data": {"reason": d.get("reason")}}
    if etype == "request/header":
        return {"seq": seq, "type": "request/header", "time": t, "data": {"reason": d.get("reason")}}
    if etype == "todo/write":
        return {"seq": seq, "type": "todo/write", "time": t, "data": {"count": len(d.get("todos") or [])}}
    if etype == "goal/change":
        goal = d.get("goal") or d
        gid = goal.get("id") or d.get("goalId")
        if not gid:
            return None
        return {"seq": seq, "type": "goal/write", "time": t,
                "data": {"goal_id": gid, "objective": goal.get("objective", ""),
                         "phase": goal.get("phase", d.get("operation", "active")),
                         "revision": goal.get("revision", d.get("revision", 0)),
                         "roundsStarted": d.get("roundsStarted", 0),
                         "createdAt": d.get("createdAt"), "updatedAt": d.get("updatedAt"),
                         "operation": d.get("operation", "create")}}
    return None


def collect_headers_todos(events, raw_events):
    headers = []
    todos = None
    for ev in events:  # structure events, already filtered
        pass
    # headers from raw request/header events
    for ev in raw_events:
        if ev.get("type") == "request/header":
            d = ev.get("data") or {}
            headers.append({"seq": ev.get("seq", 0), "reason": d.get("reason"),
                            "header": d.get("header", {}), "time": ev.get("time")})
        if ev.get("type") == "todo/write":
            todos = [{"content": x.get("content", ""), "status": x.get("status", "pending")}
                     for x in (d.get("todos") or [])]
    return headers, todos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", default=None)
    args = ap.parse_args()

    db = sqlite3.connect(_STRUCTURE_DB, timeout=30)
    db.executescript("PRAGMA busy_timeout=30000;")
    db_ids = {r[0] for r in db.execute("SELECT session_id FROM dsh_sessions").fetchall()}
    db.close()

    manifest = json.load(open(os.path.join(args.data_dir, "_manifest.json"), encoding="utf8"))
    files = [f for f in os.listdir(args.data_dir) if f.endswith(".json") and not f.startswith("_")]
    stats = {"synced": 0, "skipped_present": 0, "errored": 0, "events": 0, "goals": 0}
    for fn in sorted(files):
        rec = json.load(open(os.path.join(args.data_dir, fn), encoding="utf8"))
        sid = rec.get("session_id")
        if not sid:
            continue
        if sid in db_ids:
            stats["skipped_present"] += 1
            continue
        if args.only and sid != args.only:
            continue
        events = []
        for ev in rec.get("events", []):
            s = to_structure_event(ev)
            if s:
                events.append(s)
        if not events:
            stats["skipped_present"] += 1  # nothing to sync
            print(f"[EMPTY] {sid} no structure events")
            continue
        headers, todos = collect_headers_todos(events, rec.get("events", []))
        params = {
            "session_id": sid,
            "agent_id": f"dsh-{sid}",
            "persona_id": "default",
            "status": "closed",
            "events": events,
        }
        if todos is not None:
            params["todos"] = todos
        if headers:
            params["headers"] = headers
        if args.dry_run:
            print(f"[DRY] {sid} events={len(events)}")
            stats["synced"] += 1
            stats["events"] += len(events)
            continue
        res = structure_sync(params)
        if res.get("error"):
            print(f"[ERR] {sid}: {res['error']}")
            stats["errored"] += 1
        else:
            # backfill created_at from header
            try:
                ca = rec.get("header", {}).get("createdAt")
                if ca:
                    sqlite3.connect(_STRUCTURE_DB, timeout=30).execute(
                        "UPDATE dsh_sessions SET created_at=? WHERE session_id=?", (ca / 1000.0, sid)
                    ).connection.commit()
            except Exception as e:
                print(f"[WARN] created_at fix {sid}: {e}")
            stats["synced"] += 1
            stats["events"] += res.get("synced", 0)
            print(f"[OK] {sid} events={res.get('synced')}")
    print("=== summary ===", stats)


if __name__ == "__main__":
    main()
