"""
structure_store — DSH 结构层共享存储（结构融合核心）

以 Trinity 为主体承载 DSH 的编排结构：会话（dsh_sessions）、事件流
（dsh_events：turn/step/消息/工具轨迹）、goal（dsh_goals）、todo
（dsh_todos）、request-header（dsh_headers）、schedule（dsh_schedules）。

本模块无副作用（不重定向 stdout、不初始化引擎），供三类调用方共享：
  - trinity/engine_worker.py（DSH 插件 spawn 的常驻进程，stdio NDJSON）
  - trinity/api/server.py（REST /structure/* 端点）
  - trinity/api/graphql_schema.py（GraphQL structure 查询）
"""

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

# 2026-08-16 修复:与 core/client.py 的解析一致。
# TRINITY_STORE 语义 = store 目录(凭证: C:\Users\Administrator\.trinity\store),
# 引擎 db = <TRINITY_STORE>/trinity_store.db;未设置时回退 ~/.trinity/store/trinity_store.db。
# 旧实现把 TRINITY_STORE 当 home 根再拼 .trinity/store/,会连到错误空库(API 重启后 stats=0)。
def _resolve_structure_db() -> str:
    env_store = os.environ.get("TRINITY_STORE")
    if env_store:
        if os.path.isdir(env_store):
            return os.path.join(env_store, "trinity_store.db")
        if os.path.isfile(env_store):
            return env_store
    return os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db")


_STRUCTURE_DB = _resolve_structure_db()
_structure_lock = threading.Lock()

