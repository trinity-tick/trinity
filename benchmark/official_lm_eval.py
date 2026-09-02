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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--answer", action="store_true", help="同时跑 LLM 答案生成 + judge")
    ap.add_argument("--model", default="deepseek-chat")
    ap.add_argument("--top-k", type=int, default=10)
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
            results = ad.search_memories(query=question, top_k=args.top_k)
            hit_sessions = {r.get("session_id") for r in results}
            for k in (1, 3, 5, 10):
                top = results[:k]
                if any(r.get("session_id") in ans_sessions for r in top):
                    r_total[k] += 1
                    if k in (1, 5, 10):
                        st["r%d" % k] += 1
            # AnswerAcc
            if args.answer:
                contexts = [r.get("content", "") for r in results[:5]]
                prompt = ("Question: %s\n\nContext:\n%s\n\nAnswer concisely using ONLY the context. Answer:"
                          % (question, "\n\n".join("[%d] %s" % (i + 1, c[:600]) for i, c in enumerate(contexts))))
                ans = ""
                try:
                    ans = llm("You are an AI assistant answering from memory context only. Answer concisely.", prompt)
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
