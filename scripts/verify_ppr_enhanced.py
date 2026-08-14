#!/usr/bin/env python3
"""
verify_ppr_enhanced.py — HippoRAG 2 增强 PPR vs 原始 PPR 对比验证

使用 5 个多跳推理查询，对比：
  - 原始 PPR（KnowledgeGraph.ppr_search）
  - HippoRAG 2 增强 PPR（HippoRAG2PPR.search）

评估指标:
  - Recall@5: Top-5 结果中包含正确答案的比例
  - MRR (Mean Reciprocal Rank): 第一个正确答案的倒数排名均值
  - 检索路径长度: PPR 收敛所需的迭代次数 / 图遍历深度

输出: data/kgraph/ppr_comparison_report.json
用法: python scripts/verify_ppr_enhanced.py
"""

import json
import os
import sys
import time
from collections import defaultdict


PROJECT_ROOT = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".."
))
sys.path.insert(0, PROJECT_ROOT)

from trinity.kgraph import KnowledgeGraph, HippoRAG2PPR


# ══════════════════════════════════════════════════════════════════════
# 测试查询集 — 5 个多跳推理查询
# 每个查询包含: query（自然语言）、seed_entities（原始PPR用的种子实体）、
#               ground_truth（期望答案实体ID列表）
# ══════════════════════════════════════════════════════════════════════

TEST_QUERIES = [
    {
        "id": "Q1",
        "query": "Trinity v6.37 使用了哪些向量存储技术？",
        "description": "双跳: Trinity → uses → 向量存储技术",
        "seed_entities": ["trinity_v6_37"],
        "ground_truth": [
            "chromadb", "faiss", "sqlite", "postgresql",
        ],
    },
    {
        "id": "Q2",
        "query": "MetaEvolution 模块产生了哪些记忆分类？",
        "description": "双跳: MetaEvolution → produces → 记忆分类",
        "seed_entities": ["metaevolution"],
        "ground_truth": [
            "category_handoff", "category_evolution_state",
        ],
    },
    {
        "id": "Q3",
        "query": "彩棠品牌的仓库布局规则有哪些？",
        "description": "双跳: 彩棠 → applies_to ← 规则",
        "seed_entities": ["caitang"],
        "ground_truth": [
            "heavy_rule", "bubble_pack_rule", "color_separation_rule",
        ],
    },
    {
        "id": "Q4",
        "query": "WMS 项目 M12 阶段包含哪些算法引擎？",
        "description": "三跳: WMS → M12 → part_of ← 算法引擎",
        "seed_entities": ["wms_project", "milestone_m12"],
        "ground_truth": [
            "putaway_engine", "cartonization", "wave_engine", "pick_path_optimizer",
        ],
    },
    {
        "id": "Q5",
        "query": "Marvis 使用了哪些大语言模型？这些模型属于哪个智能系统？",
        "description": "三跳: Marvis → uses → 模型 → runs_on → 系统",
        "seed_entities": ["marvis"],
        "ground_truth": [
            "hunyu_hy3", "deepseek_v4_pro", "trinity_v6_37",
        ],
    },
]


# ══════════════════════════════════════════════════════════════════════
# 评估函数
# ══════════════════════════════════════════════════════════════════════

def evaluate_recall_at_k(
    retrieved_ids: list[str],
    ground_truth: list[str],
    k: int = 5,
) -> float:
    """计算 Recall@K。

    参数:
        retrieved_ids: 检索结果的实体 ID 列表（按得分排序）。
        ground_truth: 正确答案实体 ID 列表。
        k: Top-K。

    返回:
        Recall@K 值 [0, 1]。
    """
    if not ground_truth:
        return 0.0

    top_k_ids = set(retrieved_ids[:k])
    hits = len(top_k_ids & set(ground_truth))
    return hits / len(ground_truth)


def evaluate_mrr(
    retrieved_ids: list[str],
    ground_truth: list[str],
) -> float:
    """计算 MRR (Mean Reciprocal Rank)。

    参数:
        retrieved_ids: 检索结果的实体 ID 列表（按得分排序）。
        ground_truth: 正确答案实体 ID 列表。

    返回:
        MRR 值 [0, 1]。
    """
    if not ground_truth:
        return 0.0

    gt_set = set(ground_truth)
    for rank, rid in enumerate(retrieved_ids, 1):
        if rid in gt_set:
            return 1.0 / rank
    return 0.0