_STRUCTURE_DDL = """
CREATE TABLE IF NOT EXISTS dsh_sessions (
    session_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    persona_id TEXT DEFAULT 'default',
    parent_session TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    status TEXT DEFAULT 'active',
    title TEXT
);
CREATE TABLE IF NOT EXISTS dsh_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    turn INTEGER,
    step INTEGER,
    time REAL NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_dsh_events_session ON dsh_events(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_dsh_events_type ON dsh_events(type);
CREATE TABLE IF NOT EXISTS dsh_goals (
    goal_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    status TEXT DEFAULT 'active',
    phase TEXT,
    round INTEGER DEFAULT 0,
    max_rounds INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS dsh_todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    time REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dsh_todos_session ON dsh_todos(session_id);
CREATE TABLE IF NOT EXISTS dsh_headers (
    session_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    reason TEXT,
    header TEXT NOT NULL,
    time REAL NOT NULL,
    UNIQUE(session_id, seq)
);
CREATE TABLE IF NOT EXISTS dsh_schedules (
    schedule_id TEXT PRIMARY KEY,
    prompt TEXT NOT NULL,
    target TEXT,
    status TEXT DEFAULT 'active',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""
# ── 时间戳单位契约（2026-08-21 明确，消费方务必遵守）──
# - dsh_events.time / dsh_todos.time / dsh_headers.time = 事件源直传的
#   epoch **毫秒**（DSH 插件 JS Date.now() 语义），无值回退 time.time() 秒。
#   消费方（compact_structure 等）只做透传/聚合，切勿混算秒与毫秒。
# - dsh_sessions.created_at/updated_at、dsh_goals.*、dsh_schedules.* =
#   epoch **秒**（time.time() 语义）。时效判断（如 compact --min-days）一律用秒列。


def _structure_conn() -> sqlite3.Connection:
    """打开结构层连接（调用方自行持有 _structure_lock 保证原子性）。"""
    os.makedirs(os.path.dirname(_STRUCTURE_DB), exist_ok=True)
    conn = sqlite3.connect(_STRUCTURE_DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_STRUCTURE_DDL)
    return conn


def structure_sync(params: dict) -> dict:
    """同步一个 DSH 会话的结构（会话元数据 + 事件流 + todos + headers）。"""
    with _structure_lock:
        conn = _structure_conn()
        try:
            now = time.time()
            session = params.get("session") or {}
            session_id = session.get("session_id") or params.get("session_id")
            if not session_id:
                return {"error": "session_id required", "synced": 0}
            agent_id = session.get("agent_id") or params.get("agent_id") or "default"
            parent = session.get("parent_session") or params.get("parent_session")
            title = session.get("title") or params.get("title")
            status = session.get("status") or params.get("status") or "active"
            conn.execute(
                """INSERT INTO dsh_sessions(session_id, agent_id, persona_id, parent_session,
                                            created_at, updated_at, status, title)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                     agent_id=excluded.agent_id, status=excluded.status,
                     updated_at=excluded.updated_at,
                     title=COALESCE(excluded.title, dsh_sessions.title),
                     parent_session=COALESCE(excluded.parent_session, dsh_sessions.parent_session)""",
                (session_id, agent_id, session.get("persona_id", "default"),
                 parent, now, now, status, title),
            )
            events = params.get("events") or []
            ev_count = 0
            for ev in events:
                seq = ev.get("seq")
                if seq is None:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO dsh_events
                       (session_id, seq, type, turn, step, time, payload)
                       VALUES(?,?,?,?,?,?,?)""",
                    (session_id, seq, ev.get("type", "unknown"),
                     ev.get("turn"), ev.get("step"),
                     ev.get("time", now), json.dumps(ev.get("data", {}), ensure_ascii=False)),
                )
                ev_count += 1
                # goal/schedule 自动同步（2026-08-15）：解析工具调用事件中的
                # create_goal/update_goal/schedule_create 参数 → 写入结构表。
                # 使 DSH goal 生命周期自动反映到 dsh_goals（无需显式 trinity_goal）。
                _sync_goal_schedule_from_event(conn, ev, now)
            todos = params.get("todos")
            if todos is not None:
                conn.execute("DELETE FROM dsh_todos WHERE session_id=?", (session_id,))
                for item in todos:
                    conn.execute(
                        "INSERT INTO dsh_todos(session_id, content, status, time) VALUES(?,?,?,?)",
                        (session_id, item.get("content", ""), item.get("status", "pending"), now),
                    )
            headers = params.get("headers") or []
            for h in headers:
                conn.execute(
                    """INSERT OR IGNORE INTO dsh_headers(session_id, seq, reason, header, time)
                       VALUES(?,?,?,?,?)""",
                    (session_id, h.get("seq", 0), h.get("reason", ""),
                     json.dumps(h.get("header", {}), ensure_ascii=False), h.get("time", now)),
                )
            conn.commit()
            return {"synced": ev_count, "session_id": session_id}
        except Exception as exc:
            conn.rollback()
            return {"error": str(exc), "synced": 0}
        finally:
            conn.close()


_GOAL_ACTION_STATUS = {
    "create": "active",
    "edit": None,          # 保留现有状态
    "pause": "paused",
    "resume": "active",
    "complete": "completed",
    "blocked": "blocked",
}


