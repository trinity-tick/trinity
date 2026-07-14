#!/usr/bin/env python3
"""
一键运行 — run_latency_bench.py

串联：单模块 profiling → 端到端延迟 → 并发压力测试，
生成汇总报告 latency_report.md

用法:
    python run_latency_bench.py
    python run_latency_bench.py --trinity-path ./trinity
    python run_latency_bench.py --skip-concurrency
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime

# 确保 benchmark 包在路径中
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from latency_profiler import Profiler, ProfilerResult, results_to_json, results_to_markdown
from trinity_profiler import run_all_profiles, get_registry
from concurrency_bench import (
    ConcurrencyResult,
    run_all_concurrency_levels,
    default_trinity_stub_query,
)


# ──────────────────────────────────────────────────────────────
# 汇总报告生成
# ──────────────────────────────────────────────────────────────


def generate_report(
    profile_results: list,
    concurrency_results: list,
    output_path: str,
    trinity_path: str = "",
    total_elapsed_s: float = 0.0,
) -> str:
    """生成 Markdown 汇总报告。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        "# Trinity 三位一体系统 — 延迟与吞吐量 Benchmark 报告",
        "",
        f"**生成时间**: {now}",
        f"**Trinity 路径**: `{trinity_path or '(stub 模式)'}`",
        f"**总耗时**: {total_elapsed_s:.1f}s",
        "",
        "---",
        "",
        "## 1. 单项模块延迟 Profiling",
        "",
        ProfilerResult.markdown_header(),
    ]

    for r in profile_results:
        lines.append(r.to_markdown_row())

    lines += [
        "",
        "---",
        "",
        "## 2. 并发压力测试",
        "",
        "| 并发数 | 总请求 | QPS | P50(ms) | P95(ms) | P99(ms) | P99.9(ms) | Mean(ms) | 内存峰值(MB) | 错误数 |",
        "|--------|--------|-----|---------|---------|---------|-----------|----------|-------------|--------|",
    ]

    for r in concurrency_results:
        lines.append(
            f"| {r.concurrency} | {r.total_requests} | {r.qps:.1f} | "
            f"{r.p50_ms:.2f} | {r.p95_ms:.2f} | {r.p99_ms:.2f} | "
            f"{r.p99_9_ms:.2f} | {r.mean_ms:.2f} | {r.peak_memory_mb:.1f} | {r.errors} |"
        )

    # 关键发现
    lines += [
        "",
        "---",
        "",
        "## 3. 关键发现与优化建议",
        "",
    ]

    if profile_results:
        # 找出最大的端到端延迟
        e2e = [r for r in profile_results if "E2E" in r.name or "端到端" in r.name]
        if e2e:
            lines.append(f"- **端到端延迟**: P50={e2e[0].p50:.2f}ms, P99={e2e[0].p99:.2f}ms")
            if e2e[0].p99 > 100:
                lines.append("  - ⚠️ P99 延迟偏高，建议排查长尾来源 (GC? 锁竞争?)")

        # 找出瓶颈模块 (P50 最大的前 3)
        sorted_by_p50 = sorted(profile_results, key=lambda r: r.p50, reverse=True)
        lines.append("")
        lines.append("### Top 3 延迟瓶颈模块")
        for r in sorted_by_p50[:3]:
            lines.append(f"- **{r.name}**: P50={r.p50:.2f}ms, P99={r.p99:.2f}ms")

    if concurrency_results:
        lines.append("")
        lines.append("### 并发表现")
        max_c = concurrency_results[-1]
        lines.append(f"- 最高并发 {max_c.concurrency} 时 QPS = {max_c.qps:.1f}")
        if len(concurrency_results) >= 2:
            # 检查 QPS 是否随并发线性增长
            first = concurrency_results[0]
            ideal_qps = first.qps * (max_c.concurrency / first.concurrency)
            efficiency = max_c.qps / ideal_qps * 100 if ideal_qps > 0 else 0
            lines.append(f"- 并发效率 (vs 理想线性): {efficiency:.1f}%")
            if efficiency < 60:
                lines.append("  - ⚠️ 高并发下效率衰减明显，可能存在锁竞争或资源瓶颈")

    lines += [
        "",
        "---",
        "",
        "## 4. 输出文件清单",
        "",
        f"- 单项 Profiling JSON: `trinity_profile_results.json`",
        f"- 单项 Profiling MD: `trinity_profile_results.md`",
        f"- 并发测试 CSV: `concurrency_results.csv`",
        f"- 并发测试 JSON: `concurrency_results.json`",
        f"- 汇总报告: `latency_report.md`",
        "",
    ]

    report = "\n".join(lines)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(report)

    return report


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trinity 延迟与吞吐量一键 Benchmark",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--trinity-path",
        default="",
        help="Trinity 代码路径 (用于对接真实代码，stub 模式下忽略)",
    )
    parser.add_argument(
        "--skip-concurrency",
        action="store_true",
        help="跳过并发压力测试",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="报告输出目录 (默认: 当前脚本所在目录)",
    )
    parser.add_argument(
        "--concurrency-levels",
        type=int,
        nargs="+",
        default=[10, 50, 100, 200],
        help="并发梯度 (默认 10 50 100 200)",
    )
    parser.add_argument(
        "--concurrency-requests",
        type=int,
        default=300,
        help="每档位请求数 (默认 300)",
    )
    args = parser.parse_args()

    output_dir = args.output_dir or _SCRIPT_DIR
    os.makedirs(output_dir, exist_ok=True)

    print("=" * 60)
    print("  Trinity 延迟与吞吐量 Benchmark")
    print(f"  Trinity 路径: {args.trinity_path or '(stub 模式)'}")
    print(f"  输出目录: {output_dir}")
    print("=" * 60)

    t_overall_start = time.perf_counter()
    profile_results: list = []
    concurrency_results: list = []

    # ── 阶段 1: 单项模块 Profiling ───────────────────────
    print("\n" + "=" * 60)
    print("  阶段 1/2: 单项模块延迟 Profiling")
    print("=" * 60)

    try:
        profile_results = run_all_profiles()
    except Exception as exc:
        print(f"[ERROR] 单项 profiling 失败: {exc}")

    # 保存
    if profile_results:
        json_path = os.path.join(output_dir, "trinity_profile_results.json")
        md_path = os.path.join(output_dir, "trinity_profile_results.md")
        with open(json_path, "w", encoding="utf-8") as f:
            f.write(results_to_json(profile_results))
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(results_to_markdown(profile_results, "Trinity 单项模块延迟 Profiling"))
        print(f"\n[OK] Profiling JSON → {json_path}")
        print(f"[OK] Profiling MD   → {md_path}")

    # ── 阶段 2: 并发压力测试 ─────────────────────────────
    if not args.skip_concurrency:
        print("\n" + "=" * 60)
        print("  阶段 2/2: 并发压力测试")
        print("=" * 60)

        try:
            concurrency_results = asyncio.run(
                run_all_concurrency_levels(
                    task_fn=default_trinity_stub_query,
                    levels=args.concurrency_levels,
                    requests_per_level=args.concurrency_requests,
                    output_csv=os.path.join(output_dir, "concurrency_results.csv"),
                    output_json=os.path.join(output_dir, "concurrency_results.json"),
                )
            )
        except Exception as exc:
            print(f"[ERROR] 并发测试失败: {exc}")
    else:
        print("\n[SKIP] 跳过并发压力测试")

    # ── 生成汇总报告 ────────────────────────────────────
    t_overall_end = time.perf_counter()

    print("\n" + "=" * 60)
    print("  生成汇总报告")
    print("=" * 60)

    report_path = os.path.join(output_dir, "latency_report.md")
    report = generate_report(
        profile_results=profile_results,
        concurrency_results=concurrency_results,
        output_path=report_path,
        trinity_path=args.trinity_path,
        total_elapsed_s=t_overall_end - t_overall_start,
    )

    print(report)

    print(f"\n{'='*60}")
    print(f"  完成！报告: {report_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
