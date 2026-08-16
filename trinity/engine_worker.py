"""
trinity_engine_worker — DSH 融合的引擎侧常驻进程（F1）

DSH 原生插件 spawn 本进程，通过 stdio NDJSON 直连 Trinity 引擎，
取代"DSH → trinity-mcp → JSON-RPC/MCP 协议 → 引擎"的中间层。

协议（每行一个 JSON）：
    请求:  {"id": 1, "method": "search", "params": {...}}
    响应:  {"id": 1, "result": {...}}
    错误:  {"id": 1, "error": {"message": "..."}}

stdout 隔离：引擎初始化日志走 stdout，因此启动时用 os.dup(1) 保留
干净协议 fd，再把 sys.stdout 重定向到 stderr（日志进 stderr），
协议写入保留的 fd。

方法（与 MCP 8 工具对齐，去掉协议层）：
    ping / search / write / update / delete / audit / diagnostics /
    chronicle / tag_search / identity_register
"""

import json
import os
import sqlite3
import sys
import threading
import traceback
import time
from datetime import datetime, timezone
from typing import Any, Optional

# ── stdout 隔离（必须在 import trinity 之前）──────────────────────
_PROTO_FD = os.dup(1)
_PROTO = os.fdopen(_PROTO_FD, "w", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr  # 引擎日志进 stderr，不再污染协议

# 2026-08-16 修复：强制 stdin/stderr 使用 UTF-8（Windows 下 sys.stdin 默认按
# locale 代码页如 cp936 解码 Node 写入的 UTF-8 字节，中文会损坏成孤立代理项，
# 导致 json.dumps().encode('utf-8') 抛 UnicodeEncodeError）。
# errors="backslashreplace" 保证日志/错误信息始终可写，不因编码崩溃。
try:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except (AttributeError, ValueError):
    pass


# ── 引擎导入（1.5s 初始化，进程内只做一次）────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.core.client import Trinity  # noqa: E402

_engine: Optional[Trinity] = None
_session_recorder: Any = None

# ── DSH 结构层（共享模块：worker/API/GraphQL 三方共用）──────
# DSH 的结构框架（会话事件流 / turn-step / 工具轨迹 / goal / todo /
# request-header / schedule）由 Trinity 原生承载：可查、可回放、可审计。
# 实现在 trinity/structure_store.py（无 stdout 副作用，可被 API 安全引用）。
from trinity.structure_store import (  # noqa: E402
    structure_sync as _structure_sync,
    structure_query as _structure_query,
    structure_sessions as _structure_sessions,
    structure_stats as _structure_stats,
    goal_upsert as _goal_upsert,
    goal_list as _goal_list,
    schedule_upsert as _schedule_upsert,
    schedule_list as _schedule_list,
)


def _get_engine() -> Trinity:
    global _engine
    if _engine is None:
        _engine = Trinity()
    return _engine


def _get_recorder() -> Any:
    global _session_recorder
    if _session_recorder is None:
        from trinity.session_recorder import ChatSessionRecorder
        _session_recorder = ChatSessionRecorder()
    return _session_recorder


# ── 方法实现（与 memory_tools.py 对齐，去掉 MCP/遥测层）───────────

def _ping(params: dict) -> dict:
    """Ping + 版本握手：返回协议版本与引擎版本，供 DSH 插件做兼容性检测。

    协议版本 protocol_version 由本文件维护，引擎接口变更时递增；
    engine_version 来自引擎 diagnostics，用于判断 Trinity 版本兼容性。
    """
    try:
        diag = _get_engine().diagnostics()
        if isinstance(diag, dict):
            version = diag.get("trinity_version") or diag.get("source_version") or "unknown"
        else:
            version = "unknown"
    except Exception:
        version = "unknown"
    return {
        "pong": True,
        "ts": datetime.now(timezone.utc).isoformat(),
        "protocol_version": 1,
        "engine_version": version,
    }


def _search(params: dict) -> dict:
    engine = _get_engine()
    result = engine.search(
        query=params.get("query", ""),
        top_k=params.get("top_k", 5),
        mode=params.get("mode", "hybrid"),
        persona_id=params.get("persona_id"),
        tenant_id=params.get("tenant_id"),
        agent_id=params.get("agent_id"),
        session_id=params.get("session_id"),
        category=params.get("category"),
    )
    results = result.get("results", result if isinstance(result, list) else [])
    # 空结果回退会话全文搜索（与 MCP 行为一致）
    if not results:
        rec = _get_recorder()
        fallback = rec.search(query=params.get("query", ""), top_k=params.get("top_k", 5))
        if fallback:
            results = [
                {
                    "session_id": r["session_id"],
                    "content": r["content"],
                    "role": r["role"],
                    "timestamp": r["timestamp"],
                    "tags": r["tags"],
                    "score": r["score"],
                    "source": "session_recorder",
                }
                for r in fallback
            ]
    return {"results": results}


def _write(params: dict) -> dict:
    engine = _get_engine()
    content = params.get("content", "")
    metadata = params.get("metadata") or {}
    # F4：agent_id/session_id 显式参数（优先于 metadata 内嵌），保证落库
    agent_id = params.get("agent_id") or metadata.get("agent_id") or "default"
    session_id = params.get("session_id") or metadata.get("session_id")
    result = engine.ingest(
        content=content,
        role=metadata.get("role", "user"),
        importance=params.get("importance", 0.5),
        tags=params.get("tags") or [],
        category=params.get("category", "general"),
        metadata=metadata,
        agent_id=agent_id,
        session_id=session_id,
        postprocess=False,
    )
    memory_id = result.get("memory_id", "")
    if memory_id:
        # 后台加工（语义关联/实体提取/推送）不阻塞写入
        threading.Thread(
            target=engine._postprocess_memory,
            args=(memory_id, content),
            kwargs={"result": result},
            daemon=True,
        ).start()
    return result


def _update(params: dict) -> dict:
    engine = _get_engine()
    return engine.update_memory(
        memory_id=params.get("memory_id", ""),
        new_content=params.get("new_content", ""),
    )


def _delete(params: dict) -> dict:
    engine = _get_engine()
    memory_id = params.get("memory_id", "")
    deleted = engine.delete_memory(memory_id=memory_id)
    if not deleted:
        raise ValueError(f"Memory not found: {memory_id}")
    return {
        "memory_id": memory_id,
        "deleted": True,
        "deleted_version": f"{memory_id}_del",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _audit(params: dict) -> dict:
    engine = _get_engine()
    memory_id = params.get("memory_id", "")
    chain = engine.get_version_chain(memory_id=memory_id)
    if not chain:
        raise ValueError(f"Memory not found: {memory_id}")
    return {
        "memory_id": memory_id,
        "version_chain": chain,
        "total_versions": len(chain),
        "current_status": chain[-1].get("operation", ""),
    }


def _diagnostics(params: dict) -> dict:
    return _get_engine().diagnostics()


def _chronicle(params: dict) -> dict:
    rec = _get_recorder()
    events = params.get("events") or []
    title = params.get("title", "")
    sid = params.get("session_id")
    if title and not sid:
        sid = rec.start_session(task=title)
    elif sid is None and rec.current_session is None:
        sid = rec.start_session(task=title or "chronicle")
    all_tags: list[str] = []
    for event in events:
        result = rec.record_turn(
            role=event.get("role", "user"),
            content=event.get("content", ""),
            metadata=event.get("metadata"),
            session_id=sid,
        )
        all_tags.extend(result.get("tags", []))
    return {
        "session_id": sid or rec.current_session,
        "event_count": len(events),
        "tags": list(set(all_tags)),
    }


def _tag_search(params: dict) -> dict:
    rec = _get_recorder()
    tags = params.get("tags") or []
    top_k = params.get("top_k", 10)
    session_id = params.get("session_id")
    tag_set = set(t.lower() for t in tags)

    def _scan(sess_id: str):
        session = rec.get_session(sess_id)
        if not session:
            return []
        out = []
        for i, turn in enumerate(session.get("turns", [])):
            turn_tags = set(t.lower() for t in turn.get("tags", []))
            if turn_tags & tag_set:
                out.append({
                    "session_id": sess_id,
                    "turn_index": i,
                    "role": turn.get("role", "unknown"),
                    "content": turn.get("content", ""),
                    "timestamp": turn.get("timestamp", 0.0),
                    "tags": turn.get("tags", []),
                    "match_type": "tag_or",
                })
        return out

    matches: list[dict] = []
    if session_id:
        matches = _scan(session_id)
    else:
        for summary in rec.list_all_sessions():
            matches.extend(_scan(summary["session_id"]))
    matches.sort(key=lambda m: m["timestamp"], reverse=True)
    return {"results": matches[:top_k]}


def _identity_register(params: dict) -> dict:
    engine = _get_engine()
    agent_id = params.get("agent_id", "")
    name = params.get("name", agent_id)
    # 注册身份锚点（F4：DSH 会话自动成为 Trinity 身份）
    try:
        result = engine.register_identity_anchor(
            agent_id=agent_id,
            anchor_type="agent",
            value=name,
        )
    except Exception as exc:  # 锚点已存在等场景不致命
        result = {"status": "exists_or_failed", "detail": str(exc)}
    return {"agent_id": agent_id, "registered": True, "detail": result}


def _batch_write(params: dict) -> dict:
    """批量写入（结构融合：DSH session/event 流 → Trinity 记忆）。

    params:
        events: [{content, role?, category?, tags?, importance?, metadata?}, ...]
        agent_id / session_id: 统一归属（缺省 per-event metadata）
    逐条走 engine.ingest（postprocess=False 不阻塞），返回每条的 memory_id 与错误。
    """
    engine = _get_engine()
    events = params.get("events") or []
    default_agent = params.get("agent_id") or "default"
    default_session = params.get("session_id")
    results = []
    errors = []
    for i, ev in enumerate(events):
        try:
            content = ev.get("content", "")
            if not content:
                continue
            metadata = dict(ev.get("metadata") or {})
            agent_id = ev.get("agent_id") or default_agent
            session_id = ev.get("session_id") or default_session
            metadata.setdefault("source", "dsh-session-stream")
            r = engine.ingest(
                content=content,
                role=ev.get("role", "user"),
                importance=ev.get("importance", 0.5),
                tags=ev.get("tags") or [],
                category=ev.get("category", "general"),
                metadata=metadata,
                agent_id=agent_id,
                session_id=session_id,
                postprocess=False,
            )
            mid = r.get("memory_id", "")
            if mid:
                # 后台加工不阻塞批量写入
                threading.Thread(
                    target=engine._postprocess_memory,
                    args=(mid, content),
                    kwargs={"result": r},
                    daemon=True,
                ).start()
            results.append({"index": i, "memory_id": mid, "sha256_hash": r.get("sha256_hash")})
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})
    return {"written": len(results), "errors": errors, "items": results}



