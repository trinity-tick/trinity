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
import json, os, sys, time, urllib.request, hashlib
from datetime import datetime, timezone
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 独立脚本可能没有 trinity 路径：退回原静默行为
    def swallow(*_a, **_k):
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(*_a, **_k)
        except Exception:
            return None

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

# 2026-09-09（闭环执行 A2）：会话经验三段式蒸馏——摘要只"存得住"，经验要
# "用得上"（下次同类任务可注入）。输出直接落 procedural 层供意图检索命中。
EXPERIENCE_SYSTEM_PROMPT = (
    "You are an experience distiller. From the given DSH session summary, "
    "extract AT MOST 3 reusable experiences, each in EXACTLY three lines: "
    "结论: <what worked / the outcome>; 坑: <pitfall or mistake>; "
    "下次策略: <concrete next-time action>. If an experience has no "
    "pitfall, write 坑: 无. Output plain text lines prefixed by - . "
    "Keep every line under 80 chars, factual Chinese, no markdown headings."
)

def llm_summarize(transcript: str, api_key: str, extra: str = "") -> str | None:
    try:
        user = transcript[:14000]
        if extra:
            user = user + "\n\n" + extra
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps({
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user},
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


def llm_distill_experience(summary: str, api_key: str) -> str | None:
    """从会话摘要蒸馏 ≤3 条三段式经验（结论/坑/下次策略）。失败静默。"""
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=json.dumps({
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": EXPERIENCE_SYSTEM_PROMPT},
                    {"role": "user", "content": (summary or "")[:6000]},
                ],
                "temperature": 0.1, "max_tokens": 400,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = data["choices"][0]["message"]["content"].strip()
        # 无实质经验（全是"无"）时丢弃
        if out and not all(x in ("无", "无。", "") for x in (out or "").splitlines() if x.strip()):
            return out
        return None
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
    exp_written = 0  # 2026-09-09 A2：经验蒸馏独立计数
    # 2026-09-10（658.31 饥饿修复）：**先去重，再截断**——原顺序（先按上限截断、
    # 循环内才做 dup 判定）使 20 条上限全部消耗在已摘要会话上：实测
    # candidates=311、待处理 40、FIRST20_ALL_DUP=True、每轮 done=0/skipped=20，
    # 40 个会话永远排不进来（"session-auto 恒空转"的真因）。
    starvation_skipped = 0
    try:
        _pre = []
        for _s in candidates:
            _dup = conn.execute(
                "SELECT COUNT(*) c FROM memories WHERE session_id=? AND tags LIKE '%session-auto-summary%'",
                (_s["session_id"],)).fetchone()["c"]
            if _dup:
                starvation_skipped += 1
            else:
                _pre.append(_s)
        candidates = _pre
    except Exception as _e:
        swallow(__name__, _e)
    # 2026-09-07：安全阀/试跑（维护链高频调用 + 修复后可能一次性补大量摘要）
    _max = int(os.environ.get("SESSION_AUTO_MAX", "0") or 0)
    if _max > 0:
        candidates = candidates[:_max]
    dry_run = os.environ.get("SESSION_AUTO_DRYRUN", "1") == "1"
    # 2026-09-19（体检 839，挂死修复）：
    # ① 进度日志——本任务原先**只在最后打印一行**，挂死时维护日志里什么都没有，
    #    "卡在哪一步"完全不可见（实测 09-19 02:28/06:28/09:58 三次被看门狗杀时零输出）。
    # ② 总时长预算——本地臂/Ollama 抖动时单候选可吃掉整条链的 1800s 包装器预算；
    #    这里给自己设一个**有界**墙钟（默认 1200s，留 600s 给同链其它任务），
    #    到点就停止接纳新候选并如实打印 remaining，而不是被整链杀掉（半完成且无日志）。
    try:
        _deadline_sec = int(os.environ.get("SESSION_AUTO_DEADLINE_SEC", "1200") or 0)
    except Exception:
        _deadline_sec = 1200
    _t_start = time.time()
    _deadline = (_t_start + _deadline_sec) if _deadline_sec > 0 else None
    print(f"[{datetime.now().strftime('%H:%M:%S')}] AUTO-SESSION-SUMMARY start: candidates={len(candidates)} "
          f"dry_run={dry_run} llm={'yes' if api_key else 'no'} deadline={_deadline_sec}s", flush=True)
    deferred = 0
    for s in candidates:
        if _deadline is not None and time.time() >= _deadline:
            deferred = len(candidates) - done - skipped - failed - deferred
            print(f"[{datetime.now().strftime('%H:%M:%S')}] deadline {_deadline_sec}s reached — "
                  f"deferring remaining candidates to next run", flush=True)
            break
        sid = s["session_id"]
        aid = s["agent_id"] or f"dsh-{sid}"
        # idempotency: existing auto-summary for THIS session?
        # 2026-09-07 修复：原按 agent_id 判重——同 agent 任一会话有摘要即跳过该
        # agent 全部候选会话（曾现 candidates=270 done=0 skipped=270 全跳过，
        # 大量已结束会话从未沉淀）。摘要落库带 session_id=<sid>（见下方 INSERT），
        # 改为按会话精确判重，语义与脚本头注释一致。
        dup = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE session_id=? AND tags LIKE '%session-auto-summary%'",
            (sid,),
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
        # A 清单任务 1（EXECUTION 618，590 local_reasoner 接线）：本地臂草稿 + DS 复核终稿。
        # 启用条件双 env：TRINITY_LOCAL_REASONER_USE=on(本地模型) 且
        # TRINITY_SUMMARY_LOCAL_DRAFT=on——route_reason("summarize_facts") 产出草稿，
        # DS 复核时把草稿作为"初稿参考"注入（终稿仍 DS——local_reasoner 红线：
        # 关键输出走 DS）；DS 失败时草稿兜底。默认全关 = 行为与现状完全一致。
        summary = None
        local_draft = ""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] candidate {sid[:16]} status={s['status']} "
              f"chars={len(transcript)}", flush=True)
        if (os.environ.get("TRINITY_SUMMARY_LOCAL_DRAFT", "off").strip().lower() == "on"
                and os.environ.get("TRINITY_LOCAL_REASONER_USE", "").strip() not in ("", "off")):
            # 最后一道保险（独立于 ps1 的 env）：本地草稿**必须有界**。
            # local_reasoner.gen_timeout() 默认 900s × best_of=2 串行 = 1800s = 整条链的预算，
            # 实测 2026-09-19 冻结 20+ 分钟。草稿只是参考（DS 才是终稿），故限 60s。
            try:
                _cap = min(60, int(os.environ.get("TRINITY_LOCAL_REASONER_TIMEOUT", "900") or 900))
            except Exception:
                _cap = 60
            os.environ["TRINITY_LOCAL_REASONER_TIMEOUT"] = str(_cap)
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
                    os.path.abspath(__file__))), "trinity"))
                from trinity.brain.local_reasoner import route_reason
                _t_local = time.time()
                print(f"[{datetime.now().strftime('%H:%M:%S')}] local draft -> ollama "
                      f"(per-call {_cap}s, best_of=2)", flush=True)
                local_draft = route_reason(
                    "summarize_facts",
                    "把以下 DSH 会话提炼为结构化中文长记忆摘要草稿（事实/决策/偏好/坑，简洁）",
                    transcript[:3000]) or ""
                print(f"[{datetime.now().strftime('%H:%M:%S')}] local draft done in "
                      f"{time.time() - _t_local:.1f}s len={len(local_draft)}", flush=True)
            except Exception:
                local_draft = ""
        if api_key:
            if local_draft:
                summary = llm_summarize(
                    transcript, api_key,
                    extra="以下是本地模型初稿，供参考与事实核对；请复核修正后输出终稿（勿照抄错误）。\n初稿：\n"
                          + local_draft[:1500])
            else:
                summary = llm_summarize(transcript, api_key)
        if not summary:
            summary = local_draft or extractive_summarize(transcript)

        # 4.5) 经验蒸馏（闭环执行 A2）：三段式结论/坑/下次策略 → procedural 层。
        # 幂等（按会话判重）；仅 api_key 且轮次足够时花一次小调用；失败静默。
        exp_text = None
        if (os.environ.get("SESSION_AUTO_EXPERIENCE", "1").strip().lower()
                not in ("0", "off", "false")):
            try:
                exp_dup = conn.execute(
                    "SELECT COUNT(*) c FROM memories WHERE session_id=? AND tags LIKE '%experience%'",
                    (sid,),
                ).fetchone()["c"]
                if exp_dup:
                    exp_text = "__skip__"
                elif api_key and len(lines) >= 8:
                    exp_text = llm_distill_experience(summary, api_key)
            except Exception:
                exp_text = None
        if exp_text and exp_text != "__skip__":
            exp_content = ("[experience] {0} 会话:{1}\n{2}".format(
                datetime.fromtimestamp(s["updated_at"] or now, tz=timezone.utc).isoformat(),
                (s["title"] or sid), exp_text))  # 658.31: title 在此处尚未赋值（原引用致 UnboundLocalError）
            if dry_run:
                print(f"  [dry-exp] {sid[:16]} -> would write experience ({len(exp_text)} chars)")
            else:
                try:
                    # 2026-10-05（Step 1 根因修复）：原先 `sha256_hash` 传空串、
                    # 且**没有 content_hash 列** ⇒ 这些行无法参与
                    # `(persona_id, agent_id, content_hash)` 幂等去重、也无法复算 provenance。
                    # 与 `trinity/engine_worker.py` 的同名路径是同一个 bug（两处都要修）。
                    _ch = hashlib.sha256((exp_content or "").encode("utf-8")).hexdigest()
                    conn.execute(
                        "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content, role, importance, tags, category, status, version, sha256_hash, content_hash, created_at, updated_at, access_count) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (f"exp_{sid[:12]}_{int(now)}", sid, "default", aid, exp_content, "assistant", 0.6,
                         json.dumps(["experience", "session-auto"], ensure_ascii=False), "procedural", "active", 1,
                         _ch, _ch, datetime.now(timezone.utc).isoformat(),
                         datetime.now(timezone.utc).isoformat(), 0),
                    )
                    conn.commit()
                    exp_written += 1
                    print(f"  [exp] {sid[:16]} experience written")
                except Exception:
                    conn.rollback()

        # 4) ingest as memory
        if dry_run:
            print(f"  [dry] {sid[:16]} ({s['status']}) -> would summarize "
                  f"({len(transcript)} chars, {len(lines)} turns)")
            done += 1
            continue
        title = s["title"] or sid
        content = f"[会话自动摘要] {datetime.fromtimestamp(s['updated_at'] or now, tz=timezone.utc).isoformat()} 会话:{title}\n{summary}"
        try:
            _ch = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
            conn.execute(
                "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content, role, importance, tags, category, status, version, sha256_hash, content_hash, created_at, updated_at, access_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"summ_auto_{sid[:12]}_{int(now)}", sid, "default", aid, content, "assistant", 0.7,
                 json.dumps(["session-auto-summary", "session"], ensure_ascii=False), "session", "active", 1,
                 _ch, _ch, datetime.now(timezone.utc).isoformat(),
                 datetime.now(timezone.utc).isoformat(), 0),
            )
            conn.commit()
            done += 1
        except Exception as e:
            conn.rollback()
            failed += 1

    conn.close()
    print(f"AUTO-SESSION-SUMMARY: candidates={len(candidates)} done={done} exp_written={exp_written} "
          f"skipped={skipped} failed={failed} deferred={deferred} "
          f"elapsed={time.time() - _t_start:.0f}s llm={'yes' if api_key else 'no(extractive)'}")

if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    main()
