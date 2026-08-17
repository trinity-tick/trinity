#!/usr/bin/env python3
"""
LoCoMo Real Evaluator v2 — 检索增强对比评测
=============================================
对比 4 种配置在 LoCoMo 50 题中文集上的真实召回：
  A. turn       : 单 turn 记忆 + hybrid（基线，预期 ~0.10）
  B. session    : session 聚合记忆 + hybrid
  C. turn+exp   : 单 turn + jieba 查询扩展（多查询 RRF）
  D. session+exp: session 聚合 + 查询扩展

用法:
    python benchmark/locomo_real_eval_v2.py [--quick]
"""
import argparse
import json
import os
import sys
import time
from collections import OrderedDict

TRINITY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRINITY_DIR)
sys.path.insert(0, os.path.join(TRINITY_DIR, "benchmark"))
sys.path.insert(0, os.path.join(TRINITY_DIR, "trinity"))
os.environ.setdefault("TRINITY_SILENT", "1")


def build_aggregator():
    from trinity.agents.aggregator import MemoryAggregator
    return MemoryAggregator(persist_path=None)


def ingest_turns(agg, sessions):
    n = 0
    for sess in sessions:
        for turn in sess.get("turns", []):
            agg.ingest(f"[{turn['speaker']}] {turn['text']}",
                       source_agent="locomo-bench",
                       metadata={"category": "episodic", "session_id": sess["session_id"]})
            n += 1
    return n


def ingest_sessions(agg, sessions):
    n = 0
    for sess in sessions:
        texts = [f"[{t['speaker']}] {t['text']}" for t in sess.get("turns", [])]
        agg.ingest("\n".join(texts),
                   source_agent="locomo-bench",
                   metadata={"category": "episodic", "session_id": sess["session_id"]})
        n += 1
    return n


_STOP = {"的", "了", "在", "是", "我", "你", "他", "她", "它", "什么", "怎么", "多少",
         "这个", "那个", "一个", "一下", "吗", "呢", "啊", "与", "和", "对", "把", "被",
         "有", "没", "不", "就", "都", "也", "很", "还", "要", "会", "能", "上", "下"}


def expand_query(query: str):
    """jieba 查询扩展：原 query + 关键词拼接 + 前几个关键词。"""
    import jieba
    words = [w.strip() for w in jieba.lcut(query) if len(w.strip()) >= 2 and w.strip() not in _STOP]
    variants = [query]
    if words:
        variants.append(" ".join(words))
        variants.extend(words[:4])
    return variants, words


def rrf_fusion(ranked_lists, k=60):
    """RRF 融合多个排名列表，返回有序 (score, item)。item 需有 memory_id/content。"""
    scores = {}
    pool = {}
    for lst in ranked_lists:
        for rank, item in enumerate(lst):
            mid = item.memory_id if hasattr(item, "memory_id") else id(item)
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (k + rank + 1)
            pool[mid] = item
    ordered = sorted(scores.items(), key=lambda kv: -kv[1])
    return [(sc, pool[mid]) for mid, sc in ordered]


class Retriever:
    def __init__(self, agg, use_expansion: bool):
        self._agg = agg
        self._use_expansion = use_expansion

    def search(self, query, top_k=5):
        variants, words = expand_query(query) if self._use_expansion else ([query], [])
        ranked_lists = []
        for v in variants[:5]:
            try:
                dvs = self._agg.query(filters={}, limit=max(top_k * 3, 15),
                                      mode="hybrid", query_text=v)
                ranked_lists.append(dvs)
            except Exception:
                try:
                    dvs = self._agg.query(filters={}, limit=max(top_k * 3, 15),
                                          mode="keyword", query_text=v)
                    ranked_lists.append(dvs)
                except Exception:
                    pass
        if not ranked_lists:
            return []
        fused = rrf_fusion(ranked_lists)
        return [item for _, item in fused[:top_k]]


def run_config(name, mode, use_expansion, test_set_path, top_k, quick=False):
    from locomo_runner import LoCoMoEvaluator
    with open(test_set_path, "r", encoding="utf-8") as f:
        dataset = json.load(f)
    sessions = dataset["sessions"]

    agg = build_aggregator()
    if mode == "session":
        n = ingest_sessions(agg, sessions)
    else:
        n = ingest_turns(agg, sessions)

    retriever = Retriever(agg, use_expansion=use_expansion)
    evaluator = LoCoMoEvaluator()
    results = evaluator.run_eval(retriever, test_set_path, top_k=top_k)
    ov = results["overall"]
    print(f"[{name}] ingested={n}  Recall@{top_k}={ov[f'Recall@{top_k}']:.4f}  "
          f"Precision@{top_k}={ov[f'Precision@{top_k}']:.4f}  MRR={ov['MRR']:.4f}")
    return {"config": name, "ingested": n, "overall": ov, "by_category": results.get("by_category", {})}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--test-set", default=os.path.join(TRINITY_DIR, "benchmark", "locomo_test_set.json"))
    parser.add_argument("--output", default=os.path.join(TRINITY_DIR, "benchmark", "locomo_enhanced_report.json"))
    parser.add_argument("--quick", action="store_true", help="只跑基线+最优组合")
    args = parser.parse_args()

    configs = [
        ("A.turn-baseline", "turn", False),
        ("B.session-aggregate", "session", False),
        ("C.turn+query-expansion", "turn", True),
        ("D.session+query-expansion", "session", True),
    ]
    if args.quick:
        configs = [configs[0], configs[3]]

    summary = OrderedDict()
    for name, mode, exp in configs:
        t0 = time.perf_counter()
        try:
            r = run_config(name, mode, exp, args.test_set, args.top_k)
            r["elapsed_s"] = round(time.perf_counter() - t0, 1)
            summary[name] = r
        except Exception as exc:
            print(f"[{name}] 失败: {exc}")
            summary[name] = {"error": str(exc)}

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存: {args.output}")
    print("\n对比总览:")
    for name, r in summary.items():
        if "overall" in r:
            ov = r["overall"]
            print(f"  {name}: Recall@{args.top_k}={ov[f'Recall@{args.top_k}']:.4f}, "
                  f"Precision@{args.top_k}={ov[f'Precision@{args.top_k}']:.4f}, MRR={ov['MRR']:.4f}, {r['elapsed_s']}s")


if __name__ == "__main__":
    main()