def _session_dispose_summary(params: dict) -> dict:
    """会话销毁钩子(2026-08-16):从结构层事件流生成抽取式摘要记忆(幂等)。

    实时触发(插件 session/disposed),LLM 增强版由维护链 session-auto 任务
    (scripts/auto_session_summary.py)负责;两者都检查已有 session-auto-summary,
    不会重复落库。
    """
    import sqlite3 as _sqlite3
    sid = params.get("session_id", "")
    if not sid:
        return {"status": "noop", "reason": "no session_id"}
    db = os.path.expanduser("~/.trinity/store/trinity_store.db")
    conn = _sqlite3.connect(db, timeout=15)
    try:
        aid = f"dsh-{sid}"
        dup = conn.execute(
            "SELECT COUNT(*) FROM memories WHERE agent_id=? AND tags LIKE '%session-auto-summary%'",
            (aid,),
        ).fetchone()[0]
        if dup:
            return {"status": "skipped", "reason": "already summarized"}
        rows = conn.execute(
            "SELECT type, payload FROM dsh_events WHERE session_id=? "
            "AND type IN ('user/message','assistant/message') ORDER BY seq",
            (sid,),
        ).fetchall()
        lines = []
        for r in rows[-40:]:
            try:
                p = json.loads(r[1]) if isinstance(r[1], str) else (r[1] or {})
                c = p.get("content") or p.get("text") or ""
                if c:
                    prefix = "U: " if r[0] == "user/message" else "A: "
                    lines.append(prefix + str(c)[:600])
            except Exception:
                continue
        if not lines:
            return {"status": "noop", "reason": "no message events"}
        transcript = "\n".join(lines)[:6000]
        summary = (
            "[会话结束自动沉淀(抽取式)]\n--- 会话开头 ---\n"
            + transcript[:2500]
            + "\n--- 会话结尾 ---\n"
            + transcript[-2500:]
        )
        content = f"[会话结束自动沉淀] {sid}\n{summary}"
        import uuid as _uuid
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content, role, importance, tags, category, status, version, sha256_hash, created_at, updated_at, access_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"summ_auto_{sid[:12]}_{int(time.time())}", sid, "default", aid, content,
             "assistant", 0.7, json.dumps(["session-auto-summary", "session"], ensure_ascii=False),
             "session", "active", 1, "", now, now, 0),
        )
        conn.commit()
        return {"status": "created", "session_id": sid}
    except Exception as exc:
        return {"status": "error", "detail": str(exc)}
    finally:
        conn.close()


