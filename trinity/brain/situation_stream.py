# -*- coding: utf-8 -*-
"""trinity/brain/situation_stream.py — 情境持续上下文流（EXECUTION 457，大脑化优化）

体检结论（2026-09-02）：意识蓝图"情境感知（situatedness）"仅 6/10——情境
是"按查询现算"（_build_auto_situation 冷启动），没有持续在线的上下文流。

本模块把情境升级为**持续流**：
- refresh()：聚合系统当下信号（时间/今日活动/近期感知/好奇焦点/全局自我/
  库规模/自省条数），产出精简"当下摘要"，双写持久化：
  ① ~/.trinity/state/situation_stream.json（本进程快读）
  ② PG session_context id='ctx:brain'（跨进程共享、带 updated_at 新鲜度）
- get_stream()：TTL 惰性刷新——检索路径首次发现过期时自动补一次刷新，
  使"当下"在无人值守时也持续向前滚动（大脑的默认状态网络）。
- 检索接入：core/client/_search.py._build_auto_situation 注入摘要；
  意识蓝图 criterion-1 据此给出可验证评分（不再只看 ctx 行数）。
"""
import os
import time
import json
import threading

_STATE = os.path.expanduser("~/.trinity/state/situation_stream.json")
_LOCK = threading.Lock()
_MAX_PARTS = 6


def _pg():
    """连接 PG（与其他 brain 模块同款：env 可覆盖，默认 trinity/trinity）。"""
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
        connect_timeout=5,
    )


