#!/usr/bin/env python3
"""
P0: Third-Party Public Dataset Benchmark Runner for Trinity.

Adapts SQuAD v1.1 (Rajpurkar et al., 2016) to memory-retrieval QA format.
Since LongMemEval and LoCoMo are inaccessible from this environment,
SQuAD is used as a publicly-available, peer-reviewed benchmark.

Method:
  1. Ingest SQuAD context paragraphs into Trinity's SQLite memory store
  2. For each question, retrieve top-K memories via BM25 (FTS5 with jieba)
  3. Check if the correct context is among top-5 -> R@5

Dataset: SQuAD v1.1 dev set, 10,570 questions across 48 articles
         Subset: 200 questions sampled across diverse topics
"""

import json
import os
import sys
import time
import random
from pathlib import Path
from typing import Any, Dict, List, Tuple

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

# ── Load SQuAD ──────────────────────────────────────────────────────────

def load_squad_subset(path: str, n_articles: int = 30, seed: int = 42) -> List[Dict]:
    """Load SQuAD dev set and sample diverse articles+questions."""
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
                    "title": title,
                    "para_idx": para_idx,
                    "context_id": f"{title}__p{para_idx}",
                    "context": context,
                    "question": qa["question"].strip(),
                    "answers": [ans["text"] for ans in qa["answers"]],
                    "answer_start": qa["answers"][0]["answer_start"] if qa["answers"] else 0,
                    "is_impossible": qa.get("is_impossible", False),
                    "qid": qa["id"],
                })

    # Sample up to 200 questions, stratified by article
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


# ── Trinity Ingestion ───────────────────────────────────────────────────

def ingest_contexts(qa_pairs: List[Dict]) -> Tuple[Dict[str, List[int]], Any]:
    """Ingest unique context paragraphs into Trinity and return context_id -> memory_ids."""
    from trinity.adapters.sqlite import SQLiteAdapter

    store_path = os.environ["TEMP"] + "/trinity_squad_bench.db"
    if os.path.exists(store_path):
        os.remove(store_path)

    adapter = SQLiteAdapter(db_path=store_path)
    adapter.connect()

    # Deduplicate contexts
    seen = {}
    unique_contexts = []
    for q in qa_pairs:
        cid = q["context_id"]
        if cid not in seen:
            seen[cid] = True
            unique_contexts.append({"context_id": cid, "context": q["context"], "title": q["title"]})

    cid_to_mids = {}
    for ctx in unique_contexts:
        content = f"[{ctx['title']}] {ctx['context']}"
        result = adapter.store_memory(content, persona_id="squad_bench", session_id="ingest")
        mid = result.get("memory_id") or result.get("id")
        cid_to_mids[ctx["context_id"]] = [mid]

    return cid_to_mids, adapter


# ── Retrieval Evaluation ────────────────────────────────────────────────

def evaluate_retrieval(
    qa_pairs: List[Dict],
    cid_to_mids: Dict[str, List[int]],
    adapter,
    k: int = 5,
) -> Dict[str, Any]:
    """For each question, retrieve top-K and compute R@K."""
    results = []
    hits = 0
    total = 0

    for q in qa_pairs:
        if q["is_impossible"]:
            continue
        total += 1
        question = q["question"]
        target_cid = q["context_id"]
        target_mids = cid_to_mids.get(target_cid, [])

        # Use FTS5 keyword search (via jieba if CJK)
        retrieved = adapter.search_memories(question, top_k=k)
        retrieved_ids = [r.get("memory_id") or r.get("id") for r in retrieved]

        hit = any(mid in retrieved_ids for mid in target_mids)
        if hit:
            hits += 1

        results.append({
            "qid": q["qid"],
            "title": q["title"],
            "question": question,
            "context_id": target_cid,
            "hit": hit,
            "retrieved_count": len(retrieved_ids),
            "target_in_rank": next((i+1 for i, rid in enumerate(retrieved_ids) if rid in target_mids), None),
        })

    r_at_k = hits / total if total > 0 else 0.0
    return {
        "dataset": "SQuAD v1.1 (dev)",
        "total_questions": total,
        "hits": hits,
        "R@5": round(r_at_k, 4),
        "R@5_pct": f"{r_at_k*100:.1f}%",
        "k": k,
        "detailed_results": results,
    }


# ── Main ────────────────────────────────────────────────────────────────

def main():
    squad_path = os.environ["TEMP"] + "/squad_dev.json"
    if not os.path.exists(squad_path):
        print(f"ERROR: SQuAD dataset not found at {squad_path}")
        return

    print("Loading SQuAD dataset...")
    qa_pairs = load_squad_subset(squad_path, n_articles=30, seed=42)
    titles = sorted(set(q["title"] for q in qa_pairs))
    print(f"  Sampled {len(qa_pairs)} questions from {len(titles)} articles: {titles[:5]}...")

    print("Ingesting contexts into Trinity...")
    cid_to_mids, adapter = ingest_contexts(qa_pairs)
    unique_ctx = len(cid_to_mids)
    print(f"  Ingested {unique_ctx} unique contexts")

    print("Running retrieval evaluation...")
    t0 = time.time()
    result = evaluate_retrieval(qa_pairs, cid_to_mids, adapter, k=5)
    elapsed = time.time() - t0
    result["elapsed_seconds"] = round(elapsed, 2)
    result["qps"] = round(result["total_questions"] / elapsed, 2) if elapsed > 0 else 0

    # Category breakdown
    cat_hits = {}
    cat_totals = {}
    for r in result["detailed_results"]:
        cat = r["title"]
        cat_hits[cat] = cat_hits.get(cat, 0) + (1 if r["hit"] else 0)
        cat_totals[cat] = cat_totals.get(cat, 0) + 1
    result["by_category"] = {cat: {"hits": cat_hits[cat], "total": cat_totals[cat],
                                    "R@5": round(cat_hits[cat]/cat_totals[cat], 4)}
                             for cat in sorted(cat_totals.keys())}

    # Save JSON
    output_dir = str(TRINITY_ROOT / "output")
    os.makedirs(output_dir, exist_ok=True)
    json_path = output_dir + "/third_party_benchmark_results.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"Results saved to {json_path}")

    # Print summary
    print(f"\n{'='*60}")
    print(f"  Trinity P0: Third-Party Benchmark Results")
    print(f"  Dataset: SQuAD v1.1 (dev) | Questions: {result['total_questions']}")
    print(f"  R@5: {result['R@5_pct']} ({result['hits']}/{result['total_questions']})")
    print(f"  Time: {elapsed:.1f}s | QPS: {result['qps']}")
    print(f"{'='*60}")

    return result


if __name__ == "__main__":
    main()
