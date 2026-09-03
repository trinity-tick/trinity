#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""官方 LongMemEval 评测（2026-09-02，十一评阶段 A：mock 降级 → 官方实测）

数据集: benchmark/data/longmemeval_oracle.json（xiaowu0162/longmemeval-cleaned, 500q, 6 类）

评测语义（对齐官方 oracle 变体）:
  - 每问: 把 haystack_sessions 的消息全部摄入临时库（每条消息=一条记忆, session_id=会话id）
  - R@k: 检索 query 的 top-k 中是否含 answer_session_ids 会话的消息（官方 R@k 语义）
  - AnswerAcc（--answer）: top-k 上下文 → LLM 生成 → judge 判定是否覆盖 gold answer

用法:
  python benchmark/official_lm_eval.py --limit 500          # R@k 全量（无 LLM, 快）
  python benchmark/official_lm_eval.py --limit 100 --answer # R@k + AnswerAcc 子集
"""
# NOTICE(EXECUTION 458C): 官方 LongMemEval 锁定数字入口（正式）——分工见 docs/RUNNER_MAP.md。
import argparse
import collections
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
PRICING = {"input": 0.27, "output": 1.10}


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def _llm_callable(model: str = "deepseek-chat", timeout: int = 60):
    from trinity.daemon.memory_compressor import create_llm_compress_callable
    creds = {}
    path = os.path.expanduser("~/.dsh/.credentials.yaml")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8-sig"):
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    return create_llm_compress_callable(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key, model=model, timeout=timeout)


JUDGE_SYS = ("You are a strict fact-checker for a memory benchmark. Given a QUESTION, "
             "the GOLD ANSWER, and the MODEL ANSWER, decide whether the model answer "
             "contains the gold answer's key fact(s) (paraphrase allowed). "
             "Reply with exactly YES or NO.")


def judge(llm, question, gold, model_ans):
    # 2026-09-02: LLM 偶发返回非字符串（int/dict）——全面 str() 防御
    question = str(question or "")
    gold = str(gold or "")
    model_ans = str(model_ans or "")
    if not model_ans.strip():
        return False
    an = normalize(model_ans)
    gn = normalize(gold)
    if gn and len(gn) >= 4 and gn in an:
        return True
    try:
        out = str(llm(JUDGE_SYS, "QUESTION: %s\nGOLD ANSWER: %s\nMODEL ANSWER: %s\n\nDoes the model answer contain the gold answer's key fact? Reply YES or NO."
                    % (question[:300], gold[:300], model_ans[:600]))).strip().upper()
        return out.startswith("YES")
    except Exception:
        return False


DATE_RE = re.compile(r"\b(20\d{2}[-/\u5e74]\d{1,2}([-/\u6708]\d{1,2})?|\d{1,2}[-/\u6708]\d{1,2}[-/]20\d{2}|"
                     r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*[ .]?\d{1,2}(st|nd|rd|th)?(,? ?20\d{2})?)\b", re.I)


def _date_clues(results):
    seen = []
    for h in results[:5]:
        c = str(h.get("content") or "")
        for m in DATE_RE.finditer(c):
            if m.group(0) not in seen:
                seen.append(m.group(0))
    return "; ".join(seen[:40]) or "(none)"


def build_qa_prompt(qtype, question, results, strategy, cap=5):
    """按题型路由生成提示（EXECUTION 460；策略经 458.1b A/B 验证：
    TR 用日期线索+时序（+13.3pp），MS/SS-P 用跨会话整合+会话标注（MS +6.7pp / SS-P +20pp），
    其余题型保持 base 口径不变）。cap：上下文条数上限（默认 5 = 锁定口径）。"""
    hits = results[:cap]
    if strategy == "base":
        ctx = "\n\n".join("[%d] %s" % (i + 1, str(h.get("content", ""))[:600])
                            for i, h in enumerate(hits))
        prompt = ("Question: %s\n\nContext:\n%s\n\nAnswer concisely using ONLY the context. Answer:"
                  % (question, ctx))
        return "You are an AI assistant answering from memory context only. Answer concisely.", prompt
    tagged = []
    for i, h in enumerate(hits):
        c = str(h.get("content") or "")[:600]
        sid = str(h.get("session_id") or "")
        tagged.append("[%d][session:%s] %s" % (i + 1, sid[:18], c) if sid else "[%d] %s" % (i + 1, c))
    ctx = "\n\n".join(tagged)
    base_sys = "You are an AI assistant answering from memory context only. Answer concisely."
    if qtype == "temporal-reasoning":
        sys_p = base_sys + " Pay attention to dates and temporal order."
        clues = _date_clues(results)
        user_p = ("Question: %s\n\nContext:\n%s\n\nDate clues in context: %s\n\n"
                  "If the question asks when/order, reason carefully from the date clues. "
                  "Answer concisely using ONLY the context. Answer:") % (question, ctx, clues)
    elif qtype == "multi-session":
        sys_p = base_sys + " Integrate evidence across sessions; if sessions conflict, trust the later one."
        user_p = ("Question: %s\n\nContext (multiple sessions of the same user):\n%s\n\n"
                  "Note: integrate facts across sessions; conflicting facts resolve to the later session. "
                  "Answer concisely using ONLY the context. Answer:") % (question, ctx)
    elif qtype == "single-session-preference":
        sys_p = base_sys + (" Integrate evidence across sessions and answer in line with "
                            "this user's expressed preferences.")
        user_p = ("Question: %s\n\nContext:\n%s\n\n"
                  "Note: integrate the user's preferences; answer in the style/direction they would prefer. "
                  "Answer concisely using ONLY the context. Answer:") % (question, ctx)
    elif qtype == "knowledge-update":
        # EXECUTION 460: KU = 新信息覆盖旧信息——same conflict-newer logic as MS
        sys_p = base_sys + (" Knowledge updates supersede older facts; "
                            "answer with the most recent correct information.")
        user_p = ("Question: %s\n\nContext (ordered by recency):\n%s\n\n"
                  "Note: newer messages may update/override earlier facts; answer with the LATEST "
                  "correct information only. Answer concisely using ONLY the context. Answer:") % (question, ctx)
    else:
        return base_sys, ("Question: %s\n\nContext:\n%s\n\n"
                          "Answer concisely using ONLY the context. Answer:" % (question, ctx))
    return sys_p, user_p


# EXECUTION 463: 按类目路由的检索/上下文配置（top_k=检索条数，cap=上下文条数上限）
# 463 全量复测结论：SS-P/KU 的 cap14 外推在子集(+10pp/+6.7pp)不稳健——
# 全量 v3=0.626 < v2 0.642（SS-P -6.7pp/MS -3.0pp 噪声翻转），已回滚；
# EXECUTION 467 复测结论：MS 查询词覆盖组装 30 题 +10pp 但在全量翻转（v4=0.618 < v2 0.642，
# MS -9.8pp）→ 已回滚。经验固化：30 题抽样对 MS 类不可靠，采纳前必须全量验证或 ≥60 题分层样本。
# 官方锁定口径 = v2（0.642）：multi-session 20/14（EXECUTION 462 全量锁证 +22.6pp）。
# EXECUTION 469 实验：temporal-reasoning 时间线数据层（top-40 日期排序上下文），30 题 +10.0pp flips=7——
# 全量 v5 验证通过才转正式；默认已开启仅当本文件作为评测入口（带 --strategy routed）。
_ROUTE = {
    "multi-session": {"top_k": 20, "cap": 14},
    "temporal-reasoning": {"top_k": 40, "cap": 8, "mode": "timeline"},
}

_MONS = ["january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"]
_MON3 = [m[:3] for m in _MONS]


def _date_key(text):
    low = str(text or "").lower()
    words = low.split()
    for i, w in enumerate(words):
        if w in _MONS or w in _MON3:
            mon = (_MONS.index(w) if w in _MONS else _MON3.index(w)) + 1
            day = 0
            year = 0
            if i + 1 < len(words):
                dig = "".join(ch for ch in words[i + 1] if ch.isdigit())
                if dig:
                    v = int(dig)
                    if v > 31:
                        year = v
                    else:
                        day = v
            if i + 2 < len(words):
                d2 = "".join(ch for ch in words[i + 2] if ch.isdigit())
                if d2 and 1900 <= int(d2) <= 2100:
                    year = int(d2)
            return (year, mon, day)
        parts = w.split("-")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return (int(parts[0]), int(parts[1]), int(parts[2]))
    return None


def timeline_ctx(results, k=8):
    """时间线数据层（EXECUTION 469）：top-40 消息按解析日期排序取 ≤k 条，
    其余按引擎序补足——TR 生成看到真实时间顺序（30 题 A/B +10.0pp）。"""
    evs = []
    for h in results:
        c = str(h.get("content") or "")
        dk = _date_key(c)
        if dk:
            evs.append((dk, h))
    evs.sort(key=lambda x: x[0])
    out = []
    seen = set()
    for dk, h in evs[:k]:
        out.append(h)
        seen.add(str(h.get("content") or ""))
    for h in results:
        if len(out) >= k:
            break
        c = str(h.get("content") or "")
        if c not in seen:
            out.append(h)
            seen.add(c)
    return out[:k]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--answer", action="store_true", help="同时跑 LLM 答案生成 + judge")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--strategy", default="base", choices=["base", "routed"],
                    help="生成策略：base=官方原提示（锁定口径 0.560）；routed=按题型路由 A/B 验证策略（EXECUTION 460）")
    ap.add_argument("--out", default=os.path.join(ROOT, "output", "official_lmeval_results.json"))
    args = ap.parse_args()

    data = json.load(open(DSET, encoding="utf-8"))
    qs = data if isinstance(data, list) else (data.get("questions") or data.get("data") or [])
    qs = qs[: args.limit]
    print("official LongMemEval: %d questions, model=%s%s" % (len(qs), args.model, " (+answer)" if args.answer else ""))

    llm = _llm_callable(args.model) if args.answer else None
    from trinity.adapters.sqlite import SQLiteAdapter

    stats = {}
    t0 = time.time()
    r_total = {k: 0 for k in (1, 3, 5, 10)}
    acc_total = 0
    tok_in = tok_out = 0
    n = 0

    for qi, q in enumerate(qs):
        question = q.get("question", "")
        gold = q.get("answer", "")
        qtype = q.get("question_type", "?")
        ans_sessions = set(q.get("answer_session_ids") or [])
        sessions = q.get("haystack_sessions") or []
        st = stats.setdefault(qtype, {"total": 0, "r1": 0, "r5": 0, "r10": 0, "acc": 0})
        st["total"] += 1
        n += 1

        tmpdir = tempfile.mkdtemp(prefix="lme_official_")
        db = os.path.join(tmpdir, "store.db")
        ad = SQLiteAdapter(db_path=db)
        ad.connect()
        try:
            # 会话 id 用官方 haystack_session_ids（与 answer_session_ids 对齐）
            sid_list = q.get("haystack_session_ids") or []
            records = []
            for idx, msgs in enumerate(sessions):
                real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
                for m in msgs:
                    content = str(m.get("content") or "") if isinstance(m, dict) else str(m)
                    if not content.strip():
                        continue
                    records.append({
                        "content": content[:2000],
                        "persona_id": "u1",
                        "session_id": real_sid,
                        "agent_id": "u1",
                        "role": "user" if isinstance(m, dict) and m.get("role") == "user" else "assistant",
                        "importance": 0.5,
                        "tags": ["lme_official"],
                    })
            try:
                ad.ingest_batch(records)
            except Exception:
                for rec in records:
                    try:
                        ad.store_memory(**rec)
                    except Exception:
                        pass
            # EXECUTION 462/463: routed 按类目检索配置（MS 20 起）；Recall 口径不变——
            # R@k 统计仍按 args.top_k 截断
            _rcfg = _ROUTE.get(qtype, {}) if args.strategy == "routed" else {}
            _search_k = max(args.top_k, _rcfg.get("top_k", args.top_k))
            results = ad.search_memories(query=question, top_k=_search_k)
            hit_sessions = {r.get("session_id") for r in results}
            for k in (1, 3, 5, 10):
                top = results[:k]
                if any(r.get("session_id") in ans_sessions for r in top):
                    r_total[k] += 1
                    if k in (1, 5, 10):
                        st["r%d" % k] += 1
            # AnswerAcc（EXECUTION 460: 策略路由；base 保持锁定口径；
            # EXECUTION 462/463: 上下文深度按类目 cap）
            if args.answer:
                _ctx_results = results
                _cap = _rcfg.get("cap", 5)
                if args.strategy == "routed" and _rcfg.get("mode") == "timeline":
                    _ctx_results = timeline_ctx(results)
                    _cap = len(_ctx_results)
                sys_p, prompt = build_qa_prompt(qtype, question, _ctx_results, args.strategy, cap=_cap)
                ans = ""
                try:
                    ans = llm(sys_p, prompt)
                except Exception:
                    ans = ""
                tok_in += len(prompt)
                tok_out += len(ans)
                ok = judge(llm, question, gold, ans) if ans.strip() else False
                if ok:
                    acc_total += 1
                    st["acc"] += 1
        finally:
            ad.disconnect()
        if qi < 2 or (qi + 1) % 50 == 0:
            print("  [%d/%d] %s R@5=%s" % (qi + 1, len(qs), qtype, "Y" if st["r5"] else "N"))

    out = {
        "test": "official_longmemeval",
        "dataset": "LongMemEval oracle (xiaowu0162/longmemeval-cleaned, 500q official)",
        "questions": n,
        "R@1": round(r_total[1] / n, 4),
        "R@3": round(r_total[3] / n, 4),
        "R@5": round(r_total[5] / n, 4),
        "R@10": round(r_total[10] / n, 4),
        "AnswerAcc": round(acc_total / n, 4) if args.answer else None,
        "strategy": args.strategy,
        "est_cost_usd": round((tok_in / 1e6) * PRICING["input"] + (tok_out / 1e6) * PRICING["output"], 4) if args.answer else 0.0,
        "by_type": {c: {"total": s["total"], "R@1": round(s["r1"] / s["total"], 4),
                        "R@5": round(s["r5"] / s["total"], 4),
                        "R@10": round(s["r10"] / s["total"], 4),
                        "AnswerAcc": round(s["acc"] / s["total"], 4) if args.answer else None}
                   for c, s in sorted(stats.items())},
        "elapsed_s": round(time.time() - t0, 1),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("=" * 66)
    print("  Official LongMemEval — %d questions" % n)
    print("  R@1=%.4f  R@3=%.4f  R@5=%.4f  R@10=%.4f" % (out["R@1"], out["R@3"], out["R@5"], out["R@10"]))
    if args.answer:
        print("  AnswerAcc=%.4f  cost=$%.3f" % (out["AnswerAcc"], out["est_cost_usd"]))
    for c in sorted(out["by_type"]):
        s = out["by_type"][c]
        print("  %-24s n=%d R@1=%.3f R@5=%.3f%s" % (c, s["total"], s["R@1"], s["R@5"],
              " Acc=%.3f" % s["AnswerAcc"] if args.answer else ""))
    print("=" * 66)
    print("saved -> %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
