#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_evals.py — DSH 借鉴的断言式评测回归入口（脚本模式，避免 -m namespace 坑）。

用法:
    python scripts/run_evals.py --list
    python scripts/run_evals.py --all
    python scripts/run_evals.py --task search-schema
"""
import argparse
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


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
            import json
            print(json.dumps(result, ensure_ascii=False, indent=1))
            return 0 if result["ok"] else 1
        print(f"{'PASS' if result['ok'] else 'FAIL'} {result['name']}: {result['detail']}")
        for a in result["assertions"]:
            print(f"    {'ok' if a['ok'] else 'XX'} {a['detail']}")
        return 0 if result["ok"] else 1

    summary = run_all()
    if args.json:
        import json
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
