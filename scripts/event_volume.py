#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件水位（2026-09-01，事件驱动巩固触发器）：输出近 N 小时 dsh_events 增量。

用法: python scripts/event_volume.py [--hours 1]
输出: "EVENTS last N h: <count>"；退出码 0。
"""
import argparse
import os
import sqlite3
import sys
import time


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=1.0)
    args = ap.parse_args()
    db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")
    conn = sqlite3.connect(db, timeout=20)
    cutoff_ms = time.time() * 1000 - args.hours * 3600 * 1000
    n = conn.execute("SELECT COUNT(*) FROM dsh_events WHERE time > ?", (cutoff_ms,)).fetchone()[0]
    conn.close()
    print("EVENTS last %.1fh: %d" % (args.hours, n))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
