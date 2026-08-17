#!/usr/bin/env python3
"""
LoCoMo Real Evaluator — 用 Trinity 真实检索器（MemoryAggregator hybrid RRF）评测
===============================================================================
与 locomo_runner.py 自带的 MockRetriever 不同，本脚本接入 MemoryAggregator 的
真实混合检索（keyword + vector + RRF fusion），得到可代表真实能力的分数。

用法:
    python benchmark/locomo_real_eval.py [--top-k 5] [--output benchmark/locomo_real_report.json]
"""
import argparse
import json
import os
import sys
import time

# 路径设置：trinity 根目录 + benchmark 目录
TRINITY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRINITY_DIR)
sys.path.insert(0, os.path.join(TRINITY_DIR, "benchmark"))
sys.path.insert(0, os.path.join(TRINITY_DIR, "trinity"))

os.environ.setdefault("TRINITY_SILENT", "1")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--test-set", default=os.path.join(TRINITY_DIR, "benchmark", "locomo_test_set.json"))
    parser.add_argument("--output", default=os.path.join(TRINITY_DIR, "benchmark", "locomo_real_report.json"))
    args = parser.parse_args()

    with open(args.test_set, "r", encoding="utf-8") as f:
        dataset = json.load(f)

    sessions = dataset["sessions"]

    # ── 1. 构建真实聚合器（纯内存，不碰磁盘数据）──────────────
    from trinity.agents.aggregator import MemoryAggregator

    t0 = time.perf_counter()
    agg = MemoryAggregator(persist_path=None)
    print(f"[RealEval] MemoryAggregator 初始化: {time.perf_counter()-t0:.2f}s")

    # ── 2. 灌入全部会话 turns ──────────────────────────────
    n_turns = 0
    for sess in sessions:
        for turn in sess.get("turns", []):
            content = f"[{turn['speaker']}] {turn['text']}"
            agg.ingest(
                content,
                source_agent="locomo-bench",
                metadata={"category": "episodic", "session_id": sess["session_id"], "scope": "locomo"},
            )
            n_turns += 1
    print(f"[RealEval] 已灌入 {n_turns} 条记忆")

    # ── 3. 真实检索器适配器 ─────────────────────────────────
    class RealRetriever:
        def __init__(self, aggregator):
            self._agg = aggregator

        def search(self, query, top_k=5):
            try:
                dvs = self._agg.query(
                    filters={},
                    limit=top_k,
                    mode="hybrid",
                    query_text=query,
                )
            except Exception as exc:
                print(f"[RealEval] hybrid 检索失败，回退 keyword: {exc}")
                dvs = self._agg.query(filters={}, limit=top_k, mode="keyword", query_text=query)
            return dvs  # DimensionVector 含 content 属性

    retriever = RealRetriever(agg)

    # ── 4. 运行 LoCoMo 评测 ─────────────────────────────────
    from locomo_runner import LoCoMoEvaluator

    evaluator = LoCoMoEvaluator()
    results = evaluator.run_eval(retriever, args.test_set, top_k=args.top_k)

    # ── 5. 输出 ─────────────────────────────────────────────
    results["retriever"] = "MemoryAggregator hybrid (keyword+vector+RRF)"
    results["ingested_turns"] = n_turns
    results["engine_stats"] = agg.statistics()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"[RealEval] 报告已保存: {args.output}")

    ov = results["overall"]
    print("=" * 60)
    print(f"真实检索器 LoCoMo 评测结果 (top_k={args.top_k})")
    print(f"  Recall@{args.top_k}:    {ov[f'Recall@{args.top_k}']:.4f}")
    print(f"  Precision@{args.top_k}: {ov[f'Precision@{args.top_k}']:.4f}")
    print(f"  MRR:             {ov['MRR']:.4f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
