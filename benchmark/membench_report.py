# -*- coding: utf-8 -*-
"""MemBench 结果归一化 (A1.3) — 把各套件输出统一为 {suite, metric, value} 表格。

用法:
    python benchmark/membench_report.py [--results-dir DIR] [--out OUT]
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime


def load_latency(dirp: str) -> dict:
    """从 latency_report.md 正则提取端到端 P50/P99 与并发 QPS。"""
    md_path = os.path.join(dirp, "latency_report.md")
    out = {}
    if os.path.exists(md_path):
        text = open(md_path, encoding="utf-8", errors="ignore").read()
        m = re.search(r"E2E_trinity_query.*?\| ([\d.]+) \| ([\d.]+)", text)
        if m:
            out["e2e_p50_ms"] = float(m.group(1))
            out["e2e_p99_ms"] = float(m.group(2))
        m = re.search(r"200 \| 300 \| ([\d.]+) \|", text)
        if m:
            out["qps_at_200"] = float(m.group(1))
    return out


def load_concurrency(dirp: str) -> dict:
    path = os.path.join(dirp, "concurrency_results.json")
    if not os.path.exists(path):
        return {}
    rows = json.load(open(path, encoding="utf-8"))
    if not rows:
        return {}
    last = max(rows, key=lambda r: r.get("concurrency", 0))
    return {
        "max_qps": last.get("qps"),
        "p50_ms": last.get("p50_ms"),
        "p99_ms": last.get("p99_ms"),
        "errors": last.get("errors"),
        "concurrency": last.get("concurrency"),
    }


def load_memsyco(dirp: str) -> dict:
    path = os.path.join(dirp, "memsyco_report.json")
    if not os.path.exists(path):
        return {}
    d = json.load(open(path, encoding="utf-8"))
    keys = ("composite_score", "sycophancy_rate", "objective_accuracy")
    return {k: d.get(k) for k in keys if k in d}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results-dir", default=r"C:\Users\Administrator\.trinity\bench-results\20260814_v2baseline")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    rows = []  # (suite, metric, value, unit)

    for k, v in load_latency(args.results_dir).items():
        rows.append(("latency", k, v, "ms" if "ms" in k else "qps"))
    for k, v in load_concurrency(args.results_dir).items():
        rows.append(("concurrency", k, v, ""))
    for k, v in load_memsyco(args.results_dir).items():
        rows.append(("memsyco", k, v, ""))

    summary = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "results_dir": args.results_dir,
        "suites": sorted({r[0] for r in rows}),
        "metrics": [{"suite": s, "metric": m, "value": v, "unit": u} for s, m, v, u in rows],
    }

    out_path = args.out or os.path.join(args.results_dir, "membench_summary.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    md = ["# MemBench Summary (normalized)", "",
          f"- generated_at: {summary['generated_at']}", ""]
    md.append("| suite | metric | value | unit |")
    md.append("|---|---|---|---|")
    for r in summary["metrics"]:
        md.append(f"| {r['suite']} | {r['metric']} | {r['value']} | {r['unit']} |")
    md_path = out_path.replace(".json", ".md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    print(f"normalized {len(rows)} metrics -> {out_path}")
    print("\n".join(md))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
