#!/usr/bin/env python3
"""
Trinity — Leaderboard 生成器（2026-08-15）
==========================================
汇总公开/内部基准结果到 benchmark/LEADERBOARD.md：
  - BEAM 规模延迟（beam_results.csv）
  - LoCoMo 召回（locomo_real_report.json / locomo_enhanced_report.json）
  - 延迟/吞吐/检索/抗幻觉/压缩（MEMBENCH_REPORT v1.0 已知数字，标注来源）

用法：
    python benchmark/generate_leaderboard.py [--locomo <json>]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LOCONO = os.path.join(HERE, "locomo_real_report.json")
DEFAULT_BEAM = os.path.join(HERE, "beam_results.csv")


def read_beam(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    return rows


def read_locomo(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def locomo_overall(locomo: dict) -> dict:
    """从报告中提取代表配置的 overall 指标：优先 B.session-aggregate（推荐配置）。"""
    for key in ("B.session-aggregate", "session", "overall"):
        if key in locomo:
            v = locomo[key]
            if isinstance(v, dict):
                # 若外层直接含 recall_at_5 等指标则用本身
                if any(k in v for k in ("recall_at_5", "mrr_at_5", "Recall@5")):
                    return v
                for sub in v.values():
                    if isinstance(sub, dict) and any(k in sub for k in ("recall_at_5", "Recall@5")):
                        return sub
            break
    return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--locomo", default=DEFAULT_LOCONO)
    parser.add_argument("--beam", default=DEFAULT_BEAM)
    parser.add_argument("--output", default=os.path.join(HERE, "LEADERBOARD.md"))
    args = parser.parse_args()

    beam = read_beam(args.beam) if os.path.exists(args.beam) else []
    locomo = read_locomo(args.locomo) if os.path.exists(args.locomo) else {}

    lines = []
    lines.append("# Trinity Leaderboard（2026-08-15）")
    lines.append("")
    lines.append(f"> 生成时间：{datetime.now(timezone.utc).isoformat()}；版本 v8.2.0；环境 Windows / Python 3.14 / API :8001")
    lines.append("")

    lines.append("## 一、BEAM 规模延迟（本地模拟 50 查询）")
    lines.append("")
    lines.append("| 规模 | 记忆数 | QPS | P50 | P95 | P99 | Mean | Recall@5 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in beam:
        lines.append(
            f"| {r['scale']} | {r['memory_count']} | {r['qps']} | {r['p50_ms']}ms | "
            f"{r['p95_ms']}ms | {r['p99_ms']}ms | {r['mean_lat_ms']}ms | {r['mean_recall_at_5']} |"
        )
    lines.append("")

    lines.append("## 二、LoCoMo 长程召回（50 题真实评测，B.session-aggregate 代表配置）")
    lines.append("")
    ov = locomo_overall(locomo)
    if ov:
        key_map = {"recall_at_5": "Recall@5", "Recall@5": "Recall@5",
                   "mrr_at_5": "MRR@5", "MRR": "MRR",
                   "precision_at_5": "Precision@5", "Precision@5": "Precision@5"}
        lines.append("| 指标 | 值 |")
        lines.append("|---|---|")
        for k, label in key_map.items():
            if k in ov:
                lines.append(f"| {label} | {ov[k]} |")
        lines.append("")
        lines.append("### 按类别")
        lines.append("")
        bc = locomo.get("by_category", {})
        if bc:
            lines.append("| 类别 | Recall@5 |")
            lines.append("|---|---|")
            for cat, v in bc.items():
                r5 = v.get("recall_at_5", v) if isinstance(v, dict) else v
                lines.append(f"| {cat} | {r5} |")
        lines.append("")

    lines.append("## 三、LongMemEval（本地 55 题模拟集，BM25 检索）")
    lines.append("")
    lme = os.path.join(HERE, "..", "output", "longmemeval_results.json")
    if os.path.exists(lme):
        try:
            with open(lme, encoding="utf-8") as f:
                d = json.load(f)
            ov = d.get("overall", {})
            lines.append("| 指标 | 值 |")
            lines.append("|---|---|")
            lines.append(f"| Recall@5（整体） | {ov.get('recall_at_5', ov.get('R@5', 'n/a'))} |")
            for cat, v in (d.get("by_category") or {}).items():
                r5 = v.get("recall_at_5", v.get("R@5")) if isinstance(v, dict) else v
                lines.append(f"| {cat} | {r5} |")
            lines.append(f"| 题数 | {d.get('total_questions')} |")
            lines.append("")
        except Exception:
            lines.append("（读取 longmemeval_results.json 失败）")
            lines.append("")

    lme500 = os.path.join(HERE, "..", "output", "longmemeval_500q_results.json")
    if os.path.exists(lme500):
        try:
            with open(lme500, encoding="utf-8") as f:
                d5 = json.load(f)
            lines.append("### LongMemEval 500q（本地 mock 集，6 分类）")
            lines.append("")
            lines.append("| 分类 | n | R@5 | MRR |")
            lines.append("|---|---|---|---|")
            for cat, v in (d5.get("by_category") or {}).items():
                lines.append(f"| {cat} | {v.get('n', '')} | {v.get('R@5', '')} | {v.get('MRR', '')} |")
            lines.append("")
        except Exception:
            pass

    lines.append("## 四、MemBench v1.0 核心指标（2026-08-14 实测，来源 benchmark/MEMBENCH_REPORT.md）")
    lines.append("")
    lines.append("| 维度 | 指标 | 结果 |")
    lines.append("|---|---|---|")
    lines.append("| 延迟 | E2E P50 / P99 | 41ms / 49ms |")
    lines.append("| 吞吐 | 200 并发 QPS | 2,431（内存稳定 ~27MB） |")
    lines.append("| 检索质量 | SQuAD R@5（80 题） | 98.3% |")
    lines.append("| 长程记忆 | LoCoMo Recall@5（会话聚合） | 0.88 |")
    lines.append("| 抗幻觉 | MemSyco Composite（LLM judge） | 0.88（幻觉率 10%） |")
    lines.append("| 压缩经济 | 记忆压缩 token 节省 | ~21% |")
    lines.append("| 规模 | 大库 / 图 | 11.7k 记忆 / 11.1k 实体 / 28.3k 关系 |")
    lines.append("")

    lines.append("## 四、口径说明")
    lines.append("")
    lines.append("- BEAM 为本地 1K/10K/100K 模拟规模（beam_gin_index），非官方 BEAM 10M token 口径；")
    lines.append("  Hindsight/Exabase 在官方 BEAM 上的 SOTA（64.1%）为不同基准，不可直接比较。")
    lines.append("- LoCoMo 为中文 50 题本地集（locomo_test_set.json），与公开 LoCoMo 英文集口径不同。")
    lines.append("- 如需对外宣称，建议后续跑官方 LongMemEval / BEAM 同口径（P2 待办）。")
    lines.append("")

    with open(args.output, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"leaderboard written: {args.output} ({len(lines)} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
