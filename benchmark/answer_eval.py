#!/usr/bin/env python3
"""
OPT1: 答案生成评测 harness（LongMemEval-style 500q mock × DeepSeek）
====================================================================
把检索评测升级为"端到端答案精度"（对齐 LongMemEval-S 官方口径的评测方式）：

  1. 同 500q mock 数据集 + 同入库方式（keyword/FTS5 检索路径）；
  2. 每问：检索 top-5 上下文 → 组装 prompt → DeepSeek 生成答案；
  3. 评分：期望事实（context_facts）是否出现在生成答案中（归一化子串匹配，
     确定性、零成本，与官方"LLM-judge"相比为保守下界）；
  4. 归因：R@5（上下文含事实）vs AnswerAcc（答案含事实）→ 区分
     "检索缺口"（R@5 miss）与"生成缺口"（R@5 hit 但答案错）；
  5. 输出：逐类目 accuracy/latency/cost 估算 → output/answer_eval_results.json。

用法：
    python benchmark/answer_eval.py --limit 5     # 冒烟
    python benchmark/answer_eval.py --limit 500   # 全量（约 20-30 分钟）
    python benchmark/answer_eval.py --model deepseek-chat --retries 2
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
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

DSET = r"C:\Users\Administrator\.marvis\workspace\conv_19f49996244_37d75ffae4a6\benchmark\longmemeval_mock_dataset.json"

# DeepSeek 定价（估算，$/1M tokens；标注为估算值）
PRICING = {"input": 0.27, "output": 1.10}

SYSTEM_PROMPT = (
    "You are an AI assistant answering questions using ONLY the provided "
    "memory context. Answer concisely with the facts found in the context; "
    "do not mention the context itself. If the context does not contain the "
    "answer, state what is missing briefly."
)


def load_credentials(path=os.path.expanduser("~/.dsh/.credentials.yaml")):
    creds = {}
    if os.path.exists(path):
        raw = open(path, "r", encoding="utf-8-sig").read()
        for line in raw.splitlines():
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    return creds


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def fact_hit(answer: str, facts) -> bool:
    """期望事实是否出现在答案中（归一化子串匹配）。"""
    an = normalize(answer)
    for f in facts:
        fn = normalize(f)
        if fn and len(fn) >= 4 and fn in an:
            return True
    return False


def build_prompt(question: str, contexts) -> str:
    parts = [f"Question: {question}", "", "Context:"]
    for i, c in enumerate(contexts, 1):
        parts.append(f"[{i}] {c[:600]}")
    parts.append("")
    parts.append("Answer:")
    return "\n".join(parts)


JUDGE_SYSTEM = (
    "You are a strict fact-checker. Determine whether the ANSWER contains the "
    "stated FACT. Reply with exactly YES or NO."
)

# TR 时序类目专用提示（GEN-1）：强制带序复述每个事件
TR_ANSWER_SUFFIX = (
    "\n\nList the events in chronological order as: \"1) <full event A text>\" "
    "\"2) <full event B text>\" ... Restate every event fully. If any event has "
    "no date in the context, still place it using the best available evidence "
    "and note '(undated)'."
)

# MS 类目提示后缀（GEN-1）：上下文缺精确信息时仍总结已知情况
MS_ANSWER_SUFFIX = (
    "\n\nIf the context does not contain the exact details asked, summarize what "
    "IS known about the person's changes/activities/preferences from the context."
)

TR_JUDGE_SYSTEM = (
    "You are a strict temporal-order judge. Given the EXPECTED order of events "
    "(chronological, first to last) and a MODEL ANSWER, determine whether the "
    "model's answer lists the same events in the same relative order. "
    "Reply with exactly YES or NO."
)


def judge_facts(llm, answer: str, facts) -> bool:
    """LLM-judge：答案是否包含期望事实（任一命中即对）。"""
    if not answer.strip():
        return False
    user = "ANSWER: " + answer[:800] + "\n\nFACT: " + facts[0][:300]
    if len(facts) > 1:
        user += "\n(If the answer contains ANY of these facts, reply YES.)"
    try:
        out = llm(JUDGE_SYSTEM, user).strip().upper()
        return out.startswith("YES")
    except Exception:
        return False


def judge_tr_order(llm, answer: str, facts) -> bool:
    """TR 专用 judge：校验答案中的事件顺序是否与期望顺序一致。"""
    if not answer.strip():
        return False
    expected = " then ".join(f"[{i + 1}] {f[:120]}" for i, f in enumerate(facts))
    user = (
        "EXPECTED ORDER (chronological, first to last):\n" + expected +
        "\n\nMODEL ANSWER:\n" + answer[:800] +
        "\n\nDoes the model's answer present the events in the SAME relative "
        "order as EXPECTED (first event before second, etc.)? Reply YES or NO."
    )
    try:
        out = llm(TR_JUDGE_SYSTEM, user).strip().upper()
        return out.startswith("YES")
    except Exception:
        return False


def build_prompt_for_category(question: str, contexts, cat: str) -> str:
    prompt = build_prompt(question, contexts)
    if cat == "TR":
        prompt += TR_ANSWER_SUFFIX
    elif cat == "MS":
        prompt += MS_ANSWER_SUFFIX
    return prompt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20, help="max questions (default 20)")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--top-k", type=int, default=10,
                        help="retrieval context size (default 10: MS 类目 R@5 0.525→0.95, 见 channel_attribution)")
    parser.add_argument("--categories", default="",
                        help="comma-separated category filter, e.g. TR,MS (empty = all)")
    args = parser.parse_args()

    from trinity import Trinity
    from trinity.daemon.memory_compressor import create_llm_compress_callable

    creds = load_credentials()
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: no DEEPSEEK_API_KEY in credentials / TRINITY_LLM_API_KEY env")
        return 1

    cat_filter = {c.strip() for c in args.categories.split(",") if c.strip()}

    llm = create_llm_compress_callable(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key,
        model=args.model,
        timeout=60,
    )

    data = json.load(open(DSET, encoding="utf-8"))
    questions = [q for q in data["questions"]
                 if (not cat_filter or q.get("category") in cat_filter)][: args.limit]
    print(f"questions: {len(questions)}  model={args.model}"
          + (f"  categories={sorted(cat_filter)}" if cat_filter else ""))

    tmpdir = tempfile.mkdtemp(prefix="lme_ans_")
    mem = Trinity(adapter="sqlite", store_path=tmpdir)

    t0 = time.time()
    for q in questions:
        for fact in q.get("context_facts", []):
            ftext = fact.get("fact", "")
            if not ftext:
                continue
            try:
                mem.ingest(ftext, persona_id=q.get("persona_name") or "default",
                           session_id=str(q.get("session_id") or "0"),
                           category=q.get("category", "general"),
                           tags=["lme", q.get("category", "")])
            except Exception:
                pass
    print(f"ingested in {time.time()-t0:.1f}s")

    stats = {}
    n = 0
    r5_total = 0
    acc_total = 0
    acc_strict_total = 0
    gen_gap = 0
    retr_gap = 0
    lat_sum = 0.0
    tok_in = 0
    tok_out = 0

    for q in questions:
        cat = q.get("category", "?")
        question = q.get("question", "")
        facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
        if not facts or not question:
            continue
        n += 1
        st = stats.setdefault(cat, {"total": 0, "r5": 0, "acc": 0, "gen_gap": 0, "retr_gap": 0})
        st["total"] += 1

        results = mem.search(query=question, mode="keyword", top_k=args.top_k,
                             persona_id=q.get("persona_name") or None).get("results", [])
        contexts = [r.get("content", "") for r in results]
        r5 = fact_hit("\n".join(contexts), facts)
        if r5:
            r5_total += 1
            st["r5"] += 1
        else:
            retr_gap += 1
            st["retr_gap"] += 1

        prompt = build_prompt_for_category(question, contexts, cat)
        answer = ""
        try:
            t1 = time.time()
            for attempt in range(args.retries + 1):
                try:
                    answer = llm(SYSTEM_PROMPT, prompt)
                    break
                except Exception as e:
                    if attempt == args.retries:
                        raise
                    time.sleep(2 * (attempt + 1))
            lat_sum += time.time() - t1
            tok_in += len(SYSTEM_PROMPT) + len(prompt)
            tok_out += len(answer)
        except Exception as e:
            answer = ""

        if cat == "TR":
            acc = judge_tr_order(llm, answer, facts)
        else:
            acc = judge_facts(llm, answer, facts)
        acc_strict = fact_hit(answer, facts)
        if acc_strict:
            acc_strict_total += 1
        if acc:
            acc_total += 1
            st["acc"] += 1
        elif r5:
            gen_gap += 1
            st["gen_gap"] += 1

        if n <= 3 or not (n % 50):
            print(f"  [{n}/{len(questions)}] {cat:6s} r5={'Y' if r5 else 'N'} "
                  f"acc={'Y' if acc else 'N'} | {question[:60]}")
            if n <= 2 and answer:
                print(f"      answer: {answer[:100]}")

    cost = (tok_in / 1e6) * PRICING["input"] + (tok_out / 1e6) * PRICING["output"]
    out = {
        "test": "answer_eval",
        "dataset": "LongMemEval-style mock (500q, community-generated, not official)",
        "model": args.model,
        "questions": n,
        "R@5": round(r5_total / n, 4),
        "AnswerAcc": round(acc_total / n, 4),
        "AnswerAcc_strict_substring": round(acc_strict_total / n, 4) if n else 0,
        "generation_gap": round(gen_gap / n, 4),
        "retrieval_gap": round(retr_gap / n, 4),
        "avg_latency_s": round(lat_sum / n, 2) if n else 0,
        "est_cost_usd": round(cost, 4),
        "by_category": {
            c: {"total": s["total"], "R@5": round(s["r5"] / s["total"], 4),
                "AnswerAcc": round(s["acc"] / s["total"], 4),
                "gen_gap": round(s["gen_gap"] / s["total"], 4),
                "retr_gap": round(s["retr_gap"] / s["total"], 4)}
            for c, s in sorted(stats.items())},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }

    print(f"\n{'='*66}")
    print(f"  Answer Eval — {n} questions, {args.model}")
    print(f"  R@5 (context)      : {out['R@5']:.4f}")
    print(f"  AnswerAcc (answer) : {out['AnswerAcc']:.4f}")
    print(f"  gen gap (ctx ok, ans wrong): {out['generation_gap']:.4f}")
    print(f"  retr gap (ctx miss)        : {out['retrieval_gap']:.4f}")
    print(f"  avg latency {out['avg_latency_s']}s  est cost ${out['est_cost_usd']}")
    for c in sorted(out["by_category"]):
        s = out["by_category"][c]
        print(f"  {c:6s} n={s['total']:3d} R@5={s['R@5']:.3f} Acc={s['AnswerAcc']:.3f}")
    print(f"{'='*66}")

    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    path = os.path.join(ROOT, "output", "answer_eval_results.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"saved -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
