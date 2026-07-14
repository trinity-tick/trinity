#!/usr/bin/env python3
"""
LongMemEval Retrieval Evaluator

基于检索结果与 ground truth answer_session_ids 计算 Recall@k 和 NDCG@k，
支持批量评测全部 500 题，输出按类别分组的评测报告。

核心指标：
  - Recall@k: 检索结果 top-k 中命中 answer_session_ids 的比例
  - NDCG@k: 考虑排序位置的归一化折损累积收益

Usage:
  python longmemeval_retriever.py \
      --data ingested_memories.jsonl \
      --retrieval-results retrieval_output.json \
      --output retrieval_report.json

检索结果输入格式（JSON）：
  {
    "sample_0": {
      "retrieved_session_ids": ["sess_3", "sess_7", "sess_1", ...],  // 按相关性降序
      "ground_truth_session_ids": ["sess_3", "sess_7"]
    },
    ...
  }
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Tuple


CATEGORIES = [
    "knowledge-update",
    "multi-session-reasoning",
    "temporal-reasoning",
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
]

K_VALUES = [1, 3, 5, 10]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LongMemEval Retrieval Evaluator — Recall@k & NDCG@k 评测"
    )
    parser.add_argument(
        "--data",
        type=str,
        required=True,
        help="摄入后的 JSONL 数据文件路径（含 answer_session_ids 和 category）",
    )
    parser.add_argument(
        "--retrieval-results",
        type=str,
        required=True,
        help="检索结果 JSON 文件路径",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="retrieval_report.json",
        help="评测报告输出路径 (默认: retrieval_report.json)",
    )
    parser.add_argument(
        "--report-md",
        type=str,
        default=None,
        help="同时输出 Markdown 格式报告 (可选)",
    )
    return parser.parse_args()


def load_data(jsonl_path: str) -> Dict[int, Dict[str, Any]]:
    """
    从 JSONL 加载摄入数据，按 sample_id 聚合。
    返回: {sample_id: {"category": ..., "answer_session_ids": [...], "question": ...}}
    """
    print(f"[Retriever] 加载数据: {jsonl_path}")
    sample_map: Dict[int, Dict[str, Any]] = {}

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            mem = json.loads(line)
            sid = mem.get("source_sample_id", -1)
            if sid == -1:
                continue
            if sid not in sample_map:
                sample_map[sid] = {
                    "category": mem.get("category", "unknown"),
                    "answer_session_ids": mem.get("answer_session_ids", []),
                    "question": mem.get("question", ""),
                    "answer": mem.get("answer", ""),
                }
    print(f"[Retriever] 加载了 {len(sample_map)} 个样本")
    return sample_map


def load_retrieval_results(json_path: str) -> Dict[str, Any]:
    """加载检索结果 JSON。"""
    print(f"[Retriever] 加载检索结果: {json_path}")
    with open(json_path, "r", encoding="utf-8") as f:
        results = json.load(f)
    print(f"[Retriever] 加载了 {len(results)} 条检索结果")
    return results


def _safe_sample_key(sample_id: int) -> str:
    """检索结果文件中的 key 可能是 'sample_0' 或 '0' 或 数字。"""
    for key in [f"sample_{sample_id}", str(sample_id), sample_id]:
        yield key


def compute_recall_at_k(
    retrieved: List[str],
    ground_truth: List[str],
    k: int,
) -> float:
    """计算 Recall@k。"""
    if not ground_truth:
        return 0.0
    top_k = set(retrieved[:k])
    hits = len(top_k & set(ground_truth))
    return hits / len(ground_truth)


def compute_dcg(relevance_scores: List[float], k: int) -> float:
    """计算 DCG@k。"""
    dcg = 0.0
    for i, rel in enumerate(relevance_scores[:k]):
        dcg += (2 ** rel - 1) / math.log2(i + 2)  # i+2 因为 i 是 0-based
    return dcg


def compute_ndcg_at_k(
    retrieved: List[str],
    ground_truth: List[str],
    k: int,
) -> float:
    """计算 NDCG@k：使用 binary relevance (1 if in ground_truth else 0)。"""
    if not ground_truth:
        return 0.0

    gt_set = set(ground_truth)
    # 实际相关性列表
    relevance = [1.0 if sid in gt_set else 0.0 for sid in retrieved[:k]]
    # 理想排序：所有 ground_truth 排在最前面
    ideal_relevance = sorted(relevance, reverse=True)

    dcg = compute_dcg(relevance, k)
    idcg = compute_dcg(ideal_relevance, k)
    if idcg == 0:
        return 0.0
    return dcg / idcg


def evaluate(
    sample_map: Dict[int, Dict[str, Any]],
    retrieval_results: Dict[str, Any],
) -> Dict[str, Any]:
    """
    按样本和类别计算 Recall@k 和 NDCG@k。
    返回完整的评测报告 dict。
    """
    # 聚合结构: category -> k -> [scores]
    recall_scores: Dict[str, Dict[int, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )
    ndcg_scores: Dict[str, Dict[int, List[float]]] = defaultdict(
        lambda: defaultdict(list)
    )

    total_samples = 0
    skipped_samples = 0
    per_sample_results: Dict[int, Dict[str, Any]] = {}

    for sample_id, sample_info in sample_map.items():
        category = sample_info.get("category", "unknown")
        gt_ids = sample_info.get("answer_session_ids", [])

        # 从检索结果中匹配
        retrieved_ids = None
        for key in _safe_sample_key(sample_id):
            if key in retrieval_results:
                retrieved_ids = retrieval_results[key].get("retrieved_session_ids", [])
                break

        if retrieved_ids is None:
            skipped_samples += 1
            continue

        total_samples += 1

        # 计算所有 k 值的指标
        sample_result: Dict[str, Any] = {
            "category": category,
            "question": sample_info.get("question", ""),
            "ground_truth_count": len(gt_ids),
        }
        for k in K_VALUES:
            rec = compute_recall_at_k(retrieved_ids, gt_ids, k)
            ndcg = compute_ndcg_at_k(retrieved_ids, gt_ids, k)
            recall_scores[category][k].append(rec)
            ndcg_scores[category][k].append(ndcg)
            sample_result[f"recall@{k}"] = round(rec, 4)
            sample_result[f"ndcg@{k}"] = round(ndcg, 4)

        per_sample_results[sample_id] = sample_result

        if (total_samples + skipped_samples) % 50 == 0:
            print(f"  [进度] {total_samples + skipped_samples} 样本已评测 ...")

    print(f"[Retriever] 评测完成: {total_samples} 有效样本, {skipped_samples} 跳过")

    # 构建报告
    report = _build_report(
        recall_scores, ndcg_scores, total_samples, skipped_samples, per_sample_results
    )
    return report


def _build_report(
    recall_scores: Dict[str, Dict[int, List[float]]],
    ndcg_scores: Dict[str, Dict[int, List[float]]],
    total_samples: int,
    skipped_samples: int,
    per_sample_results: Dict[int, Dict[str, Any]],
) -> Dict[str, Any]:
    """组装最终报告 dict。"""

    def _agg(scores_dict: Dict[str, Dict[int, List[float]]]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        all_scores: Dict[int, List[float]] = defaultdict(list)
        for cat in CATEGORIES:
            cat_result: Dict[str, float] = {}
            for k in K_VALUES:
                vals = scores_dict.get(cat, {}).get(k, [])
                if vals:
                    cat_result[f"@{k}"] = round(sum(vals) / len(vals), 4)
                    all_scores[k].extend(vals)
                else:
                    cat_result[f"@{k}"] = None
            result[cat] = cat_result
        # Overall
        overall: Dict[str, float] = {}
        for k in K_VALUES:
            vals = all_scores.get(k, [])
            overall[f"@{k}"] = round(sum(vals) / len(vals), 4) if vals else None
        result["overall"] = overall
        return result

    report = {
        "meta": {
            "dataset": "LongMemEval",
            "total_samples_in_data": total_samples + skipped_samples,
            "evaluated_samples": total_samples,
            "skipped_samples": skipped_samples,
            "k_values": K_VALUES,
        },
        "recall": _agg(recall_scores),
        "ndcg": _agg(ndcg_scores),
        "per_sample": per_sample_results,
    }
    return report


def generate_markdown_report(report: Dict[str, Any]) -> str:
    """生成 Markdown 格式的评测报告。"""
    meta = report["meta"]
    lines = []
    lines.append("# LongMemEval Retrieval Evaluation Report")
    lines.append("")
    lines.append(f"- **评测样本数**: {meta['evaluated_samples']}")
    lines.append(f"- **跳过样本数**: {meta['skipped_samples']}")
    lines.append(f"- **K 值**: {K_VALUES}")
    lines.append("")

    # Recall 表格
    lines.append("## Recall@k")
    lines.append("")
    header = "| Category | " + " | ".join([f"R@{k}" for k in K_VALUES]) + " |"
    lines.append(header)
    sep = "|---|" + "|".join(["---" for _ in K_VALUES]) + "|"
    lines.append(sep)

    recall = report["recall"]
    for cat in CATEGORIES:
        vals = [str(recall[cat].get(f"@{k}", "-")) for k in K_VALUES]
        lines.append(f"| {cat} | " + " | ".join(vals) + " |")
    overall_vals = [str(recall["overall"].get(f"@{k}", "-")) for k in K_VALUES]
    lines.append(f"| **Overall** | " + " | ".join(overall_vals) + " |")
    lines.append("")

    # NDCG 表格
    lines.append("## NDCG@k")
    lines.append("")
    header2 = "| Category | " + " | ".join([f"NDCG@{k}" for k in K_VALUES]) + " |"
    lines.append(header2)
    lines.append(sep)

    ndcg = report["ndcg"]
    for cat in CATEGORIES:
        vals = [str(ndcg[cat].get(f"@{k}", "-")) for k in K_VALUES]
        lines.append(f"| {cat} | " + " | ".join(vals) + " |")
    overall_ndcg = [str(ndcg["overall"].get(f"@{k}", "-")) for k in K_VALUES]
    lines.append(f"| **Overall** | " + " | ".join(overall_ndcg) + " |")

    return "\n".join(lines)


def main() -> None:
    args = parse_args()

    # 1. 加载数据
    sample_map = load_data(args.data)

    # 2. 加载检索结果
    retrieval_results = load_retrieval_results(args.retrieval_results)

    # 3. 评测
    report = evaluate(sample_map, retrieval_results)

    # 4. 输出 JSON 报告
    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n[Retriever] JSON 报告已保存: {os.path.abspath(args.output)}")

    # 5. 可选 Markdown 报告
    if args.report_md:
        md_content = generate_markdown_report(report)
        with open(args.report_md, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"[Retriever] Markdown 报告已保存: {os.path.abspath(args.report_md)}")

    # 6. 终端摘要
    print("\n" + "=" * 60)
    print("  Recall@k Summary")
    print("=" * 60)
    recall = report["recall"]
    for k in K_VALUES:
        overall = recall["overall"].get(f"@{k}", "-")
        print(f"  Recall@{k}: {overall}")
    print("-" * 60)
    ndcg = report["ndcg"]
    for k in K_VALUES:
        overall = ndcg["overall"].get(f"@{k}", "-")
        print(f"  NDCG@{k}:   {overall}")
    print("=" * 60)


if __name__ == "__main__":
    main()
