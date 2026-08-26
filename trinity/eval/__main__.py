#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""python -m trinity.eval — DSH 借鉴的断言式评测回归（Phase 2）。

用法:
    python -m trinity.eval --list          # 列出任务
    python -m trinity.eval --all           # 跑全部任务
    python -m trinity.eval --task search-schema
    python -m trinity.eval --json          # JSON 输出
"""
import argparse
import json
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--task", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from trinity.eval.runner import DEFAULT_TASKS, run_all, run_task

    if args.list:
        for t in DEFAULT_TASKS:
            print(f"  {t['name']:24s} {t.get('description', '')}")
        return 0

    if args.task:
        task = next((t for t in DEFAULT_TASKS if t["name"] == args.task), None)
        if task is None:
            print(f"ERROR: unknown task {args.task!r} (see --list)")
            return 1
        result = run_task(task)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 0 if result["ok"] else 1
        print(f"{'PASS' if result['ok'] else 'FAIL'} {result['name']}: {result['detail']}")
        for a in result["assertions"]:
            print(f"    {'ok' if a['ok'] else 'XX'} {a['detail']}")
        return 0 if result["ok"] else 1

    summary = run_all()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0 if summary["failed"] == 0 else 1
    print(f"== Eval {summary['passed']}/{summary['total']} passed ==")
    for r in summary["results"]:
        flag = "PASS" if r["ok"] else "FAIL"
        print(f"  [{flag}] {r['name']:24s} {r['detail']}")
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
