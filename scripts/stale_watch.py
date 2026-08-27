# -*- coding: utf-8 -*-
"""stale_watch.py — stale 观察（2026-08-27）：stale 数/最旧源/预计自然触发日期。

用法:
    python scripts/stale_watch.py
"""
import os
import sys
import datetime

_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

STALE_DAYS = 30.0


def main() -> int:
    from trinity.knowledge import sources
    reg = sources()
    ss = reg.get("sources", [])
    stale = [s for s in ss if s.get("stale")]
    oldest = sorted(ss, key=lambda x: -x.get("freshness_days", 0))[:3]
    print(f"知识源: {reg.get('total')} | stale: {len(stale)}")
    today = datetime.date.today()
    for s in oldest:
        d = s.get("freshness_days", 0)
        due = today + datetime.timedelta(days=max(0, STALE_DAYS - d))
        print(f"  oldest: {s['source_id'][:48]} {d:.1f}d | 预计 {due} 自然过期触发")
    if stale:
        print(f"⚠ {len(stale)} 个 stale 源——自动化应已触发重新摄入（检查 automation stats）")
    else:
        next_due = today + datetime.timedelta(days=max(0, STALE_DAYS - oldest[0].get("freshness_days", 0))) if oldest else None
        print(f"无 stale 源；最近预计触发: {next_due}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
