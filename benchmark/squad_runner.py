#!/usr/bin/env python3
"""
SQuAD v1.1 Unified Benchmark Runner — 统一评测入口（M1-1）
=============================================================

背景：仓库曾并存两份 SQuAD 评测（squad_benchmark_runner.py 的 BM25-only
35.6%，与 squad_hybrid_runner.py 的 47-channel keyword 对比 98.3%），
口径不一致导致 README/JSON 数字互相矛盾（DSH workflow 基准核查报告披露）。

本文件是**唯一权威入口**：单次运行、同一数据子集、同一命中判定，输出两份
子口径 + 一个 headline 数字，写入固定产物 `output/squad_unified_results.json`。

方法论（固定，勿改）：
  - 数据：SQuAD v1.1 dev（%TEMP%/squad_dev.json），30 篇文章 / seed 42 /
    最多 200 题（与历史一致），跳过 is_impossible；
  - 入库：唯一 context 段落 → 独立临时 SQLite（trinity_squad_unified.db）；
  - 子口径 A `bm25_adapter`：adapter.search_memories(question, top_k=5)
    （低层 BM25/FTS5 通道，passage-selection 视角）；
  - 子口径 B `keyword_47ch`：Trinity.search(mode='keyword', top_k=5)
    （产品级 47-channel 路由检索，**headline 口径**）；
  - 命中判定：目标 context 的 memory_id 出现在 top-5（A），或目标 content
    出现在 results（B）。两者均为 R@5。
  - 输出：headline = keyword_47ch 的 R@5，并同时披露 bm25_adapter。

用法：
    python benchmark/squad_runner.py [--n-articles 30] [--seed 42] [--max-q 200]
"""

import json
import os
import random
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TRINITY_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TRINITY_ROOT))
os.environ["TRINITY_MEMORY_ENABLED"] = "0"
os.environ["PYTHONIOENCODING"] = "utf-8"


