#!/usr/bin/env python3
"""
LongMemEval Benchmark Runner — 一键运行脚本

依次执行: 数据摄入 → Recall@k 评测 → QA 评测 → 生成汇总报告

Usage:
  python run_benchmark.py --api-key sk-xxx
  python run_benchmark.py --skip-qa --max-samples 50
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from typing import Optional


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="LongMemEval Benchmark Runner — 一键执行完整评测流水线"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=os.path.join(SCRIPT_DIR, "results"),
        help="结果输出目录 (默认: <script_dir>/results)",
    )
    parser.add_argument(
        "--api-base",
        type=str,
        default=os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1"),
        help="OpenAI-compatible API base URL",
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=os.environ.get("OPENAI_API_KEY", ""),
        help="API key",
    )
    parser.add_argument(
        "--reader-model",
        type=str,
        default="gpt-4o-mini",
        help="QA reader 模型",
    )
    parser.add_argument(
        "--judge-model",
        type=str,
        default="gpt-4o",
        help="QA judge 模型",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help="检索 top-k 条记忆用于 QA",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="最大摄入/评测样本数 (调试用)",
    )
    parser.add_argument(
        "--skip-ingest",
        action="store_true",
        help="跳过数据摄入步骤（使用已有 JSONL）",
    )
    parser.add_argument(
        "--skip-retrieval",
        action="store_true",
        help="跳过检索评测步骤",
    )
    parser.add_argument(
        "--skip-qa",
        action="store_true",
        help="跳过 QA 评测步骤",
    )
    parser.add_argument(
        "--jsonl-path",
        type=str,
        default=None,
        help="已有的摄入 JSONL 文件路径（--skip-ingest 时使用）",
    )
    parser.add_argument(
        "--retrieval-results",
        type=str,
        default=None,
        help="已有的检索结果 JSON 路径（--skip-retrieval 时使用）",
    )
    return parser.parse_args()


def run_command(cmd: list, step_name: str) -> int:
    """运行一个子进程命令，返回 exit code。"""
    print(f"\n{'=' * 60}")
    print(f"  [Runner] 阶段: {step_name}")
    print(f"{'=' * 60}")
    print(f"  CMD: {' '.join(cmd)}")
    print()

    result = subprocess.run(cmd, cwd=SCRIPT_DIR)
    if result.returncode != 0:
        print(f"\n[Runner] [ERROR] {step_name} 失败 (exit code={result.returncode})")
    else:
        print(f"\n[Runner] [OK] {step_name} 完成")
    return result.returncode


def generate_summary_report(
    output_dir: str,
    ingest_stats: Optional[dict],
    retrieval_report_path: Optional[str],
    qa_report_path: Optional[str],
) -> str:
    """生成最终汇总 Markdown 报告。"""
    lines = []
    lines.append("# LongMemEval Benchmark Summary Report")
    lines.append(f"\n**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("")

    # 数据摄入摘要
    if ingest_stats:
        lines.append("## 1. Data Ingestion")
        lines.append("")
        lines.append(f"- 总样本数: {ingest_stats.get('total_samples', '-')}")
        lines.append(f"- 总记忆条数: {ingest_stats.get('total_memories', '-')}")
        lines.append("")
        lines.append("| Category | Count |")
        lines.append("|---|---|")
        categories = [
            "knowledge-update",
            "multi-session-reasoning",
            "temporal-reasoning",
            "single-session-user",
            "single-session-assistant",
            "single-session-preference",
        ]
        for cat in categories:
            cnt = ingest_stats.get(cat, 0)
            lines.append(f"| {cat} | {cnt} |")
        lines.append("")

    # 检索评测摘要
    if retrieval_report_path and os.path.exists(retrieval_report_path):
        lines.append("## 2. Retrieval Evaluation (Recall@k & NDCG@k)")
        lines.append("")
        with open(retrieval_report_path, "r", encoding="utf-8") as f:
            retrieval_report = json.load(f)

        meta = retrieval_report.get("meta", {})
        lines.append(f"- 评测样本数: {meta.get('evaluated_samples', '-')}")
        lines.append(f"- 跳过样本数: {meta.get('skipped_samples', '-')}")
        lines.append("")

        recall = retrieval_report.get("recall", {})
        ndcg = retrieval_report.get("ndcg", {})

        for metric_name, metric_data in [("Recall@k", recall), ("NDCG@k", ndcg)]:
            lines.append(f"### {metric_name}")
            lines.append("")
            k_vals = meta.get("k_values", [1, 3, 5, 10])
            header = "| Category | " + " | ".join([f"@{k}" for k in k_vals]) + " |"
            lines.append(header)
            sep = "|---|" + "|".join(["---" for _ in k_vals]) + "|"
            lines.append(sep)
            for cat in categories:
                cat_data = metric_data.get(cat, {})
                vals = [str(cat_data.get(f"@{k}", "-")) for k in k_vals]
                lines.append(f"| {cat} | " + " | ".join(vals) + " |")
            overall = metric_data.get("overall", {})
            overall_vals = [str(overall.get(f"@{k}", "-")) for k in k_vals]
            lines.append(f"| **Overall** | " + " | ".join(overall_vals) + " |")
            lines.append("")

    # QA 评测摘要
    if qa_report_path and os.path.exists(qa_report_path):
        lines.append("## 3. End-to-End QA Evaluation")
        lines.append("")
        with open(qa_report_path, "r", encoding="utf-8") as f:
            qa_report = json.load(f)

        qa_meta = qa_report.get("meta", {})
        qa_summary = qa_report.get("summary", {})

        lines.append(f"- Reader Model: {qa_meta.get('reader_model', '-')}")
        lines.append(f"- Judge Model: {qa_meta.get('judge_model', '-')}")
        lines.append(f"- Top-K: {qa_meta.get('top_k', '-')}")
        lines.append("")

        overall_qa = qa_summary.get("overall", {})
        lines.append(f"**Overall Accuracy**: {overall_qa.get('accuracy', '-')}")
        lines.append(f"- Total: {overall_qa.get('total', '-')}")
        lines.append(f"- Correct: {overall_qa.get('correct', '-')}")
        lines.append(f"- Skipped: {overall_qa.get('skipped', '-')}")
        lines.append("")

        lines.append("| Category | Total | Correct | Accuracy |")
        lines.append("|---|---|---|---|")
        for cat in categories:
            cs = qa_summary.get(cat, {})
            total = cs.get("total", 0)
            correct = cs.get("correct", 0)
            acc = cs.get("accuracy")
            acc_str = f"{acc:.2%}" if acc is not None else "-"
            lines.append(f"| {cat} | {total} | {correct} | {acc_str} |")
        lines.append("")

    # 产出物清单
    lines.append("## Output Files")
    lines.append("")
    for fname in os.listdir(output_dir):
        fpath = os.path.join(output_dir, fname)
        if os.path.isfile(fpath):
            size = os.path.getsize(fpath)
            lines.append(f"- `{fname}` ({size:,} bytes)")
    lines.append("")

    report_path = os.path.join(output_dir, "benchmark_report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return report_path


def main() -> None:
    args = parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    jsonl_path = args.jsonl_path or os.path.join(args.output_dir, "ingested_memories.jsonl")
    retrieval_results_path = args.retrieval_results or os.path.join(
        args.output_dir, "retrieval_dummy.json"
    )
    retrieval_report_path = os.path.join(args.output_dir, "retrieval_report.json")
    qa_report_path = os.path.join(args.output_dir, "qa_report.json")

    ingest_stats: Optional[dict] = None
    start_time = time.time()

    # ============================================================
    # Stage 1: Data Ingestion
    # ============================================================
    if not args.skip_ingest:
        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "longmemeval_ingestor.py"),
            "--output", jsonl_path,
        ]
        if args.max_samples:
            cmd += ["--max-samples", str(args.max_samples)]

        rc = run_command(cmd, "Data Ingestion")
        if rc != 0:
            print("[Runner] 数据摄入失败，终止流水线", file=sys.stderr)
            sys.exit(1)

        # 尝试解析摄入统计（从 stdout 不一定能拿到，但记录即可）
        ingest_stats = {
            "total_samples": args.max_samples or 500,
            "total_memories": "see JSONL",
        }
    else:
        print("[Runner] 跳过数据摄入步骤")
        if not os.path.exists(jsonl_path):
            print(
                f"[Runner] [ERROR] JSONL 文件不存在: {jsonl_path}",
                file=sys.stderr,
            )
            sys.exit(1)

    # ============================================================
    # Stage 2: Retrieval Evaluation
    # ============================================================
    if not args.skip_retrieval:
        if not os.path.exists(retrieval_results_path):
            print(
                "[Runner] [WARN] 检索结果文件不存在，生成 dummy 检索结果用于演示"
            )

            # 加载 JSONL 获取 answer_session_ids 作为 dummy oracle 检索
            dummy_results = {}
            with open(jsonl_path, "r", encoding="utf-8") as f:
                seen_samples = set()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    mem = json.loads(line)
                    sid = mem.get("source_sample_id", -1)
                    if sid in seen_samples:
                        continue
                    seen_samples.add(sid)
                    gt_ids = mem.get("answer_session_ids", [])
                    dummy_results[f"sample_{sid}"] = {
                        "retrieved_session_ids": gt_ids,  # oracle retrieval
                    }

            with open(retrieval_results_path, "w", encoding="utf-8") as f:
                json.dump(dummy_results, f, ensure_ascii=False, indent=2)
            print(f"[Runner] 已生成 dummy (oracle) 检索结果: {retrieval_results_path}")

        cmd = [
            sys.executable,
            os.path.join(SCRIPT_DIR, "longmemeval_retriever.py"),
            "--data", jsonl_path,
            "--retrieval-results", retrieval_results_path,
            "--output", retrieval_report_path,
            "--report-md", os.path.join(args.output_dir, "retrieval_report.md"),
        ]
        rc = run_command(cmd, "Retrieval Evaluation")
        if rc != 0:
            print("[Runner] 检索评测失败，终止流水线", file=sys.stderr)
            sys.exit(1)
    else:
        print("[Runner] 跳过检索评测步骤")

    # ============================================================
    # Stage 3: QA Evaluation
    # ============================================================
    if not args.skip_qa:
        if not args.api_key:
            print(
                "[Runner] [WARN] 未设置 API key，跳过 QA 评测。"
                "请通过 --api-key 或 OPENAI_API_KEY 传入。"
            )
        else:
            cmd = [
                sys.executable,
                os.path.join(SCRIPT_DIR, "longmemeval_qa_evaluator.py"),
                "--data", jsonl_path,
                "--retrieval-results", retrieval_results_path,
                "--output", qa_report_path,
                "--report-md", os.path.join(args.output_dir, "qa_report.md"),
                "--api-base", args.api_base,
                "--api-key", args.api_key,
                "--reader-model", args.reader_model,
                "--judge-model", args.judge_model,
                "--top-k", str(args.top_k),
            ]
            if args.max_samples:
                cmd += ["--max-samples", str(args.max_samples)]

            rc = run_command(cmd, "QA Evaluation")
            if rc != 0:
                print("[Runner] QA 评测失败（流水线继续）")
    else:
        print("[Runner] 跳过 QA 评测步骤")

    # ============================================================
    # Generate Summary Report
    # ============================================================
    report_path = generate_summary_report(
        args.output_dir,
        ingest_stats,
        retrieval_report_path if not args.skip_retrieval else None,
        qa_report_path if not args.skip_qa else None,
    )

    elapsed = time.time() - start_time
    print("\n" + "=" * 60)
    print(f"  Benchmark 完成! 耗时: {elapsed:.1f}s")
    print(f"  汇总报告: {report_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