def evaluate_path_length(
    result: dict,
    kg: KnowledgeGraph,
    seed_entities: list[str],
) -> dict:
    """估算检索路径长度。

    通过检查从种子实体到每个 top 结果的 BFS 距离来估算。

    参数:
        result: PPR 搜索结果（phrases 列表）。
        kg: KnowledgeGraph 实例。
        seed_entities: 种子实体列表。

    返回:
        {
            "min_hops": int,
            "max_hops": int,
            "avg_hops": float,
            "ppr_iterations": int (if available),
        }
    """
    phrase_ids = [r.get("entity_id", r.get("node_id", ""))
                  for r in result.get("phrases", result.get("phrases", []))]

    # 通过 BFS 计算最短跳数
    def bfs_distance(start: str, target: str, max_depth: int = 10) -> int | None:
        if start == target:
            return 0
        visited = {start}
        queue = [(start, 0)]
        while queue:
            current, depth = queue.pop(0)
            if depth >= max_depth:
                continue
            for idx in kg._relation_index.get(current, []):
                rel = kg._relations[idx]
                neighbor = rel["object"] if rel["subject"] == current else rel["subject"]
                if neighbor == target:
                    return depth + 1
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, depth + 1))
        return None

    all_dists: list[int] = []
    for pid in phrase_ids[:10]:
        min_dist = None
        for seed in seed_entities:
            d = bfs_distance(seed, pid)
            if d is not None and (min_dist is None or d < min_dist):
                min_dist = d
        if min_dist is not None:
            all_dists.append(min_dist)

    if not all_dists:
        return {"min_hops": 0, "max_hops": 0, "avg_hops": 0.0, "ppr_iterations": 0}

    return {
        "min_hops": min(all_dists),
        "max_hops": max(all_dists),
        "avg_hops": round(sum(all_dists) / len(all_dists), 2),
        "ppr_iterations": result.get("stats", {}).get("iterations", 0),
    }


# ══════════════════════════════════════════════════════════════════════
# 主对比验证流程
# ══════════════════════════════════════════════════════════════════════

