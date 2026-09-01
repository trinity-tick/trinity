#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PG vs SQLite 对账（2026-09-01，自查短板 #2 第一步——只读，不写任何库）

背景：PG 主存储 33,186 条 vs SQLite 镜像 28,032 条（分叉 5k+），聚合池独立漂移。
本工具输出三份差异事实，供收敛决策：
  1. 总数对比（total / active / archived）
  2. 集合差：PG-only / SQLite-only（按 memory_id）
  3. 共有的 active 记忆里 content 哈希不一致数（按 sha256_hash 或内容首段）

退出码恒 0（信息型）；输出 JSON 报告。
用法: python scripts/reconcile_pg_sqlite.py [--json] [--limit 20]
"""
import argparse
import hashlib
import json
import os
import sys


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    # ── SQLite（C: 权威路径与 maintenance 一致）──
    import sqlite3
    sq_db = os.environ.get("TRINITY_STORE_DB") or os.path.expanduser("~/.trinity/store/trinity_store.db")
    sq = sqlite3.connect(sq_db, timeout=30)
    sq.row_factory = sqlite3.Row
    sq_tot = sq.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    sq_act = sq.execute("SELECT COUNT(*) FROM memories WHERE status='active'").fetchone()[0]
    sq_arch = sq.execute("SELECT COUNT(*) FROM memories WHERE status='archived'").fetchone()[0]
    sq_ids = set(r["memory_id"] for r in sq.execute("SELECT memory_id FROM memories"))
    sq_active_ids = set(r["memory_id"] for r in sq.execute("SELECT memory_id FROM memories WHERE status='active'"))
    sq_hash = {r["memory_id"]: r["sha256_hash"] for r in
               sq.execute("SELECT memory_id, sha256_hash FROM memories WHERE status='active' AND sha256_hash IS NOT NULL")}
    sq.close()

    # ── PG ──
    try:
        import psycopg2
    except Exception as e:
        print(json.dumps({"error": "psycopg2 unavailable: %s" % e}, ensure_ascii=False))
        return 0
    pg = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
                          port=os.environ.get("TRINITY_PG_PORT", "5432"),
                          dbname="trinity", user=os.environ.get("TRINITY_PG_USER", "trinity"),
                          password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"))
    cur = pg.cursor()
    pg_tot = cur.execute("SELECT COUNT(*) FROM memories"); pg_tot = cur.fetchone()[0]
    pg_act = cur.execute("SELECT COUNT(*) FROM memories WHERE status='active'"); pg_act = cur.fetchone()[0]
    pg_arch = cur.execute("SELECT COUNT(*) FROM memories WHERE status='archived'"); pg_arch = cur.fetchone()[0]
    cur.execute("SELECT memory_id FROM memories")
    pg_ids = set(r[0] for r in cur.fetchall())
    cur.execute("SELECT memory_id FROM memories WHERE status='active'")
    pg_active_ids = set(r[0] for r in cur.fetchall())
    cur.execute("SELECT memory_id, sha256_hash FROM memories WHERE status='active' AND sha256_hash IS NOT NULL")
    pg_hash = {r[0]: r[1] for r in cur.fetchall()}
    pg.close()

    pg_only = sorted(x for x in (pg_ids - sq_ids) if x is not None)
    sq_only = sorted(x for x in (sq_ids - pg_ids) if x is not None)
    shared_active = {x for x in (sq_active_ids & pg_active_ids) if x is not None}
    hash_mismatch = [mid for mid in shared_active
                     if sq_hash.get(mid) and pg_hash.get(mid) and sq_hash[mid] != pg_hash[mid]]
    pg_only_active = sorted(pg_active_ids - sq_active_ids)

    report = {
        "sqlite": {"db": sq_db, "total": sq_tot, "active": sq_act, "archived": sq_arch},
        "pg": {"total": pg_tot, "active": pg_act, "archived": pg_arch},
        "diff": {
            "pg_only": len(pg_only), "sq_only": len(sq_only),
            "pg_only_active": len(pg_only_active),
            "shared_active_hash_mismatch": len(hash_mismatch),
        },
        "samples": {
            "pg_only": pg_only[:args.limit],
            "sq_only": sq_only[:args.limit],
            "pg_only_active": pg_only_active[:args.limit],
            "hash_mismatch": hash_mismatch[:args.limit],
        },
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print("RECONCILE: sqlite total=%d active=%d archived=%d | pg total=%d active=%d archived=%d"
              % (sq_tot, sq_act, sq_arch, pg_tot, pg_act, pg_arch))
        print("RECONCILE: pg_only=%d sq_only=%d pg_only_active=%d hash_mismatch(active)=%d"
              % (len(pg_only), len(sq_only), len(pg_only_active), len(hash_mismatch)))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