def _gather():
    """聚合当下信号。任何单项失败都不阻断整体（各 try 独立）。"""
    import datetime
    parts = []
    counts = {}
    now = datetime.datetime.now()
    counts["hour_label"] = now.strftime("%m-%d %a %H:%M")
    try:
        conn = _pg()
        cur = conn.cursor()
        # 今日(24h)活动
        cur.execute(
            "SELECT action, count(*) FROM audit_log "
            "WHERE timestamp::timestamptz > now() - interval '24 hours' "
            "AND action IN ('create','search','search_hybrid') GROUP BY action")
        for action, n in cur.fetchall():
            counts[action] = int(n)
        # 新鲜会话上下文数（24h 内有更新的 ctx 行）
        cur.execute("SELECT count(DISTINCT id) FROM session_context "
                    "WHERE updated_at > now() - interval '24 hours'")
        counts["ctx_fresh_24h"] = int(cur.fetchone()[0] or 0)
        # 近期感知（perceptions 表，最多 3 条；滤错误噪音/密文）
        perc = []
        cur.execute("SELECT channel, signal FROM perceptions "
                    "WHERE detected_at > now() - interval '24 hours' "
                    "ORDER BY detected_at DESC LIMIT 6")
        for ch, sig in cur.fetchall():
            _s = str(sig or "").strip()
            if not _s or _s.lower().startswith("error") or "fserror" in _s[:80].lower() \
                    or _s.startswith("enc:v1:"):
                continue
            perc.append(f"{ch}:{_s[:44]}")
            if len(perc) >= 3:
                break
        counts["perceptions_24h"] = len(perc)
        if perc:
            parts.append("近期感知 " + " / ".join(perc))
        # 全局自我（最新 self-identity；解密失败/密文则放弃该段，不留垃圾）
        cur.execute("SELECT content FROM memories WHERE category='self-identity' "
                    "ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        if row and row[0]:
            _idn = str(row[0]).strip()
            if _idn.startswith("enc:v1:"):
                try:
                    from trinity.security.crypto import decrypt_content as _dc
                    _idn = str(_dc(_idn) or "").strip()
                except Exception:
                    _idn = ""
            if _idn.startswith("enc:v1:"):
                _idn = ""
            if _idn:
                counts["self_identity"] = _idn[:90]
        # 库规模
        cur.execute("SELECT count(*) FROM memories WHERE status='active'")
        counts["mem_active"] = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT count(*) FROM dcpm_beliefs")
        counts["dcpm_beliefs"] = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT count(*) FROM entities")
        counts["entities"] = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT count(*) FROM relations")
        counts["relations"] = int(cur.fetchone()[0] or 0)
        cur.execute("SELECT count(*) FROM memories WHERE category='self-reflection'")
        counts["reflections"] = int(cur.fetchone()[0] or 0)
        conn.close()
    except Exception:
        pass
    # 好奇焦点（active perception 关注方向）
    try:
        with open(os.path.expanduser("~/.trinity/perception_focus.json"),
                  "r", encoding="utf-8") as f:
            focus = json.load(f)
        topics = focus.get("focus_topics") or []
        if topics:
            parts.append("好奇焦点 " + ",".join(str(t)[:20] for t in topics[:_MAX_PARTS]))
    except Exception:
        pass
    # 自省/规模感
    if counts.get("reflections"):
        parts.append(f"自省 {counts['reflections']} 条")
    if counts.get("mem_active"):
        parts.append(f"活跃记忆 {counts['mem_active']} 条")
    return {"counts": counts, "parts": parts, "ts": now.isoformat(timespec="seconds")}


def _compose(g: dict) -> str:
    """拼当下摘要（紧凑，供检索情境前 200 字符截断使用）。"""
    c = g["counts"]
    head = f"当下 {c.get('hour_label','')}"
    act = []
    if c.get("create"):
        act.append(f"写入 {c['create']}")
    if c.get("search"):
        act.append(f"检索 {c['search']}")
    if c.get("ctx_fresh_24h"):
        act.append(f"活跃会话 {c['ctx_fresh_24h']}")
    line = head + (" · " + " ".join(act) if act else "")
    if c.get("self_identity"):
        line += " | 我:" + str(c["self_identity"])[:60]
    parts = [line] + g.get("parts", [])
    return " ；".join(parts)[:300]


def refresh(force: bool = False) -> dict:
    """刷新情境流快照（进程锁防并发双写；返回摘要 dict）。"""
    with _LOCK:
        try:
            if not force:
                st = _read_state()
                if st and (time.time() - st.get("ts_epoch", 0)) < 300:
                    return {"ok": True, "cached": True, "summary": st.get("summary", "")}
            g = _gather()
            summary = _compose(g)
            st = {"ts": g["ts"], "ts_epoch": time.time(), "summary": summary,
                  "counts": g["counts"], "parts": g["parts"]}
            try:
                os.makedirs(os.path.dirname(_STATE), exist_ok=True)
                with open(_STATE, "w", encoding="utf-8") as f:
                    json.dump(st, f, ensure_ascii=False, indent=1)
            except Exception:
                pass
            try:
                conn = _pg()
                cur = conn.cursor()
                import json as _j
                cur.execute(
                    "INSERT INTO session_context (id, last_query, percepts, affect, wm) "
                    "VALUES ('ctx:brain', %s, %s, NULL, NULL) "
                    "ON CONFLICT (id) DO UPDATE SET last_query=EXCLUDED.last_query, "
                    "percepts=EXCLUDED.percepts, updated_at=NOW()",
                    (summary, _j.dumps(g["parts"], ensure_ascii=False)))
                conn.commit()
                conn.close()
            except Exception:
                pass
            return {"ok": True, "cached": False, "summary": summary}
        except Exception as e:
            return {"ok": False, "error": str(e)[:120]}


def _read_state():
    try:
        with open(_STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("ts_epoch", time.time())
        return st
    except Exception:
        return None


def get_stream(max_age_sec: int = 600, allow_refresh: bool = True) -> str:
    """读当前"当下摘要"（新鲜则直接返回；过期且允许则惰性刷新一次）。

    供检索路径调用——首次过期触发刷新使情境流持续向前滚动。
    """
    st = _read_state()
    if st and (time.time() - st.get("ts_epoch", 0)) <= max_age_sec:
        return st.get("summary", "") or ""
    if allow_refresh:
        r = refresh()
        if r.get("ok") and r.get("summary"):
            return r["summary"]
    return st.get("summary", "") if st else ""


def get_counts() -> dict:
    """最近一次快照的计数（无则空 dict）。"""
    st = _read_state()
    return (st or {}).get("counts", {}) or {}


if __name__ == "__main__":
    import sys
    r = refresh(force=True)
    print(json.dumps(r, ensure_ascii=False, indent=1)[:800])
