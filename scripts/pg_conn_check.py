#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PG 连接水位（2026-09-01，连接耗尽事故后监控）：输出当前连接数与阈值对比。

用法: python scripts/pg_conn_check.py [--warn 150]
输出: "PGCONN: <count>/<max> (warn at <warn>)"
"""
import argparse
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--warn", type=int, default=150)
    args = ap.parse_args()
    import psycopg2
    try:
        c = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                             user=os.environ.get("TRINITY_PG_USER", "trinity"),
                             password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
                             connect_timeout=5)
        cur = c.cursor()
        cur.execute("SHOW max_connections")
        mx = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM pg_stat_activity")
        n = cur.fetchone()[0]
        c.close()
    except Exception as e:
        print("PGCONN: ERROR %s" % e)
        return 1
    flag = "HIGH" if n >= args.warn else "ok"
    print("PGCONN: %d/%s (warn at %d) %s" % (n, mx, args.warn, flag))
    return 1 if n >= args.warn else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
