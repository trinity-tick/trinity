#!/usr/bin/env python3
"""
OPT3: 会话扩展检索（session-expanded retrieval）验证
====================================================
针对 LongMemEval MS（多会话）类目短板：单次 top-5 检索只覆盖一个会话的
相关事实，跨会话事实会漏检。本脚本实现两段式检索：

  1) 首轮 top-10 检索 → 提取命中的 session_id 集合；
  2) 对每个候选 session 做同 query 检索（session 过滤）→ 合并去重；
  3) 按 score 重排取 top-5。

评测同一 500q mock 数据集，输出"baseline vs session-expand"逐类目 R@5，
确认 MS 类目提升（目标 ≥ 0.8）。
"""
import json
import os
import sys
import tempfile
import time

ROOT = r"C:\Users\Administrator\trinity"
sys.path.insert(0, ROOT)
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

DSET = r"C:\Users\Administrator\.marvis\workspace\conv_19f49996244_37d75ffae4a6\benchmark\longmemeval_mock_dataset.json"


def search_session_expand(mem, question, persona, top_k=5, first_pass=10, max_sessions=3):
    r1 = mem.search(query=question, mode="keyword", top_k=first_pass,
                    persona_id=persona or None).get("results", [])
    sessions = list(dict.fromkeys(str(r.get("session_id") or "") for r in r1))[:max_sessions]
    merged = list(r1)
    for sid in sessions:
        if not sid:
            continue
        r2 = mem.search(query=question, mode="keyword", top_k=first_pass,
                        persona_id=persona or None, session_id=sid).get("results", [])
        seen = {str(m.get("memory_id")) for m in merged}
        for r in r2:
            if str(r.get("memory_id")) not in seen:
                merged.append(r)
                seen.add(str(r.get("memory_id")))
    merged.sort(key=lambda r: float(r.get("score", 0.0)), reverse=True)
    return merged[:top_k]


def main():
    data = json.load(open(DSET, encoding="utf-8"))
    questions = data["questions"]
    print(f"total questions: {len(questions)}")

    tmpdir = tempfile.mkdtemp(prefix="lme_ses_")
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=tmpdir)
    t0 = time.time()
    for q in questions:
        for fact in q.get("context_facts", []):
            ftext = fact.get("fact", "")
            if not ftext:
                continue
            # 会话粒度用事实自身的 session_id（MS 多会话类目的事实跨会话）
            fsess = str(fact.get("session_id") or q.get("session_id") or "0")
            try:
                mem.ingest(ftext, persona_id=q.get("persona_name") or "default",
                           session_id=fsess,
                           category=q.get("category", "general"),
                           tags=["lme", q.get("category", "")])
            except Exception:
                pass
    print(f"ingested in {time.time()-t0:.1f}s")

    def evaluate(use_expand):
        cat = {}
        total = 0
        hits = 0
        for q in questions:
            c = q.get("category", "?")
            question = q.get("question", "")
            facts = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
            if not facts or not question:
                continue
            results = (search_session_expand(mem, question, q.get("persona_name"))
                       if use_expand else
                       mem.search(query=question, mode="keyword", top_k=5,
                                  persona_id=q.get("persona_name") or None).get("results", []))
            contents = "\n".join(r.get("content", "") for r in results)
            hit = any(f and f in contents for f in facts)
            total += 1
            hits += 1 if hit else 0
            st = cat.setdefault(c, {"total": 0, "hits": 0})
            st["total"] += 1
            st["hits"] += 1 if hit else 0
        return total, hits, cat

    print("\n--- baseline (top-5 keyword) ---")
    n1, h1, c1 = evaluate(False)
    print(f"overall R@5: {h1/n1:.4f} ({h1}/{n1})")
    for c in sorted(c1):
        print(f"  {c:6s} R@5={c1[c]['hits']/c1[c]['total']:.3f} ({c1[c]['hits']}/{c1[c]['total']})")

    print("\n--- session-expand (top-10 → sessions → merge → top-5) ---")
    n2, h2, c2 = evaluate(True)
    print(f"overall R@5: {h2/n2:.4f} ({h2}/{n2})")
    for c in sorted(c2):
        print(f"  {c:6s} R@5={c2[c]['hits']/c2[c]['total']:.3f} ({c2[c]['hits']}/{c2[c]['total']})")

    print("\n--- delta ---")
    for c in sorted(c1):
        d = c2[c]["hits"] / c2[c]["total"] - c1[c]["hits"] / c1[c]["total"]
        print(f"  {c:6s} {d:+.3f}")

    out = {
        "test": "session_expand_eval",
        "baseline": {c: round(c1[c]["hits"] / c1[c]["total"], 4) for c in c1},
        "session_expand": {c: round(c2[c]["hits"] / c2[c]["total"], 4) for c in c2},
        "overall_baseline": round(h1 / n1, 4),
        "overall_session_expand": round(h2 / n2, 4),
    }
    with open(os.path.join(ROOT, "output", "session_expand_results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved -> output/session_expand_results.json")


if __name__ == "__main__":
    main()
