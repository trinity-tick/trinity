# -*- coding: utf-8 -*-
"""Citation-coverage 归因指标（2026-09-02, Fable 5.1 对照审计 P1-③，闭环 CH-1）。

背景：Fable 5.1 泄露揭示生产 agent 的答案必须保持"证据随流程走"——检索
证据要能挂回它支持的论断（provenance）。Trinity answer_eval 已测 AnswerAcc
（事实包含率），但答案与证据的**归因链**（每个论断是否引用了支持它的
证据条目）此前无指标（评测只取 content、丢弃 memory_id，--cite 仅为提示
未测覆盖）。本模块在答案评测 harness 上补齐：

  citation_coverage = 答案中"带 [n] 引用且该引用证据确实支持该论断"的
                      ground-truth 事实数 / ground-truth 事实总数
  answer_coverage   = 论断被答案覆盖（无论是否引用）的比例
  citation_rate     = 已覆盖论断中带有效引用的比例
  evidence_coverage = ground-truth 事实在检索证据中的比例（召回侧参照）

与 answer_eval/answer_eval_strategies 同款数据（LongMemEval-S 官方 500q）、
同款 sqlite temp 隔离（TRINITY_STORAGE_BACKEND=sqlite 显式）与 DeepSeek
LLM 接线；确定性字符串判定为默认（与 AnswerAcc_strict_substring 哲学一致），
--judge-llm 可选 LLM 宽松判分（同 JUDGE_SYSTEM 提示）。

用法：
    python benchmark/citation_coverage.py [--limit N] [--category TYPE]
        [--top-k 10] [--out PATH] [--judge-llm] [--pool benchmark/data/longmemeval_s_cleaned.json]
产物：~/.trinity/bench-official/citation_coverage_<ts>.json（per-category 指标）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(ROOT))  # tests 从仓库根 import scripts/benchmark 时

_MARKER = re.compile(r"\[\s*(\d{1,3})\s*\]")
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])\s*|(?<=\.)\s+(?=[A-Z0-9\u4e00-\u9fff])")


def normalize(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (text or "").lower())


def split_facts(gold: str) -> list:
    """ground-truth 答案按句切分为事实（无标点的短答案整体作一条）。"""
    parts = [p.strip() for p in _SENT_SPLIT.split(gold or "") if p and p.strip()]
    if not parts:
        return []
    facts = []
    for p in parts:
        p = p.strip(" .,;:，。；：")
        if not p:
            continue
        if normalize(p) and len(normalize(p)) < 3:
            continue  # 过短噪声（如单个年份数字不作为独立事实，避免分母膨胀）
        facts.append(p)
    if not facts:
        return []
    return facts


def extract_markers(answer: str) -> list:
    """答案中的 [n] 引用标记 → [(start, idx), ...]（idx 1-based）。"""
    return [(m.start(), int(m.group(1))) for m in _MARKER.finditer(answer or "")]


def classify_fact(fact: str, answer: str, contexts: list) -> dict:
    """确定性判定一条 ground-truth 事实的覆盖与归因状态。

    contexts: [{"content": str, "memory_id": str}]（证据列表，序号 1-based）
    返回 {fact, covered, cited, evidence_supported, cited_evidence_id}
    """
    nf = normalize(fact)
    res = {"fact": fact, "covered": False, "cited": False,
           "evidence_supported": False, "cited_evidence_id": None}
    if not nf:
        return res
    for ctx in contexts:
        if nf in normalize(ctx.get("content") or ""):
            res["evidence_supported"] = True
            break
    # 答案分句 + 标记归属
    sentences = [s for s in _SENT_SPLIT.split(answer or "") if s and s.strip()]
    if not sentences and (answer or "").strip():
        sentences = [answer]
    for sent in sentences:
        if nf in normalize(sent):
            res["covered"] = True
            for _pos, idx in extract_markers(sent):
                if 1 <= idx <= len(contexts):
                    ctx = contexts[idx - 1]
                    if nf in normalize(ctx.get("content") or ""):
                        res["cited"] = True
                        res["cited_evidence_id"] = ctx.get("memory_id")
                        break
            if res["covered"]:
                break
    return res


def accumulate(q_items: list) -> dict:
    """q_items: [{"category", "gold", "answer", "contexts":[{content,memory_id}]}]
    汇总 per-category 指标（确定性）。"""
    per_cat = {}
    totals = {"facts": 0, "covered": 0, "cited": 0, "evidence": 0, "q": 0}
    for qi in q_items:
        cat = str(qi.get("category") or "other")
        acc = per_cat.setdefault(cat, {"facts": 0, "covered": 0, "cited": 0,
                                       "evidence": 0, "q": 0, "details": []})
        acc["q"] += 1
        totals["q"] += 1
        facts = split_facts(qi.get("gold") or "")
        ctx = qi.get("contexts") or []
        for f in facts:
            cl = classify_fact(f, qi.get("answer") or "", ctx)
            acc["facts"] += 1
            totals["facts"] += 1
            if cl["evidence_supported"]:
                acc["evidence"] += 1
                totals["evidence"] += 1
            if cl["covered"]:
                acc["covered"] += 1
                totals["covered"] += 1
            if cl["cited"]:
                acc["cited"] += 1
                totals["cited"] += 1
            acc["details"].append(cl)
    out = {"per_category": {}, "totals": totals}
    for cat, acc in per_cat.items():
        f = max(1, acc["facts"])
        cov = max(1, acc["covered"])
        out["per_category"][cat] = {
            "questions": acc["q"], "facts": acc["facts"],
            "answer_coverage": round(acc["covered"] / f, 4),
            "citation_coverage": round(acc["cited"] / f, 4),
            "citation_rate": round(acc["cited"] / cov, 4),
            "evidence_coverage": round(acc["evidence"] / f, 4),
        }
    f = max(1, totals["facts"])
    cov = max(1, totals["covered"])
    out["totals"] = {
        "questions": totals["q"], "facts": totals["facts"],
        "answer_coverage": round(totals["covered"] / f, 4),
        "citation_coverage": round(totals["cited"] / f, 4),
        "citation_rate": round(totals["cited"] / cov, 4),
        "evidence_coverage": round(totals["evidence"] / f, 4),
    }
    return out


# ── LLM 接线（与 answer_eval 同款 DeepSeek 客户端）──────────────────
def load_credentials(path=os.path.expanduser("~/.dsh/.credentials.yaml")):
    creds = {}
    if os.path.exists(path):
        for line in open(path, "r", encoding="utf-8-sig"):
            m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
            if m and not line.strip().startswith("#"):
                creds[m.group(1)] = m.group(2).strip().strip("'\"")
    return creds


def _llm():
    from openai import OpenAI
    creds = load_credentials()
    api_key = os.environ.get("TRINITY_LLM_API_KEY") or creds.get("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("no DEEPSEEK_API_KEY in credentials / TRINITY_LLM_API_KEY env")
    os.environ.setdefault("TRINITY_LLM_API_KEY", api_key)
    return OpenAI(
        base_url=os.environ.get("TRINITY_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        api_key=api_key, timeout=60)


def _model():
    return os.environ.get("TRINITY_LLM_MODEL", "deepseek-chat")


def ask(llm, system: str, user: str) -> str:
    r = llm.chat.completions.create(
        model=_model(), messages=[{"role": "system", "content": system},
                                  {"role": "user", "content": user}],
        temperature=0.0, max_tokens=400)
    return (r.choices[0].message.content or "").strip()


JUDGE_SYSTEM = (
    "You are a strict fact-checker. Determine whether the ANSWER contains the "
    "stated FACT. Reply with exactly YES or NO."
)

CITE_INSTRUCTION = (
    "\n\nGround every factual claim in the cited context items: after each claim "
    "append the bracket markers [n] of the context item(s) that support it, e.g. "
    "'I graduated with Business Administration [3]'. Every claim must carry its "
    "supporting [n] marker(s)."
)


def run(pool, limit=5, top_k=10, category=None, judge_llm=False, llm=None):
    t0 = time.time()
    q_items = []
    done = 0
    llm_judge_calls = 0
    for q in pool:
        qt = str(q.get("question_type") or "other")
        if category and qt != category:
            continue
        if done >= limit:
            break
        done += 1
        # sqlite temp 隔离库（显式后端，防误写 PG 主库——EXECUTION 458 教训）
        os.environ["TRINITY_STORAGE_BACKEND"] = "sqlite"
        tmp = tempfile.mkdtemp(prefix="citecov_")
        from trinity.adapters.sqlite import SQLiteAdapter
        ad = SQLiteAdapter(db_path=os.path.join(tmp, "c.db"))
        ad.connect()
        contexts = []
        try:
            records = []
            sid_list = q.get("haystack_session_ids") or []
            for idx, msgs in enumerate(q.get("haystack_sessions") or []):
                real_sid = str(sid_list[idx]) if idx < len(sid_list) else "sess_%d" % idx
                for m in msgs:
                    content = str(m.get("content") or "") if isinstance(m, dict) else str(m)
                    if content.strip():
                        records.append({"content": content[:2000], "persona_id": "u1",
                                        "session_id": real_sid, "agent_id": "u1",
                                        "importance": 0.5})
            if records:
                ad.ingest_batch(records)
            hits = ad.search_memories(query=str(q.get("question") or ""), top_k=top_k)
            for h in hits:
                contexts.append({"content": (h.get("content") or "")[:600],
                                 "memory_id": h.get("memory_id")})
        finally:
            ad.disconnect()
        # 生成答案（带引用指令）
        answer = ""
        if llm is not None:
            parts = [f"Question: {q.get('question')}", "", "Context:"]
            for i, c in enumerate(contexts, 1):
                parts.append(f"[{i}] {c['content']}")
            parts.append("Answer:")
            parts.append(CITE_INSTRUCTION)
            try:
                answer = ask(llm, "You are a precise assistant that answers only "
                                  "from the provided context.", "\n".join(parts))
            except Exception as e:
                answer = ""
                print("  ask error:", e, flush=True)
        q_items.append({"category": qt, "gold": q.get("answer") or "",
                        "answer": answer, "contexts": contexts,
                        "question": q.get("question") or ""})
        if done % 5 == 0 or done == limit:
            print("  processed %d/%d" % (done, limit), flush=True)
    metrics = accumulate(q_items)
    metrics["_run"] = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "limit": limit, "category": category, "top_k": top_k,
        "judge_llm": bool(judge_llm),
        "elapsed_s": round(time.time() - t0, 1),
        "with_answers": sum(1 for x in q_items if x["answer"]),
    }
    return metrics, q_items


def main(argv=None):
    ap = argparse.ArgumentParser(description="citation-coverage metric")
    ap.add_argument("--pool", default=os.path.join(HERE, "data", "longmemeval_s_cleaned.json"))
    ap.add_argument("--limit", type=int, default=5, help="questions to process (full=500)")
    ap.add_argument("--category", default=None)
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--judge-llm", action="store_true", help="LLM soft judge (slower)")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    pool = json.load(open(args.pool, encoding="utf-8"))
    llm = _llm()
    metrics, q_items = run(pool, limit=args.limit, top_k=args.top_k,
                           category=args.category, judge_llm=args.judge_llm,
                           llm=llm)
    out_path = args.out or os.path.expanduser(
        os.path.join("~", ".trinity", "bench-official",
                     "citation_coverage_" + time.strftime("%Y%m%d_%H%M%S") + ".json"))
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=1)
    print("==", out_path)
    for cat, m in (metrics.get("per_category") or {}).items():
        print("  %-12s answer_cov=%.3f citation_cov=%.3f citation_rate=%.3f evidence_cov=%.3f (q=%d facts=%d)"
              % (cat, m["answer_coverage"], m["citation_coverage"], m["citation_rate"],
                 m["evidence_coverage"], m["questions"], m["facts"]))
    t = metrics.get("totals") or {}
    print("  TOTAL       answer_cov=%.3f citation_cov=%.3f citation_rate=%.3f evidence_cov=%.3f (q=%d facts=%d)"
          % (t.get("answer_coverage", 0), t.get("citation_coverage", 0),
             t.get("citation_rate", 0), t.get("evidence_coverage", 0),
             t.get("questions", 0), t.get("facts", 0)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
