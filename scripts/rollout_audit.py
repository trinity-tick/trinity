# -*- coding: utf-8 -*-
"""rollout_audit.py — rollout 轨迹异常检测（2026-08-27）。

扫描 automation rollouts JSONL，统计失败模式（ok=false/exit_code!=0/error_tail），
有异常时 emit automation.failed（告警规则响应）并输出报告。

用法:
    python scripts/rollout_audit.py [--days 7] [--no-emit]
"""
import json
import os
import sys
import glob
import argparse
from datetime import datetime, timedelta

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

_ROLLOUT_DIR = os.path.expanduser("~/.trinity/automation/rollouts")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-emit", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(_ROLLOUT_DIR):
        print("no rollouts dir yet (automation 启用后产生)")
        return 0

    since = (datetime.now() - timedelta(days=args.days)).strftime("%Y%m%d")
    files = [f for f in sorted(glob.glob(os.path.join(_ROLLOUT_DIR, "*.jsonl")))
             if os.path.basename(f)[:8] >= since]
    total = failed = 0
    fail_details = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    failed += 1
                    fail_details.append({"file": os.path.basename(f), "parse_error": line[:80]})
                    continue
                total += 1
                if rec.get("ok") is False or rec.get("exit_code") not in (None, 0):
                    failed += 1
                    fail_details.append({
                        "file": os.path.basename(f),
                        "rule": rec.get("rule"),
                        "action_type": rec.get("action_type"),
                        "exit_code": rec.get("exit_code"),
                        "error_tail": str(rec.get("error_tail", ""))[:100],
                    })
    print(f"rollouts: {len(files)} files / {total} actions / {failed} failures ({round(failed/max(1,total)*100,1)}%)")
    for d in fail_details[:5]:
        print("  FAIL:", d)
    if failed > 0 and not args.no_emit:
        sys.path.insert(0, _TRINITY_ROOT)
        from trinity.automation import emit
        emit("automation.failed", {
            "rule": "rollout-audit",
            "trigger": "scheduled",
            "error": f"rollout audit: {failed}/{total} actions failed in last {args.days} days",
        })
        print("emitted automation.failed")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
