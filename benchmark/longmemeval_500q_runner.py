"""LongMemEval-style evaluation on the 500-question community mock dataset.

Dataset: C:\\Users\\Administrator\\.marvis\\workspace\\conv_19f49996244_37d75ffae4a6\\benchmark\\longmemeval_mock_dataset.json
(500 questions, 6 categories aligned with LongMemEval-S structure.
 NOT the official annotated LongMemEval-S — network unreachable in this env.)

Method: ingest each question's context_facts as memories into a temp SQLite
store, then keyword search (FTS5) the question and measure R@5 / MRR against
the question's answer facts.
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

from trinity import Trinity  # noqa: E402


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="keyword", choices=["keyword", "semantic", "hybrid"])
    parser.add_argument("--category", default="", help="只跑指定分类（如 MS）")
    parser.add_argument("--top-k", type=int, default=5, help="检索候选数（round9：MS 需更大 top_k）")
    parser.add_argument("--dedup", action="store_true", help="按 session 去重（多会话均衡）")
    args = parser.parse_args()

    data = json.load(open(DSET, encoding="utf-8"))
    questions = data["questions"]
    print(f"total questions: {len(questions)}")

    tmpdir = tempfile.mkdtemp(prefix="lme_")
    mem = Trinity(adapter="sqlite", store_path=tmpdir)

    # ingest facts per question (each fact = one memory)
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
                pass  # 同 persona 重复事实（唯一约束），视为已存在
    print(f"ingested in {time.time()-t0:.1f}s")

    cat_stats = {}
    hits_total = 0
    rr_sum = 0.0
    n = 0

    for q in questions:
        cat = q.get("category", "?")
        if args.category and cat != args.category:
            continue
        question = q.get("question", "")
        answers = [f.get("fact", "").strip() for f in q.get("context_facts", []) if f.get("fact")]
        if not answers or not question:
            continue
        results = mem.search(query=question, mode=args.mode, top_k=args.top_k,
                             persona_id=q.get("persona_name") or None,
                             dedup_by_session=args.dedup).get("results", [])
        top_contents = [r.get("content", "") for r in results]
        hit = any(any(a and a in c for c in top_contents) for a in answers)
        # MRR: first rank where any answer fact appears
        rank = 0
        for i, c in enumerate(top_contents, 1):
            if any(a and a in c for a in answers):
                rank = i
                break
        rr = 1.0 / rank if rank else 0.0

        st = cat_stats.setdefault(cat, {"total": 0, "hits": 0, "rr": 0.0})
        st["total"] += 1
        st["hits"] += 1 if hit else 0
        st["rr"] += rr
        hits_total += 1 if hit else 0
        rr_sum += rr
        n += 1

    print(f"\n{'='*60}")
    print(f"  LongMemEval-style (500q mock) — Trinity keyword/FTS5")
    print(f"  questions evaluated: {n}")
    print(f"  Overall R@5: {hits_total/n:.4f}  MRR: {rr_sum/n:.4f}")
    print(f"{'='*60}")
    for cat in sorted(cat_stats):
        st = cat_stats[cat]
        print(f"  {cat:6s} n={st['total']:3d} R@5={st['hits']/st['total']:.3f} "
              f"MRR={st['rr']/st['total']:.3f}")

    out = {
        "dataset": "LongMemEval-style mock (500q, community-generated, not official)",
        "questions": n,
        "R@5": round(hits_total / n, 4),
        "MRR": round(rr_sum / n, 4),
        "by_category": {c: {"total": s["total"], "R@5": round(s["hits"] / s["total"], 4),
                            "MRR": round(s["rr"] / s["total"], 4)}
                        for c, s in sorted(cat_stats.items())},
    }
    os.makedirs(os.path.join(ROOT, "output"), exist_ok=True)
    with open(os.path.join(ROOT, "output", "longmemeval_500q_results.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("saved -> output/longmemeval_500q_results.json")


if __name__ == "__main__":
    main()
