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

# 2026-08-17（worker 卡死根因修复）: worker 只需引擎功能，禁用 import 期
# 聚合器自举——trinity/__init__ 的 ensure_bootstrapped() 会创建共享
# MemoryAggregator 并启动 agg-ann-prewarm（大库 11k+ 条 faiss 全量构建数分钟，
# GIL 饥饿把主循环拖死，ping/write 排队超时）。聚合器由 rl_feedback 等按需懒创建。
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
# 2026-08-17（锁争用根治）: 写锁等待 3s 快速失败（默认 15s 的多步写入可叠加
# >60s 工具超时），由 _retry_on_locked 自动重试，最坏秒级失败+重试而非卡死。
os.environ.setdefault("TRINITY_SQLITE_BUSY_TIMEOUT_MS", "3000")

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
    global _engine, _prewarm_done
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = Trinity()
                _prewarm_done = True  # 任一路径完成初始化即视为预热完成
    return _engine


# ── 首请求预热（2026-08-22 优化）──────────────────────────────────
# worker 首请求懒初始化引擎：Trinity() 连接大库 + 建表 + FTS/jieba 预热，
# 实测 5-30s。启动后用一个 daemon 后台线程预先完成初始化 + 一次轻量
# 只读 FTS 查询，使后续首请求不再承担这段初始化。
# 开关：TRINITY_WORKER_PREWARM（默认 on）；TRINITY_MEMORY_ENABLED=0
# （worker 默认形态：仅引擎、聚合器懒创建）时跳过，保持现状。
_PREWARM_QUERY = "prewarm"  # 极短只读探针，仅触发 FTS 快通道，不写库
_engine_lock = threading.Lock()
_prewarm_done = False


def should_prewarm(env) -> bool:
    """判定是否应启用首请求预热（纯函数，便于单测）。

    三态：
      - TRINITY_MEMORY_ENABLED=0 → False（保持现状：聚合器懒创建形态不预热）
      - TRINITY_WORKER_PREWARM ∈ {off,0,false,no} → False（显式关闭）
      - 其余（默认）→ True
    """
    if str(env.get("TRINITY_MEMORY_ENABLED", "")).strip().lower() == "0":
        return False
    prewarm = str(env.get("TRINITY_WORKER_PREWARM", "on")).strip().lower()
    return prewarm not in ("off", "0", "false", "no")


def _run_prewarm() -> None:
    """后台预热：预初始化引擎 + 一次轻量只读 FTS 查询。异常静默降级。"""
    try:
        engine = _get_engine()
        engine.search(query=_PREWARM_QUERY, top_k=1, mode="keyword")
        print("[worker] prewarm done (engine initialized)", file=sys.stderr, flush=True)
    except Exception as exc:  # noqa: BLE001 — 预热失败不致命，首请求仍走懒初始化
        print(f"[worker] prewarm degraded: {exc}", file=sys.stderr, flush=True)
    finally:
        global _prewarm_done
        _prewarm_done = True


def _start_prewarm() -> None:
    """按开关启动预热线程；不满足条件则跳过（保持现状）。"""
    if not should_prewarm(os.environ):
        return
    threading.Thread(target=_run_prewarm, daemon=True, name="worker-prewarm").start()


def _get_recorder() -> Any:
    global _session_recorder
    if _session_recorder is None:
        from trinity.session_recorder import ChatSessionRecorder
        _session_recorder = ChatSessionRecorder()
    return _session_recorder