def run_comparison(
    kg_path: str | None = None,
    dense_sparse_path: str | None = None,
) -> dict:
    """执行完整对比验证。

    返回: 对比报告 dict。
    """
    # ── 加载知识图谱 ──
    print("=" * 60)
    print("HippoRAG 2 Enhanced PPR — 对比验证")
    print("=" * 60)

    print("\n[初始化] 加载知识图谱...")
    kg = KnowledgeGraph(storage_path=kg_path) if kg_path else KnowledgeGraph()
    print(f"  实体: {kg.get_stats()['entity_count']}, 关系: {kg.get_stats()['relation_count']}")

    # ── 初始化增强 PPR ──
    print("[初始化] 构建 HippoRAG 2 增强 PPR...")
    h2ppr = HippoRAG2PPR(
        knowledge_graph=kg,
        alpha=0.85,
    )

    # 加载 dense-sparse 数据（如果存在）
    ds_loaded = False
    if dense_sparse_path and os.path.exists(dense_sparse_path):
        print(f"  从 {dense_sparse_path} 加载 Dense-Sparse 数据...")
        try:
            with open(dense_sparse_path, "r", encoding="utf-8") as f:
                passage_list: list[dict] = []
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    item = json.loads(line)
                    if item.get("type") == "passage_node":
                        passage_list.append({
                            "passage_id": item["id"],
                            "content": item.get("content", ""),
                            "phrase_ids": item.get("phrase_ids", []),
                            "source_memory_id": item.get("source_memory_id", ""),
                            "metadata": {
                                "category": item.get("category", ""),
                                "importance": item.get("importance", 0.5),
                            },
                        })
                    elif item.get("type") == "synonym_edge":
                        h2ppr.add_synonym_edge(
                            phrase_id_a=item["source"],
                            phrase_id_b=item["target"],
                            confidence=item.get("confidence", 1.0),
                        )

                if passage_list:
                    h2ppr.add_passages_batch(passage_list)
                    ds_loaded = True
                    print(f"  加载 {len(passage_list)} 段落节点")
        except Exception as e:
            print(f"  WARNING: 加载 Dense-Sparse 数据失败: {e}")

    stats = h2ppr.get_stats()
    print(f"  短语节点: {stats['phrase_nodes']}, 段落节点: {stats['passage_nodes']}")
    print(f"  总节点: {stats['total_nodes']}, 总边: {stats['total_edges']}")

    # ── 执行每个查询 ──
    results_per_query: list[dict] = []
    aggregate = {
        "original": {"recall_at_5": [], "mrr": [], "path_lengths": []},
        "enhanced": {"recall_at_5": [], "mrr": [], "path_lengths": []},
    }

    print("\n" + "=" * 60)
    for qi, tq in enumerate(TEST_QUERIES, 1):
        print(f"\n[{qi}/5] {tq['id']}: {tq['query']}")
        print(f"  描述: {tq['description']}")
        print(f"  Ground Truth ({len(tq['ground_truth'])}): {tq['ground_truth']}")

        # ── 原始 PPR ──
        orig_results = kg.ppr_search(
            query_entities=tq["seed_entities"],
            alpha=0.85,
            top_k=10,
        )
        orig_ids = [r["entity_id"] for r in orig_results]
        orig_recall5 = evaluate_recall_at_k(orig_ids, tq["ground_truth"], k=5)
        orig_mrr_val = evaluate_mrr(orig_ids, tq["ground_truth"])

        # 构建可序列化的原始结果
        orig_phrases = [
            {
                "node_id": r["entity_id"],
                "text": r["entity"].get("properties", {}).get("name", r["entity_id"]),
                "ppr_score": r["ppr_score"],
            }
            for r in orig_results
        ]

        # ── 增强 PPR ──
        enh_result = h2ppr.search(
            query=tq["query"],
            top_k=10,
            include_passages=True,
            use_recognition_memory=True,
            seed_entities=tq["seed_entities"],
        )

        enh_ids = [r["node_id"] for r in enh_result["phrases"]]
        # 段落节点也纳入评估（如果 ground truth 中的实体以 passage 形式出现）
        enh_passage_ids = [r["node_id"] for r in enh_result["passages"]]
        all_enhanced_ids = enh_ids + enh_passage_ids
        enh_recall5 = evaluate_recall_at_k(all_enhanced_ids, tq["ground_truth"], k=5)
        enh_mrr_val = evaluate_mrr(all_enhanced_ids, tq["ground_truth"])

        # ── 路径长度 ──
        orig_path = evaluate_path_length(
            {"phrases": orig_phrases},
            kg,
            tq["seed_entities"],
        )
        enh_path = evaluate_path_length(
            enh_result,
            kg,
            tq["seed_entities"],
        )

        # ── 记录每个查询的结果 ──
        query_result = {
            "query_id": tq["id"],
            "query": tq["query"],
            "description": tq["description"],
            "seed_entities": tq["seed_entities"],
            "ground_truth": tq["ground_truth"],
            "original_ppr": {
                "top_10_entities": orig_ids,
                "top_10_scores": [r["ppr_score"] for r in orig_results],
                "recall_at_5": orig_recall5,
                "mrr": orig_mrr_val,
                "path_length": orig_path,
            },
            "enhanced_ppr": {
                "top_10_phrase_entities": enh_ids,
                "top_10_passage_entities": enh_passage_ids,
                "recall_at_5": enh_recall5,
                "mrr": enh_mrr_val,
                "path_length": enh_path,
                "ppr_iterations": enh_result["stats"]["iterations"],
                "n_phrase_nodes": enh_result["stats"]["n_phrase_nodes"],
                "n_passage_nodes": enh_result["stats"]["n_passage_nodes"],
            },
            "improvement": {
                "recall_at_5_delta": round(enh_recall5 - orig_recall5, 4),
                "mrr_delta": round(enh_mrr_val - orig_mrr_val, 4),
            },
        }

        results_per_query.append(query_result)

        # 累积指标
        aggregate["original"]["recall_at_5"].append(orig_recall5)
        aggregate["original"]["mrr"].append(orig_mrr_val)
        aggregate["original"]["path_lengths"].append(orig_path.get("avg_hops", 0))
        aggregate["enhanced"]["recall_at_5"].append(enh_recall5)
        aggregate["enhanced"]["mrr"].append(enh_mrr_val)
        aggregate["enhanced"]["path_lengths"].append(enh_path.get("avg_hops", 0))

        # 打印单条结果
        print(f"  原始 PPR: Recall@5={orig_recall5:.2f}, MRR={orig_mrr_val:.4f}, "
              f"AvgHops={orig_path.get('avg_hops', 0):.1f}")
        print(f"  增强 PPR: Recall@5={enh_recall5:.2f}, MRR={enh_mrr_val:.4f}, "
              f"AvgHops={enh_path.get('avg_hops', 0):.1f}, "
              f"Iter={enh_result['stats']['iterations']}")
        delta_recall = enh_recall5 - orig_recall5
        delta_mrr = enh_mrr_val - orig_mrr_val
        indicator = "▲+" if delta_recall > 0 else ("▼" if delta_recall < 0 else "=")
        print(f"  Δ: Recall@5 {indicator}{delta_recall:+.2f}, MRR {delta_mrr:+.4f}")

    # ── 汇总 ──
    def _avg(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    report = {
        "title": "HippoRAG 2 Enhanced PPR vs Original PPR — Comparison Report",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "config": {
            "alpha": 0.85,
            "restart_probability": 0.15,
            "dense_sparse_loaded": ds_loaded,
            "kgraph_stats": kg.get_stats(),
            "enhanced_stats": h2ppr.get_stats(),
        },
        "aggregate": {
            "original_ppr": {
                "avg_recall_at_5": _avg(aggregate["original"]["recall_at_5"]),
                "avg_mrr": _avg(aggregate["original"]["mrr"]),
                "avg_path_length": _avg(aggregate["original"]["path_lengths"]),
                "recall_at_5": aggregate["original"]["recall_at_5"],
                "mrr": aggregate["original"]["mrr"],
            },
            "enhanced_ppr": {
                "avg_recall_at_5": _avg(aggregate["enhanced"]["recall_at_5"]),
                "avg_mrr": _avg(aggregate["enhanced"]["mrr"]),
                "avg_path_length": _avg(aggregate["enhanced"]["path_lengths"]),
                "recall_at_5": aggregate["enhanced"]["recall_at_5"],
                "mrr": aggregate["enhanced"]["mrr"],
            },
            "improvement": {
                "recall_at_5_delta": round(
                    _avg(aggregate["enhanced"]["recall_at_5"])
                    - _avg(aggregate["original"]["recall_at_5"]), 4
                ),
                "mrr_delta": round(
                    _avg(aggregate["enhanced"]["mrr"])
                    - _avg(aggregate["original"]["mrr"]), 4
                ),
                "path_length_delta": round(
                    _avg(aggregate["enhanced"]["path_lengths"])
                    - _avg(aggregate["original"]["path_lengths"]), 2
                ),
            },
        },
        "per_query": results_per_query,
        "summary_text": "",
    }

    # 生成文字摘要
    o_recall = report["aggregate"]["original_ppr"]["avg_recall_at_5"]
    e_recall = report["aggregate"]["enhanced_ppr"]["avg_recall_at_5"]
    o_mrr = report["aggregate"]["original_ppr"]["avg_mrr"]
    e_mrr = report["aggregate"]["enhanced_ppr"]["avg_mrr"]
    imp = report["aggregate"]["improvement"]

    summary_lines = [
        "=" * 60,
        "  对比验证摘要",
        "=" * 60,
        "",
        f"指标                    原始 PPR        增强 PPR        Δ",
        f"{'─' * 60}",
        f"Avg Recall@5            {o_recall:<16.4f}{e_recall:<16.4f}{imp['recall_at_5_delta']:+.4f}",
        f"Avg MRR                 {o_mrr:<16.4f}{e_mrr:<16.4f}{imp['mrr_delta']:+.4f}",
        f"Avg Path Length         {report['aggregate']['original_ppr']['avg_path_length']:<16.2f}"
        f"{report['aggregate']['enhanced_ppr']['avg_path_length']:<16.2f}"
        f"{imp['path_length_delta']:+.2f}",
        "",
        f"Dense-Sparse 已加载: {ds_loaded}",
        f"短语节点数: {h2ppr.get_stats()['phrase_nodes']}",
        f"段落节点数: {h2ppr.get_stats()['passage_nodes']}",
        f"PPR α = {0.85} (重启概率 = 0.15)",
        "",
    ]

    report["summary_text"] = "\n".join(summary_lines)
    print("\n" + report["summary_text"])

    return report


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="HippoRAG 2 Enhanced PPR 对比验证"
    )
    parser.add_argument(
        "--kgraph", default=None,
        help="kgraph_data.jsonl 路径（默认使用默认路径）"
    )
    parser.add_argument(
        "--dense-sparse",
        default=os.path.join(PROJECT_ROOT, "data", "kgraph", "dense_sparse_kgraph.jsonl"),
        help="dense_sparse_kgraph.jsonl 路径"
    )
    parser.add_argument(
        "--output",
        default=os.path.join(PROJECT_ROOT, "data", "kgraph", "ppr_comparison_report.json"),
        help="报告输出路径"
    )

    args = parser.parse_args()

    report = run_comparison(
        kg_path=args.kgraph,
        dense_sparse_path=args.dense_sparse,
    )

    # 保存报告
    output_path = args.output
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    print(f"\n报告已保存: {output_path}")

    return report


if __name__ == "__main__":
    main()
