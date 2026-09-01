#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""结构事件水位检查 (2026-09-01, 自查 P1b)

读 ~/.trinity/store/trinity_store.db 的 dsh_events MAX(time)（epoch ms），
超过 --stale-hours（默认 24）没有新事件则打印 STALE 并 exit 1（supervisor 据此 WARN）；
正常打印 OK 并 exit 0（supervisor 静默，不刷日志）。
"""
import os
import sqlite3
import sys
import time

STALE_HOURS = 24.0


def main() -> int:
    db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser(
        "~/.trinity/store/trinity_store.db")
    try:
        conn = sqlite3.connect(db, timeout=10)
        try:
            row = conn.execute("SELECT MAX(time), COUNT(*) FROM dsh_events").fetchone()
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print("STALE: dsh_events unreadable: %s" % e)
        return 1
    mx, n = (row[0], row[1]) if row else (None, 0)
    if not mx:
        print("STALE: dsh_events empty")
        return 1
    age_h = (time.time() * 1000 - float(mx)) / 3600000.0
    if age_h > STALE_HOURS:
        print("STALE: dsh_events last event %.1fh ago (rows=%d) — 插件 structure_sync 疑似断流" % (age_h, n))
        return 1
    print("OK: dsh_events last event %.1fh ago (rows=%d)" % (age_h, n))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
