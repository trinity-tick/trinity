# -*- coding: utf-8 -*-
"""C3 榜单提交校验器 — 校验 submissions/*.json 格式与数值范围（防作弊前置）。

用法:
    python benchmark/leaderboard/validate.py                      # 校验全部提交
    python benchmark/leaderboard/validate.py --file <path>        # 校验单个
"""
import argparse
import json
import os
import sys

ALLOWED_SUITES = {"latency", "concurrency", "squad", "locomo", "memsyco", "compress", "longmemeval", "custom"}
RANGES = {
    "e2e_p50_ms": (0, 60000), "e2e_p99_ms": (0, 120000),
    "max_qps": (0, 1000000), "p50_ms": (0, 60000), "errors": (0, 100000),
    "r_at_5": (0, 1), "recall_at_5_session": (0, 1),
    "composite_judge": (0, 1), "sycophancy_rate": (0, 1), "token_savings_pct": (0, 1),
}

SUBMISSIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "submissions")


def validate(sub: dict) -> list:
    errs = []
    if not isinstance(sub.get("submitter"), str) or not sub["submitter"].strip():
        errs.append("submitter 缺失")
    if not isinstance(sub.get("results"), list) or not sub["results"]:
        errs.append("results 为空")
    seen = set()
    for r in sub.get("results", []):
        suite, metric = r.get("suite"), r.get("metric")
        value = r.get("value")
        key = f"{suite}:{metric}"
        if suite not in ALLOWED_SUITES:
            errs.append(f"未知 suite: {suite}")
        if key in seen:
            errs.append(f"重复指标: {key}")
        seen.add(key)
        if not isinstance(value, (int, float)):
            errs.append(f"{key} value 非数值: {value!r}")
        elif metric in RANGES:
            lo, hi = RANGES[metric]
            if not (lo <= value <= hi):
                errs.append(f"{key} 值越界: {value} 不在 [{lo},{hi}]")
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="")
    args = ap.parse_args()
    files = [args.file] if args.file else [
        os.path.join(SUBMISSIONS_DIR, f) for f in sorted(os.listdir(SUBMISSIONS_DIR))
        if f.endswith(".json")
    ]
    total_err = 0
    for f in files:
        if not os.path.exists(f):
            print(f"[SKIP] {os.path.basename(f)}: 不存在")
            continue
        sub = json.load(open(f, encoding="utf-8"))
        errs = validate(sub)
        if errs:
            total_err += 1
            print(f"[FAIL] {os.path.basename(f)}: {'; '.join(errs)}")
        else:
            print(f"[PASS] {os.path.basename(f)} ({sub['submitter']}, {len(sub['results'])} metrics)")
    print(f"\n校验完成: {len(files)} 个提交, {total_err} 个失败")
    sys.exit(1 if total_err else 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