def _retry_on_locked(fn, retries: int = 1, backoff_s: float = 0.5):
    """SQLite 写锁争用快速失败 + 自动重试（2026-08-17 根治 worker 卡死）。

    其他进程突发批量写（benchmark 摄入/维护链）时写锁可能被连续占用，
    短 busy_timeout(3s) 会抛 'database is locked'——这里退避重试一次，
    仍失败抛明确错误（含原因），避免 15s×N 叠加成 60s 工具超时。
    """
    last_err = None
    for attempt in range(retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "locked" not in msg.lower():
                raise
            last_err = exc
            if attempt < retries:
                time.sleep(backoff_s * (attempt + 1))
    raise RuntimeError(
        f"trinity store write lock busy (retried {retries}x): {last_err} — "
        "another process (maintenance/benchmark) holds the SQLite write lock"
    ) from last_err


# ── 主循环看门狗（2026-08-17 修复 worker 卡死）────────────────────
# 场景: worker 主循环顺序处理请求，若某请求被 SQLite 写锁阻塞
# （busy_timeout=15s 的多步写入可叠加 >60s，如维护链/其他会话写库并发），
# 后续请求（含 ping）全部排队超时 → "活着的僵尸 worker"。
# 看门狗检测主循环静默超过 _STALL_TIMEOUT 即 dump 线程栈 + 退出，
# 由 DSH 插件自动重启 worker（自愈）。TRINITY_WORKER_STALL_TIMEOUT 可调。
_STALL_TIMEOUT = float(os.environ.get("TRINITY_WORKER_STALL_TIMEOUT", "90"))
_request_in_flight = False
_request_start = time.time()


def _start_watchdog() -> None:
    """请求处理看门狗：仅当"有请求正在处理且超过 _STALL_TIMEOUT"才退出。

    空闲等待输入（无 in-flight 请求）永不触发——避免插件空闲期 worker
    自退出造成 90s 一次的重启循环。
    """
    try:
        import faulthandler
    except Exception:
        faulthandler = None

    def _watch() -> None:
        while True:
            time.sleep(10)
            global _request_in_flight, _request_start
            if _request_in_flight and time.time() - _request_start > _STALL_TIMEOUT:
                print(
                    f"[worker] request stalled >{_STALL_TIMEOUT}s, "
                    "dumping traceback & exiting (plugin will respawn)",
                    file=sys.stderr, flush=True,
                )
                if faulthandler is not None:
                    try:
                        faulthandler.dump_traceback(file=sys.stderr)
                    except Exception:
                        pass
                os._exit(1)

    threading.Thread(target=_watch, daemon=True, name="worker-stall-watchdog").start()


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
    if not content:
        raise ValueError("content required")
    return _retry_on_locked(lambda: _write_impl(engine, params, content))


def _write_impl(engine, params: dict, content: str) -> dict:
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
    return _retry_on_locked(lambda: engine.update_memory(
        memory_id=params.get("memory_id", ""),
        new_content=params.get("new_content", ""),
    ))


def _delete(params: dict) -> dict:
    engine = _get_engine()
    memory_id = params.get("memory_id", "")
    deleted = _retry_on_locked(lambda: engine.delete_memory(memory_id=memory_id))
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
            r = _retry_on_locked(lambda: engine.ingest(
                content=content,
                role=ev.get("role", "user"),
                importance=ev.get("importance", 0.5),
                tags=ev.get("tags") or [],
                category=ev.get("category", "general"),
                metadata=metadata,
                agent_id=agent_id,
                session_id=session_id,
                postprocess=False,
            ))
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



def _reason(params: dict) -> dict:
    """开放域推理（产品化 RouteReasoner, 2026-08-17）。

    params: query / qtype(可选, 策略路由) / question_date(可选, REL 用) /
            top_k / agent_id / persona_id
    未启用 TRINITY_ROUTE_REASONER 或失败时回退引擎 reason()。
    """
    engine = _get_engine()
    query = params.get("query", "")
    if not query:
        raise ValueError("query required")
    qtype = params.get("qtype")
    qdate = params.get("question_date")
    top_k = int(params.get("top_k", 8))
    return engine.reason(
        query=query, top_k=top_k, qtype=qtype, question_date=qdate,
        agent_id=params.get("agent_id"), persona_id=params.get("persona_id"),
    )


def _rl_feedback(params: dict) -> dict:
    """RL 记忆反馈（MemRL 对齐）：记录用户确认/纠正信号，更新记忆 Q 值。

    冷启动兜底：引擎侧（非聚合池）记忆 ID 也能直接反馈，未注册先注册。
    """
    from trinity.agents import MemoryAggregator, create_aggregator
    agg = create_aggregator(persist=True)
    memory_id = params.get("memory_id", "")
    positive = bool(params.get("positive", True))
    if not memory_id:
        raise ValueError("memory_id required")
    r = agg.rl_feedback(memory_id, positive=positive)
    return {"memory_id": memory_id, "positive": positive, **r}


def _resolve_store_db() -> str:
    """权威库路径解析(2026-08-16,与 core/client.py 一致,替代硬编码)。"""
    env_store = os.environ.get("TRINITY_STORE")
    if env_store:
        if os.path.isdir(env_store):
            return os.path.join(env_store, "trinity_store.db")
        if os.path.isfile(env_store):
            return env_store
    return os.path.expanduser("~/.trinity/store/trinity_store.db")


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
    db = _resolve_store_db()
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
    "rl_feedback": _rl_feedback,
    "reason": _reason,
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
    global _request_in_flight, _request_start
    _start_watchdog()
    _start_prewarm()
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
        # 请求进入处理：看门狗只在该状态超时（>_STALL_TIMEOUT）时判定卡死
        _request_in_flight = True
        _request_start = time.time()
        if handler is None:
            _emit({"id": req_id, "error": {"message": f"unknown method: {method}"}})
            _request_in_flight = False
            continue
        try:
            result = handler(params)
            _emit({"id": req_id, "result": result})
        except Exception as exc:
            _emit({
                "id": req_id,
                "error": {"message": str(exc), "trace": traceback.format_exc()[-2000:]},
            })
        finally:
            _request_in_flight = False
    return 0


if __name__ == "__main__":
    sys.exit(main())