_METHODS = {
    "ping": _ping,
    "search": _search,
    "write": _write,
    "batch_write": _batch_write,
    "update": _update,
    "delete": _delete,
    "audit": _audit,
    "diagnostics": _diagnostics,
    "chronicle": _chronicle,
    "tag_search": _tag_search,
    "identity_register": _identity_register,
    "session_dispose_summary": _session_dispose_summary,
    # ── DSH 结构层（结构融合核心）──
    "structure_sync": _structure_sync,
    "structure_query": _structure_query,
    "structure_sessions": _structure_sessions,
    "structure_stats": _structure_stats,
    "goal_upsert": _goal_upsert,
    "goal_list": _goal_list,
    "schedule_upsert": _schedule_upsert,
    "schedule_list": _schedule_list,
}


def _emit(obj: dict) -> None:
    _PROTO.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            _emit({"id": None, "error": {"message": f"invalid JSON: {exc}"}})
            continue
        req_id = req.get("id")
        method = req.get("method", "")
        params = req.get("params") or {}
        handler = _METHODS.get(method)
        if handler is None:
            _emit({"id": req_id, "error": {"message": f"unknown method: {method}"}})
            continue
        try:
            result = handler(params)
            _emit({"id": req_id, "result": result})
        except Exception as exc:
            _emit({
                "id": req_id,
                "error": {"message": str(exc), "trace": traceback.format_exc()[-2000:]},
            })
    return 0


if __name__ == "__main__":
    sys.exit(main())
