#!/usr/bin/env python3
"""
SQuAD Hybrid Retrieval Benchmark — fixed version.
Uses Trinity adapter search_memories() to avoid persona/tenant filter bug.
"""

import json, os, sys, time, random
from pathlib import Path

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"


def load_squad_subset(path, n_articles=30, seed=42):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)["data"]
    random.seed(seed)
    random.shuffle(data)
    articles = data[:n_articles]
    qa_pairs = []
    for article in articles:
        title = article["title"]
        for para_idx, paragraph in enumerate(article["paragraphs"]):
            context = paragraph["context"].strip()
            for qa in paragraph["qas"]:
                qa_pairs.append({
                    "title": title, "para_idx": para_idx,
                    "context_id": f"{title}__p{para_idx}",
                    "context": context,
                    "question": qa["question"].strip(),
                    "answers": [ans["text"] for ans in qa["answers"]],
                    "answer_start": qa["answers"][0]["answer_start"] if qa["answers"] else 0,
                    "is_impossible": qa.get("is_impossible", False),
                    "qid": qa["id"],
                })
    if len(qa_pairs) > 200:
        by_title = {}
        for q in qa_pairs:
            by_title.setdefault(q["title"], []).append(q)
        sampled = []
        budget_per = max(1, 200 // len(by_title))
        for title, qs in by_title.items():
            sampled.extend(random.sample(qs, min(budget_per, len(qs))))
        qa_pairs = sampled[:200]
    return qa_pairs


def main():
    squad_path = os.environ["TEMP"] + "/squad_dev.json"
    if not os.path.exists(squad_path):
        print(f"ERROR: SQuAD dataset not found at {squad_path}")
        return

    from trinity import Trinity

    print("Loading SQuAD dataset...")
    qa_pairs = load_squad_subset(squad_path, n_articles=30, seed=42)
    titles = sorted(set(q["title"] for q in qa_pairs))
    print(f"  Sampled {len(qa_pairs)} questions from {len(titles)} articles")

    db_path = os.environ["TEMP"] + "/trinity_squad_hybrid_v2.db"
    t = Trinity(store_path=db_path)

    # Ingest unique contexts
    seen = {}
    for q in qa_pairs:
        cid = q["context_id"]
        if cid not in seen:
            seen[cid] = q["context"]
    for cid, ctx in seen.items():
        t.ingest(ctx, persona_id="squad_bench", session_id="ingest", tenant_id="default")
    print(f"  Ingested {len(seen)} unique contexts")

    adapter = t._adapter
    total, hits = 0, 0
    results = []
    t0 = time.time()

    for q in qa_pairs:
        if q["is_impossible"]:
            continue
        total += 1
        question = q["question"]
        context = q["context"]
        cid = q["context_id"]

        # Get all memories matching by content
        all_mems = adapter.get_all_memories()
        # Find memory_id for this context
        target_mid = None
        for m in all_mems:
            if m.get("content") == context:
                target_mid = m.get("memory_id")
                break

        # Search via adapter (BM25+jieba FTS5)
        retrieved = adapter.search_memories(question, top_k=5)
        retrieved_ids = [r.get("memory_id") for r in retrieved]

        hit = target_mid and target_mid in retrieved_ids
        if hit:
            hits += 1
        results.append({
            "qid": q["qid"], "title": q["title"],
            "question": question, "context_id": cid, "hit": hit,
            "retrieved_count": len(retrieved_ids),
        })

    elapsed = time.time() - t0
    r_at_k = hits / total if total > 0 else 0.0

    # Category breakdown
    cat_hits, cat_totals = {}, {}
    for r in results:
        cat = r["title"]
        cat_hits[cat] = cat_hits.get(cat, 0) + (1 if r["hit"] else 0)
        cat_totals[cat] = cat_totals.get(cat, 0) + 1

    hybrid_block = {
        "retrieval_engine": "Trinity retrieval_v47 — Keyword (BM25 FTS5 + jieba, 47-channel routing)",
        "note": "Hybrid (BM25+vector+graph) requires full second_brain deployment; test uses keyword mode via adapter.search_memories(). This is equivalent to the BM25 baseline but routed through Trinity's 47-channel retrieval pipeline.",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        "total_questions": total,
        "hits": hits,
        "R@5": round(r_at_k, 4),
        "R@5_pct": f"{r_at_k*100:.1f}%",
        "k": 5,
        "elapsed_seconds": round(elapsed, 2),
        "qps": round(total / elapsed, 2) if elapsed > 0 else 0,
        "by_category": {
            cat: {"hits": cat_hits[cat], "total": cat_totals[cat],
                  "R@5": round(cat_hits[cat] / cat_totals[cat], 4)}
            for cat in sorted(cat_totals.keys())
        },
    }

    # Write updated JSON
    json_path = str(TRINITY_ROOT / "output" / "third_party_benchmark_results.json")
    existing = {}
    if os.path.exists(json_path):
        with open(json_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    existing["hybrid_retrieval"] = hybrid_block
    if "detailed_results" in existing:
        del existing["detailed_results"]

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

    bm25_r5 = existing.get("R@5", 0)
    delta = r_at_k - bm25_r5

    print(f"\n{'='*60}")
    print(f"  SQuAD Hybrid Retrieval Benchmark")
    print(f"  BM25-only (adapter):    {existing.get('R@5_pct','N/A')} ({existing.get('hits','?')}/{existing.get('total_questions','?')})")
    print(f"  Trinity keyword:        {hybrid_block['R@5_pct']} ({hits}/{total})")
    print(f"  Delta:                  {delta*100:+.1f}%")
    print(f"  Time: {elapsed:.1f}s | QPS: {hybrid_block['qps']}")
    print(f"  Results: {json_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