def _sync_goal_schedule_from_event(conn, ev: dict, now: float) -> None:
    """从 tool/call 或 goal/write 事件解析 goal/schedule 变更并写入结构表。

    2026-08-15：
      - tool/call 的 create_goal/update_goal（DSH 内置工具，create 无 goal_id）
      - goal/write（插件新增：DSH goal/change 快照，含完整 GoalSnapshot）
    在 structure_sync 持有锁的同一连接上操作（避免 goal_upsert 锁重入）。
    """
    if ev.get("type") == "goal/write":
        data = ev.get("data") or {}
        gid = data.get("goal_id")
        if not gid:
            return
        objective = data.get("objective", "")
        phase = data.get("phase", "active")
        status = _GOAL_ACTION_STATUS.get(data.get("operation"), None) or \
            {"active": "active", "complete": "completed", "paused": "paused",
             "blocked": "blocked"}.get(phase, phase)
        conn.execute(
            """INSERT INTO dsh_goals(goal_id, objective, status, phase, round, max_rounds,
                                     created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(goal_id) DO UPDATE SET
                 objective=COALESCE(NULLIF(excluded.objective,''), dsh_goals.objective),
                 status=excluded.status, phase=excluded.phase,
                 round=excluded.round, max_rounds=COALESCE(excluded.max_rounds, dsh_goals.max_rounds),
                 updated_at=excluded.updated_at""",
            (gid, objective, status, phase, int(data.get("roundsStarted", 0) or 0),
             None, data.get("createdAt") or now, data.get("updatedAt") or now),
        )
        return

    if ev.get("type") != "tool/call":
        return
    data = ev.get("data") or {}
    name = data.get("name", "")
    raw_args = data.get("arguments")
    try:
        a = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
    except Exception:
        return
    if not isinstance(a, dict):
        return

    if name in ("create_goal", "update_goal") and (a.get("goal_id") or name == "create_goal"):
        # DSH 系统内置 create_goal 工具不携带 goal_id（参数为 objective/
        # max_goal_rounds，goal_id 由 harness 生成）。用 objective 哈希生成
        # 稳定 id，使后续 update_goal（携带真实 goal_id）能合并到同一行。
        gid = a.get("goal_id")
        if not gid and name == "create_goal":
            import hashlib as _hl
            obj = a.get("objective", "")
            gid = f"goal-{_hl.sha256(obj.encode()).hexdigest()[:16]}" if obj else f"goal-sys-{int(now)}"
        action = a.get("action", "create")
        status = _GOAL_ACTION_STATUS.get(action)
        if status is None:
            status = a.get("status") or "active"
        conn.execute(
            """INSERT INTO dsh_goals(goal_id, objective, status, phase, round, max_rounds,
                                     created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(goal_id) DO UPDATE SET
                 objective=COALESCE(NULLIF(excluded.objective,''), dsh_goals.objective),
                 status=excluded.status,
                 phase=excluded.phase, round=excluded.round,
                 max_rounds=excluded.max_rounds, updated_at=excluded.updated_at""",
            (gid, a.get("objective", ""), status, a.get("phase"),
             a.get("round", 0), a.get("max_rounds"), now, now),
        )
    elif name == "schedule_create" and a.get("schedule_id"):
        sid = a["schedule_id"]
        target = (a.get("target") or a.get("at")
                  or (str(a.get("after_seconds")) + "s" if a.get("after_seconds") else "")
                  or (str(a.get("every_seconds")) + "s" if a.get("every_seconds") else ""))
        conn.execute(
            """INSERT INTO dsh_schedules(schedule_id, prompt, target, status, created_at, updated_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(schedule_id) DO UPDATE SET
                 prompt=excluded.prompt, target=excluded.target,
                 status=excluded.status, updated_at=excluded.updated_at""",
            (sid, a.get("prompt", ""), target, "active", now, now),
        )


def structure_query(params: dict) -> dict:
    """查询 DSH 结构：按 session 取事件流（可回放），或按类型/agent 过滤。"""
    conn = _structure_conn()
    try:
        session_id = params.get("session_id")
        event_type = params.get("type")
        agent_id = params.get("agent_id")
        limit = min(int(params.get("limit", 200)), 2000)
        where: List[str] = []
        args: list = []
        if session_id:
            where.append("e.session_id=?")
            args.append(session_id)
        if event_type:
            where.append("e.type=?")
            args.append(event_type)
        if agent_id:
            where.append("s.agent_id=?")
            args.append(agent_id)
        sql = ("SELECT e.session_id, e.seq, e.type, e.turn, e.step, e.time, e.payload "
               "FROM dsh_events e LEFT JOIN dsh_sessions s ON s.session_id=e.session_id")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY e.session_id, e.seq DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        events = [
            {
                "session_id": r["session_id"], "seq": r["seq"], "type": r["type"],
                "turn": r["turn"], "step": r["step"], "time": r["time"],
                "data": json.loads(r["payload"] or "{}"),
            }
            for r in rows
        ]
        return {"events": events, "count": len(events)}
    finally:
        conn.close()