def load_squad_subset(path: str, n_articles: int = 30, seed: int = 42,
                      max_q: int = 200) -> List[Dict]:
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
    if len(qa_pairs) > max_q:
        by_title: Dict[str, List[Dict]] = {}
        for q in qa_pairs:
            by_title.setdefault(q["title"], []).append(q)
        sampled = []
        budget_per = max(1, max_q // len(by_title))
        for title, qs in by_title.items():
            sampled.extend(random.sample(qs, min(budget_per, len(qs))))
        qa_pairs = sampled[:max_q]
    return qa_pairs


def ingest_contexts(qa_pairs: List[Dict]) -> Tuple[Dict[str, List[str]], Any]:
    """入库唯一 context，返回 context_id -> [memory_id]。"""
    from trinity.adapters.sqlite import SQLiteAdapter

    store_path = os.environ["TEMP"] + "/trinity_squad_unified.db"
    if os.path.exists(store_path):
        os.remove(store_path)
    adapter = SQLiteAdapter(db_path=store_path)
    adapter.connect()

    seen = {}
    for q in qa_pairs:
        seen.setdefault(q["context_id"], {"context": q["context"], "title": q["title"]})

    cid_to_mids: Dict[str, List[str]] = {}
    for cid, ctx in seen.items():
        content = f"[{ctx['title']}] {ctx['context']}"
        result = adapter.store_memory(content, persona_id="squad_bench", session_id="ingest")
        mid = result.get("memory_id") or result.get("id")
        cid_to_mids[cid] = [str(mid)]
    return cid_to_mids, adapter


def evaluate_bm25_adapter(qa_pairs, cid_to_mids, adapter, k=5) -> Dict[str, Any]:
    hits = 0
    total = 0
    for q in qa_pairs:
        if q["is_impossible"]:
            continue
        total += 1
        retrieved = adapter.search_memories(q["question"], top_k=k)
        retrieved_ids = [str(r.get("memory_id") or r.get("id")) for r in retrieved]
        if any(mid in retrieved_ids for mid in cid_to_mids.get(q["context_id"], [])):
            hits += 1
    r5 = hits / total if total else 0.0
    return {"pipeline": "bm25_adapter",
            "note": "adapter.search_memories() — 低层 BM25/FTS5 通道（passage-selection 视角）",
            "total_questions": total, "hits": hits,
            "R@5": round(r5, 4), "R@5_pct": f"{r5*100:.1f}%"}


def evaluate_keyword_47ch(qa_pairs, cid_to_mids, trinity, k=5) -> Dict[str, Any]:
    hits = 0
    total = 0
    for q in qa_pairs:
        if q["is_impossible"]:
            continue
        total += 1
        s = trinity.search(q["question"], top_k=k, mode="keyword")
        results = s.get("results", []) if isinstance(s, dict) else []
        retrieved_ids = [str(r.get("memory_id") or r.get("id")) for r in results]
        if any(mid in retrieved_ids for mid in cid_to_mids.get(q["context_id"], [])):
            hits += 1
    r5 = hits / total if total else 0.0
    return {"pipeline": "keyword_47ch",
            "note": "Trinity.search(mode='keyword') — 产品级 47-channel 路由检索（headline 口径）",
            "total_questions": total, "hits": hits,
            "R@5": round(r5, 4), "R@5_pct": f"{r5*100:.1f}%"}


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="SQuAD v1.1 unified benchmark")
    parser.add_argument("--n-articles", type=int, default=30)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-q", type=int, default=200)
    parser.add_argument("--squad-path", default=os.environ["TEMP"] + "/squad_dev.json")
    args = parser.parse_args()

    if not os.path.exists(args.squad_path):
        print(f"ERROR: SQuAD dataset not found at {args.squad_path}")
        return 1

    print("Loading SQuAD dataset...")
    qa_pairs = load_squad_subset(args.squad_path, args.n_articles, args.seed, args.max_q)
    titles = sorted(set(q["title"] for q in qa_pairs))
    print(f"  {len(qa_pairs)} questions / {len(titles)} articles")

    print("Ingesting contexts...")
    cid_to_mids, adapter = ingest_contexts(qa_pairs)
    print(f"  {len(cid_to_mids)} unique contexts")

    print("Running BM25 adapter evaluation...")
    t0 = time.time()
    bm25 = evaluate_bm25_adapter(qa_pairs, cid_to_mids, adapter, k=5)
    bm25["elapsed_seconds"] = round(time.time() - t0, 2)

    from trinity import Trinity
    db_path = os.environ["TEMP"] + "/trinity_squad_unified.db"
    t = Trinity(store_path=db_path)
    t._adapter = adapter  # reuse same store for the keyword path

    print("Running 47-channel keyword evaluation...")
    t0 = time.time()
    kw = evaluate_keyword_47ch(qa_pairs, cid_to_mids, t, k=5)
    kw["elapsed_seconds"] = round(time.time() - t0, 2)

    headline = kw["R@5_pct"]
    report = {
        "test": "squad_unified",
        "dataset": "SQuAD v1.1 (dev)",
        "config": {"n_articles": args.n_articles, "seed": args.seed,
                   "max_questions": args.max_q, "top_k": 5},
        "methodology": ("单次运行、同一子集(seed=42)、同一命中判定(R@5)。"
                        "headline 取 keyword_47ch（产品级 47-channel 路由检索）；"
                        "bm25_adapter 为低层通道基线，两者并存披露，避免口径混用。"),
        "headline_R@5": headline,
        "sub_metrics": {"bm25_adapter": bm25, "keyword_47ch": kw},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
    }

    out_dir = TRINITY_ROOT / "output"
    out_dir.mkdir(exist_ok=True)
    json_path = out_dir / "squad_unified_results.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*66}")
    print(f"  SQuAD v1.1 Unified Benchmark — {report['dataset']}")
    print(f"  Questions: {bm25['total_questions']}  (seed={args.seed}, {args.n_articles} articles)")
    print(f"  headline R@5 (keyword_47ch): {kw['R@5_pct']} ({kw['hits']}/{kw['total_questions']})")
    print(f"  bm25_adapter  R@5:           {bm25['R@5_pct']} ({bm25['hits']}/{bm25['total_questions']})")
    print(f"  Results -> {json_path}")
    print(f"{'='*66}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
