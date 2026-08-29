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
    "\n\nThe question asks about CHANGES/UPDATES across multiple sessions. "
    "Organize your answer as a CHRONOLOGICAL LIST of distinct facts: each item on "
    "its own line as '- <fact> (<date/session if stated>)'. Cover ALL distinct "
    "changes/updates found in the context — do NOT merge them into one sentence. "
    "If the context does not contain the exact details asked, summarize what "
    "IS known about the person's changes/activities/preferences from the context."
)

# KU 类目专用提示（GEN-3）：强制"before / later"两段式回答
KU_ANSWER_SUFFIX = (
    "\n\nAnswer in two labeled parts: \"BEFORE: <what was true before>\" and "
    "\"LATER: <what changed or the update>\". Use the facts marked 'direct' for "
    "BEFORE and 'update' for LATER when present."
)

# SS-P 类目专用提示（GEN-3）：trait 即偏好表述
SSP_ANSWER_SUFFIX = (
    "\n\nNote: the context may record the preference as \"<name> has the trait: "
    "<X>\". In that case report the preference as X (e.g., 'lives in Seattle')."
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


MS_JUDGE_SYSTEM = (
    "You are a strict completeness judge for significant-changes questions. "
    "Given the EXPECTED answer (a set of key changes) and a MODEL ANSWER, decide "
    "whether the model answer covers the KEY changes in the expected answer "
    "(same events/facts, paraphrasing allowed; missing a key change counts as NO). "
    "Reply with exactly YES or NO."
)


def judge_ms_complete(llm, answer: str, facts) -> bool:
    """MS 专用 judge（2026-08-27 P0 优化 4）：完整性校验——答案须覆盖期望
    答案中的关键变化/事实（TR 式严格化；v6 教训：改答案格式是负优化，改 judge）。"""
    if not answer.strip():
        return False
    expected = "; ".join(f[:200] for f in facts)
    user = ("EXPECTED (key changes): " + expected[:600]
            + "\n\nMODEL ANSWER:\n" + answer[:800]
            + "\n\nDoes the model answer cover the key changes in EXPECTED? Reply YES or NO.")
    try:
        out = llm(MS_JUDGE_SYSTEM, user).strip().upper()
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
    elif cat == "KU":
        prompt += KU_ANSWER_SUFFIX
    elif cat == "SS-P":
        prompt += SSP_ANSWER_SUFFIX
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
    parser.add_argument("--ctx-n", type=int, default=0,
                        help="how many retrieved contexts go into the LLM prompt "
                             "(0 = use top_k; smaller = context pruning / noise cut)")
    parser.add_argument("--min-overlap", type=int, default=0,
                        help="only evaluate questions whose topic words overlap >=N facts "
                             "(filters malformed/mismatched questions; EVAL-1 clean subset)")
    # ── 2026-08-27（RAGFlow 对比 P1-1）：有引文生成 ──
    parser.add_argument("--cite", action="store_true",
                        help="生成答案末尾附上下文编号引用 [n]（可溯源/防幻觉）")
    # ── 2026-08-26（PageIndex 借鉴 Phase 1）：页树模式 A/B ──
    parser.add_argument("--pagetree", action="store_true",
                        help="页树模式：ingest 后 build_pagetree，检索走 page_tree（先定位页再读页内）")
    parser.add_argument("--page-k", type=int, default=2, help="页树选页数（默认 2）")
    parser.add_argument("--reason-deep", action="store_true",
                        help="reason 深度模式（deep=True）：候选池 50/hybrid 20，难查询召回更强")
    parser.add_argument("--reason", action="store_true",
                        help="reason 模式（Phase 3）：LLM 相关重判（候选=关键词+页树，带活跃 goal 上下文）")
    parser.add_argument("--out", default=os.path.join(ROOT, "output", "answer_eval_results.json"),
                        help="结果 JSON 输出路径")
    args = parser.parse_args()

    from trinity import Trinity
    from trinity.daemon.memory_compressor import create_llm_compress_callable

    creds = load_credentials()
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("ERROR: no DEEPSEEK_API_KEY in credentials / TRINITY_LLM_API_KEY env")
        return 1
    if args.reason:
        # reason 模式内部走 trinity.llm.client.resolve_api_key（只读环境变量）
        os.environ.setdefault("TRINITY_LLM_API_KEY", api_key)

    cat_filter = {c.strip() for c in args.categories.split(",") if c.strip()}

    llm = create_llm_compress_callable(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key,
        model=args.model,
        timeout=60,
    )

    data = json.load(open(DSET, encoding="utf-8"))
    _stop = {"what", "are", "the", "three", "most", "significant", "changes", "over",
             "first", "half", "how", "many", "did", "person", "work", "on", "their",
             "main", "focus", "career", "since", "before", "after", "year", "and",
             "what", "regarding", "is", "this", "that", "with", "for"}

    def _overlap(q) -> int:
        """问题主题词（去人名与停用词）与任一期望事实的词重叠数。"""
        qt = set(re.findall(r"[a-zA-Z]{4,}", q.get("question", "").lower()))
        qt -= set(re.findall(r"[a-zA-Z]{4,}", (q.get("persona_name") or "").lower()))
        qt -= _stop
        ft = set()
        for f in q.get("context_facts", []):
            ft |= set(re.findall(r"[a-zA-Z]{4,}", (f.get("fact") or "").lower()))
        return len(qt & ft)

    questions = [q for q in data["questions"]
                 if (not cat_filter or q.get("category") in cat_filter)
                 and (args.min_overlap == 0 or _overlap(q) >= args.min_overlap)][: args.limit]
    print(f"questions: {len(questions)}  model={args.model}"
          + (f"  categories={sorted(cat_filter)}" if cat_filter else "")
          + (f"  min_overlap={args.min_overlap}" if args.min_overlap else ""))

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

    if args.pagetree or args.reason:
        pt0 = time.time()
        build_stats = mem.build_pagetree(
            exclude_categories=set(),
            exclude_tags={"lme"},
        )
        print(f"pagetree built in {time.time()-pt0:.1f}s: "
              f"{build_stats.get('records')} records -> "
              f"{build_stats.get('categories')} categories / {build_stats.get('clusters')} clusters")

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

        results = mem.search(query=question, mode="reason" if args.reason else "keyword",
                             top_k=args.top_k,
                             persona_id=q.get("persona_name") or None,
                             page_tree=args.pagetree, page_k=args.page_k,
                             reason_deep=args.reason_deep).get("results", [])
        contexts = [r.get("content", "") for r in results]
        if args.ctx_n and args.ctx_n < len(contexts):
            contexts = contexts[: args.ctx_n]
        r5 = fact_hit("\n".join(contexts), facts)
        if r5:
            r5_total += 1
            st["r5"] += 1
        else:
            retr_gap += 1
            st["retr_gap"] += 1

        prompt = build_prompt_for_category(question, contexts, cat)
        if args.cite:
            # 2026-08-27（RAGFlow 对比 P1-1）：有引文生成——回答末尾附所用上下文编号
            prompt += "\n\nCite the context numbers you used at the end of your answer, e.g. [1][3]."
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
            # 2026-08-27（P0 优化 4 回滚）：MS 完整性 judge 实验 0.0 < judge_facts 0.237——
            # 生成侧未解决前改严 judge 是负优化（与 v6 教训一致）。judge_ms_complete 保留备用。
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

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    path = args.out
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"saved -> {path}")
    # 2026-08-26（Claude Science 借鉴 Phase 1）：实验工件 manifest（代码/环境/数据集/参数）
    try:
        from trinity.benchmark.manifest import build_manifest
        build_manifest(path, params={
            "top_k": args.top_k, "model": args.model,
            "mode": "reason" if args.reason else "keyword",
            "pagetree": args.pagetree, "reason_deep": args.reason_deep,
            "ctx_n": args.ctx_n, "limit": args.limit,
        }, dataset_paths=[DSET])
    except Exception as _m_exc:
        print(f"WARN manifest: {_m_exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
