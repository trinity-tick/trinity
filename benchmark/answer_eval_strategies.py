#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""answer_eval_strategies.py — 生成侧弱项专项 A/B（EXECUTION 458，P1-1）

官方 oracle 500 题 AnswerAcc 基线（2026-09-02 上午锁定）：TR 0.399 / MS 0.391 /
SS-P 0.367（R@1=1.0 全绿——检索满分、生成拖后腿）。本脚本在**同一批题目**上对比
生成策略：
  base : 官方现用提示（对照组）
  tr   : 时序策略——时间线线索显式化（日期正则 + 顺序提示）
  ms   : 跨会话整合策略——会话标注 + 冲突取新
  ssp  : 偏好两段式——先抽取用户偏好，再作答（2 次生成）

判分与 official_lm_eval 同款（normalize 子串 + LLM 语义 judge，judge3 口径可选）。
用法:
  python benchmark/answer_eval_strategies.py --cats tr,ms,ss-p --per 40
输出: .trinity/bench-official/qa_strategy_<ts>.json（每策略 × 每类目 Acc）
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

DSET = os.path.join(ROOT, "benchmark", "data", "longmemeval_oracle.json")
CAT_ALIAS = {"tr": "temporal-reasoning", "ms": "multi-session",
             "ss-p": "single-session-preference", "ss-pref": "single-session-preference"}

DATE_RE = re.compile(r"\b(20\d{2}[-/年]\d{1,2}([-/月]\d{1,2})?|\d{1,2}[-/月]\d{1,2}[-/]20\d{2}|"
                     r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ .]?\d{1,2}(st|nd|rd|th)?(,? ?20\d{2})?)\b", re.I)


def _llm():
    from trinity.daemon.memory_compressor import create_llm_compress_callable
    creds = {}
    path = os.path.expanduser("~/.dsh/.credentials.yaml")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    return create_llm_compress_callable(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=key, model="deepseek-chat", timeout=60)


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def judge(llm, question, gold, model_ans) -> bool:
    question, gold, model_ans = str(question or ""), str(gold or ""), str(model_ans or "")
    if not model_ans.strip():
        return False
    an, gn = normalize(model_ans), normalize(gold)
    if gn and len(gn) >= 4 and gn in an:
        return True
    try:
        out = str(llm(
            "You are a strict fact-checker for a memory benchmark. Given a QUESTION, "
            "the GOLD ANSWER, and the MODEL ANSWER, decide whether the model answer "
            "contains the gold answer's key fact(s) (paraphrase allowed). "
            "Reply with exactly YES or NO.",
            "QUESTION: %s\nGOLD ANSWER: %s\nMODEL ANSWER: %s\n\n"
            "Does the model answer contain the gold answer's key fact? Reply YES or NO."
            % (question[:300], gold[:300], model_ans[:600]))).strip().upper()
        return out.startswith("YES")
    except Exception:
        return False


def gen(llm, sys_p, user_p) -> str:
    try:
        return str(llm(sys_p, user_p)).strip()
    except Exception:
        return ""


def contexts_formatted(hits, strategy):
    out = []
    for i, h in enumerate(hits[:5]):
        c = str(h.get("content") or "")[:600]
        sid = str(h.get("session_id") or "")
        if strategy in ("ms", "ssp") and sid:
            out.append("[%d][session:%s] %s" % (i + 1, sid[:18], c))
        else:
            out.append("[%d] %s" % (i + 1, c))
    return "\n\n".join(out)


def timeline_of(hits):
    lines = []
    for h in hits[:5]:
        for m in DATE_RE.finditer(str(h.get("content") or "")):
            lines.append(m.group(0))
    return "; ".join(dict.fromkeys(lines))[:300]


