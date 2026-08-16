# -*- coding: utf-8 -*-
"""auto_session_summary.py — 会话结束自动沉淀(结构层事件流 -> session-summary 记忆)

数据源: dsh_events 完整事件流(DSH 结构层,插件实时同步)
触发:   maintenance 链每日/每小时(本脚本幂等,可任意频次运行)
逻辑:   对"已结束"会话(closed/compacted,或超过 12h 无活动的 active 会话,
        且事件数 > 0)生成摘要记忆;已有 auto-summary 的会话跳过。
摘要:   优先 DeepSeek LLM(凭证 DEEPSEEK_API_KEY);失败或无 key 降级抽取式。
落库:   agent_id=dsh-<sid>, session_id=<sid>, category=session,
        tags=[session-auto-summary, session], importance=0.7
"""
from __future__ import annotations
import json, os, sys, time, urllib.request
from datetime import datetime, timezone

DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
INACTIVE_HOURS = float(os.environ.get("SESSION_AUTO_INACTIVE_HOURS", "12"))
MAX_TURNS = 40
MAX_CHARS = 6000

def load_credentials():
    cred_file = os.path.expanduser("~/.dsh/.credentials.yaml")
    key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("TRINITY_LLM_API_KEY")
    if not key and os.path.exists(cred_file):
        with open(cred_file, encoding="utf-8-sig") as fh:  # utf-8-sig 兼容 BOM
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip().strip('"').strip("'")
                if k in ("DEEPSEEK_API_KEY", "TRINITY_LLM_API_KEY"):
                    key = v.strip().strip('"').strip("'")
                    if key:
                        break
    return key

SYSTEM_PROMPT = (
    "You are a session consolidator. Summarize the given DSH agent session "
    "transcript (user requests and assistant replies) into a compact Chinese "
    "session summary that preserves: 1) the task/goal; 2) key decisions and "
    "outcomes (file paths, tool names, exact numbers); 3) pitfalls and reusable "
    "lessons; 4) open questions / next steps. Keep under 200 words, factual, "
    "no preamble, no markdown headings."
)

def llm_summarize(transcript: str, api_key: str) -> str | None:
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps({
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": transcript[:14000]},
                ],
                "temperature": 0.2, "max_tokens": 500,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

def extractive_summarize(transcript: str) -> str:
    # 保头尾:用户目标 + 最近结论
    head = transcript[:2500]
    tail = transcript[-2500:]
    return f"[抽取式摘要]\n--- 会话开头 ---\n{head}\n--- 会话结尾 ---\n{tail}"

def main():
    import sqlite3
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    api_key = load_credentials()
    now = time.time()
    cutoff = now - INACTIVE_HOURS * 3600

    # 1) candidate sessions: ended by status, or inactive long enough
    sessions = conn.execute(
        "SELECT session_id, agent_id, status, title, updated_at FROM dsh_sessions"
    ).fetchall()
    candidates = []
    for s in sessions:
        if s["status"] in ("closed", "compacted"):
            candidates.append(s)
        elif s["status"] == "active" and s["updated_at"] and s["updated_at"] < cutoff:
            n = conn.execute("SELECT COUNT(*) c FROM dsh_events WHERE session_id=?", (s["session_id"],)).fetchone()["c"]
            if n > 0:
                candidates.append(s)

    done = skipped = failed = 0
    for s in candidates:
        sid = s["session_id"]
        aid = s["agent_id"] or f"dsh-{sid}"
        # idempotency: existing auto-summary memory?
        dup = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE agent_id=? AND tags LIKE '%session-auto-summary%'",
            (aid,),
        ).fetchone()["c"]
        if dup:
            skipped += 1
            continue
        # 2) extract transcript from event stream
        rows = conn.execute(
            "SELECT type, payload, seq FROM dsh_events WHERE session_id=? AND type IN ('user/message','assistant/message') ORDER BY seq",
            (sid,),
        ).fetchall()
        if not rows:
            skipped += 1
            continue
        lines = []
        for r in rows[-MAX_TURNS:]:
            try:
                p = json.loads(r["payload"]) if isinstance(r["payload"], str) else (r["payload"] or {})
            except Exception:
                continue
            content = p.get("content") or p.get("text") or ""
            if not content:
                continue
            role = "U" if r["type"] == "user/message" else "A"
            lines.append(f"{role}: {str(content)[:600]}")
        if not lines:
            skipped += 1
            continue
        transcript = "\n".join(lines)[:MAX_CHARS]

        # 3) summarize
        summary = None
        if api_key:
            summary = llm_summarize(transcript, api_key)
        if not summary:
            summary = extractive_summarize(transcript)

        # 4) ingest as memory
        title = s["title"] or sid
        content = f"[会话自动摘要] {datetime.fromtimestamp(s['updated_at'] or now, tz=timezone.utc).isoformat()} 会话:{title}\n{summary}"
        try:
            conn.execute(
                "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content, role, importance, tags, category, status, version, sha256_hash, created_at, updated_at, access_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"summ_auto_{sid[:12]}_{int(now)}", sid, "default", aid, content, "assistant", 0.7,
                 json.dumps(["session-auto-summary", "session"], ensure_ascii=False), "session", "active", 1,
                 "", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), 0),
            )
            conn.commit()
            done += 1
        except Exception as e:
            conn.rollback()
            failed += 1

    conn.close()
    print(f"AUTO-SESSION-SUMMARY: candidates={len(candidates)} done={done} skipped={skipped} failed={failed} llm={'yes' if api_key else 'no(extractive)'}")

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