def structure_sessions(params: dict = None) -> dict:
    conn = _structure_conn()
    try:
        rows = conn.execute(
            """SELECT session_id, agent_id, persona_id, parent_session, created_at,
                      updated_at, status, title
               FROM dsh_sessions ORDER BY updated_at DESC LIMIT 200""").fetchall()
        return {"sessions": [dict(r) for r in rows]}
    finally:
        conn.close()


def structure_stats(params: dict = None) -> dict:
    conn = _structure_conn()
    try:
        stats: Dict[str, Any] = {
            "sessions": conn.execute("SELECT COUNT(*) FROM dsh_sessions").fetchone()[0],
            "events": conn.execute("SELECT COUNT(*) FROM dsh_events").fetchone()[0],
            "goals": conn.execute("SELECT COUNT(*) FROM dsh_goals").fetchone()[0],
            "todos": conn.execute("SELECT COUNT(*) FROM dsh_todos").fetchone()[0],
            "headers": conn.execute("SELECT COUNT(*) FROM dsh_headers").fetchone()[0],
            "schedules": conn.execute("SELECT COUNT(*) FROM dsh_schedules").fetchone()[0],
            "event_types": {},
        }
        for r in conn.execute("SELECT type, COUNT(*) c FROM dsh_events GROUP BY type"):
            stats["event_types"][r["type"]] = r["c"]
        return stats
    finally:
        conn.close()


def goal_upsert(params: dict) -> dict:
    with _structure_lock:
        conn = _structure_conn()
        try:
            now = time.time()
            gid = params.get("goal_id", "")
            if not gid:
                return {"error": "goal_id required"}
            conn.execute(
                """INSERT INTO dsh_goals(goal_id, objective, status, phase, round, max_rounds,
                                         created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(goal_id) DO UPDATE SET
                     objective=excluded.objective, status=excluded.status,
                     phase=excluded.phase, round=excluded.round,
                     max_rounds=excluded.max_rounds, updated_at=excluded.updated_at""",
                (gid, params.get("objective", ""), params.get("status", "active"),
                 params.get("phase"), params.get("round", 0), params.get("max_rounds"),
                 now, now),
            )
            conn.commit()
            # 2026-08-26（Budibase 借鉴 Phase 1）：goal.updated 事件（默认关闭）
            try:
                from trinity.automation import emit as _automation_emit
                _automation_emit("goal.updated", {
                    "goal_id": gid,
                    "status": params.get("status", "active"),
                    "phase": params.get("phase") or "",
                    "objective": (params.get("objective") or "")[:200],
                })
            except Exception:
                pass
            return {"goal_id": gid, "status": params.get("status", "active")}
        except Exception as exc:
            conn.rollback()
            return {"error": str(exc)}
        finally:
            conn.close()


def goal_list(params: dict = None) -> dict:
    conn = _structure_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM dsh_goals ORDER BY updated_at DESC LIMIT 100").fetchall()
        return {"goals": [dict(r) for r in rows]}
    finally:
        conn.close()


def schedule_upsert(params: dict) -> dict:
    """结构化追踪 DSH schedule（会话内定时提醒）到 Trinity。"""
    with _structure_lock:
        conn = _structure_conn()
        try:
            now = time.time()
            sid = params.get("schedule_id", "")
            if not sid:
                return {"error": "schedule_id required"}
            conn.execute(
                """INSERT INTO dsh_schedules(schedule_id, prompt, target, status,
                                             created_at, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(schedule_id) DO UPDATE SET
                     prompt=excluded.prompt, target=excluded.target,
                     status=excluded.status, updated_at=excluded.updated_at""",
                (sid, params.get("prompt", ""), params.get("target"),
                 params.get("status", "active"), now, now),
            )
            conn.commit()
            return {"schedule_id": sid, "status": params.get("status", "active")}
        except Exception as exc:
            conn.rollback()
            return {"error": str(exc)}
        finally:
            conn.close()


def schedule_list(params: dict = None) -> dict:
    conn = _structure_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM dsh_schedules ORDER BY updated_at DESC LIMIT 100").fetchall()
        return {"schedules": [dict(r) for r in rows]}
    finally:
        conn.close()
