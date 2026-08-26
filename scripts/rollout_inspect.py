# -*- coding: utf-8 -*-
"""rollout_inspect.py — 自动化执行轨迹回放查看工具（Codex 借鉴 Phase 2）。

读取 ~/.trinity/automation/rollouts/<date>.jsonl（每行一个动作事件），
支持按日期/规则/结果过滤与汇总统计。

用法:
    python scripts/rollout_inspect.py --summary                 # 汇总统计（近 7 天）
    python scripts/rollout_inspect.py --date 2026-08-26         # 指定日期
    python scripts/rollout_inspect.py --rule pagetree           # 按规则名过滤
    python scripts/rollout_inspect.py --failed                  # 只看失败
    python scripts/rollout_inspect.py --tail 20                 # 最近 20 条
    python scripts/rollout_inspect.py --json                    # 原始 JSON 输出
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

ROLLOUT_DIR = os.path.expanduser("~/.trinity/automation/rollouts")


def load_events(days: int = 7, date_filter: str = "") -> list:
    events = []
    pattern = os.path.join(ROLLOUT_DIR, "*.jsonl")
    for path in sorted(glob.glob(pattern), reverse=True):
        if date_filter and date_filter not in os.path.basename(path):
            continue
        try:
            # utf-8-sig：兼容 PowerShell Add-Content 产生的 BOM
            with open(path, "r", encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except Exception:
                        continue
        except Exception:
            continue
    events.sort(key=lambda e: e.get("ts", ""))
    return events[-days * 2000:] if days else events


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", action="store_true", help="汇总统计")
    ap.add_argument("--date", default="", help="日期过滤（如 2026-08-26）")
    ap.add_argument("--rule", default="", help="规则名过滤")
    ap.add_argument("--failed", action="store_true", help="只看失败")
    ap.add_argument("--tail", type=int, default=0, help="最近 N 条")
    ap.add_argument("--json", action="store_true", help="原始 JSON 输出")
    args = ap.parse_args()

    events = load_events(days=7, date_filter=args.date)
    if args.rule:
        events = [e for e in events if args.rule in (e.get("rule") or "")]
    if args.failed:
        events = [e for e in events if not e.get("ok")]
    if args.tail:
        events = events[-args.tail:]

    if args.json:
        print(json.dumps(events, ensure_ascii=False, indent=1))
        return 0

    if args.summary or not (args.date or args.rule or args.failed or args.tail):
        total = len(events)
        ok_n = sum(1 for e in events if e.get("ok"))
        failed = total - ok_n
        dur = [e.get("duration_ms", 0) for e in events]
        by_rule = Counter(e.get("rule") or "?" for e in events)
        by_action = Counter(e.get("action_type") or "?" for e in events)
        by_day = Counter((e.get("ts") or "")[:10] for e in events)
        print(f"== Automation Rollout 汇总（{total} 条事件）==")
        print(f"  成功 {ok_n} / 失败 {failed} / 成功率 {ok_n / max(1, total):.1%}")
        if dur:
            print(f"  平均耗时 {sum(dur) / len(dur):.0f}ms  max {max(dur):.0f}ms")
        print("  按规则:", dict(by_rule.most_common(8)))
        print("  按动作:", dict(by_action.most_common(5)))
        print("  按日期:", dict(sorted(by_day.items())[-7:]))
        if failed:
            print("  -- 失败样例（最近 5）--")
            for e in [x for x in events if not x.get("ok")][-5:]:
                print(f"    {e.get('ts')} [{e.get('rule')}] rc={e.get('exit_code')} "
                      f"{e.get('error_tail', '')[:80]}")
        return 0

    for e in events:
        flag = "OK " if e.get("ok") else "FAIL"
        print(f"{e.get('ts')} [{flag}] {e.get('rule'):40s} "
              f"{e.get('action_type'):14s} {e.get('duration_ms', 0):8.0f}ms "
              f"{' '.join(str(c) for c in (e.get('command') or []))[-70:]}")
        if not e.get("ok") and e.get("error_tail"):
            print(f"      error: {e.get('error_tail')[:160]}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