def answer_question(llm, q, hits, strategy):
    question = str(q.get("question") or "")
    gold = str(q.get("answer") or "")
    ctx = contexts_formatted(hits, strategy)
    base_sys = "You are an AI assistant answering from memory context only. Answer concisely."
    if strategy == "base":
        user = "Question: %s\n\nContext:\n%s\n\nAnswer concisely using ONLY the context. Answer:" % (question, ctx)
        ans = gen(llm, base_sys, user)
    elif strategy == "tr":
        tl = timeline_of(hits)
        user = ("Question: %s\n\nContext:\n%s\n\nDate clues in context: %s\n\n"
                "Note: the conversation spans time. If the question asks when/order, reason from the date clues. "
                "Answer concisely using ONLY the context. Answer:") % (question, ctx, tl or "(none)")
        ans = gen(llm, base_sys + " Pay attention to dates and temporal order.", user)
    elif strategy == "ms":
        user = ("Question: %s\n\nContext (multiple sessions of the same user):\n%s\n\n"
                "Note: integrate facts across sessions; if sessions conflict, trust the later one. "
                "Answer concisely using ONLY the context. Answer:") % (question, ctx)
        ans = gen(llm, base_sys + " You must integrate evidence across sessions.", user)
    elif strategy == "ssp":
        s1 = gen(llm, base_sys,
                 "Context:\n%s\n\nExtract this user's preferences/habits as short bullet points "
                 "(answer style they like, values, routines). If none found reply NONE." % ctx)
        user = ("User preferences: %s\n\nQuestion: %s\n\n"
                "Answer in the way this user would prefer, grounded in the preferences above. Answer:") % (s1[:800], question)
        ans = gen(llm, base_sys, user)
    else:
        raise ValueError(strategy)
    return ans, judge(llm, question, gold, ans)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cats", default="tr,ms,ss-p")
    ap.add_argument("--per", type=int, default=40, help="每类目题目数（对照实验同题）")
    ap.add_argument("--out", default=os.path.expanduser(
        "~/.trinity/bench-official/qa_strategy_%s.json" % time.strftime("%Y%m%d_%H%M%S")))
    args = ap.parse_args()

    data = json.load(open(DSET, encoding="utf-8"))
    qs = data if isinstance(data, list) else (data.get("questions") or [])
    selected = collections_Counter = None
    from collections import Counter as _C
    want = [CAT_ALIAS.get(c.strip().lower(), c.strip().lower()) for c in args.cats.split(",")]
    buckets = {}
    for q in qs:
        t = q.get("question_type", "")
        if t in want:
            buckets.setdefault(t, []).append(q)
    pool = {}
    for t in want:
        pool[t] = buckets.get(t, [])[: args.per]
    print("A/B pool:", {t: len(v) for t, v in pool.items()})

    from trinity.adapters.sqlite import SQLiteAdapter
    llm = _llm()
    strategies = ["base", "tr", "ms", "ssp"]
    results = {s: {} for s in strategies}
    t0 = time.time()
    for t, qlist in pool.items():
        for strategy in strategies:
            acc = 0
            total = 0
            for qi, q in enumerate(qlist):
                tmp = tempfile.mkdtemp(prefix="qa_s_")
                db = os.path.join(tmp, "s.db")
                ad = SQLiteAdapter(db_path=db)
                ad.connect()
                try:
                    records = []
                    sid_list = q.get("haystack_session_ids") or []
                    for idx, msgs in enumerate(q.get("haystack_sessions") or []):
                        real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
                        for m in msgs:
                            content = str(m.get("content") or "") if isinstance(m, dict) else str(m)
                            if content.strip():
                                records.append({"content": content[:2000], "persona_id": "u1",
                                                "session_id": real_sid, "agent_id": "u1", "importance": 0.5})
                    ad.ingest_batch(records)
                    hits = ad.search_memories(query=str(q.get("question") or ""), top_k=10)
                finally:
                    ad.disconnect()
                ans, ok = answer_question(llm, q, hits, strategy)
                acc += 1 if ok else 0
                total += 1
                if qi % 10 == 9:
                    print("  [%s][%s] %d/%d acc=%.3f" % (t, strategy, qi + 1, total, acc / max(1, total)), flush=True)
            results[strategy][t] = round(acc / max(1, total), 4)
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "per_cat": args.per,
           "elapsed_s": round(time.time() - t0, 1), "results": results}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(out, open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print("saved ->", args.out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
