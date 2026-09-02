#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场供给自动化（2026-09-01，生态启动）：高价值记忆自动上架。

从 PG 主库挑选高价值记忆（decision/insight/milestone 类 + importance>=0.8），
按 agent 分组去重后上架记忆市场（已上架的不重复挂单）。

用法: python scripts/market_list_high_value.py [--top 20] [--dry-run]
输出: 上架/跳过/失败计数；market 状态写 ~/.trinity/memory_market_*.json（API 同源）
"""
import argparse
import json
import os
import sys
import time
import urllib.request


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20, help="候选记忆数（按 importance DESC）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
        connect_timeout=8)
    cur = conn.cursor()
    cur.execute("""
        SELECT memory_id, COALESCE(metadata->>'category', category), importance,
               COALESCE(metadata->>'agent_id', 'default')
        FROM memories
        WHERE status='active'
          AND COALESCE(metadata->>'category', category) IN ('decision','insight','milestone','summary','kb_harvested','knowledge','wms_knowledge')
          AND (importance::float8) >= 0.8
        ORDER BY importance DESC, created_at DESC
        LIMIT %s
    """, (args.top,))
    rows = cur.fetchall()
    conn.close()
    print("candidates: %d" % len(rows))

    # 已上架清单（去重）
    listed = set()
    _ob_path = os.path.join(os.path.expanduser("~/.trinity"), "memory_market_orderbook.json")
    if os.path.exists(_ob_path):
        try:
            _data = json.load(open(_ob_path, encoding="utf-8"))
            for _aid, _d in (_data or {}).items():
                _m = (_d.get("asset") or {}).get("memory_id") or ""
                if _m:
                    listed.add(_m)
        except Exception:
            pass

    done = skipped = failed = 0
    for mid, cat, imp, agent in rows:
        if mid in listed:
            skipped += 1
            continue
        if args.dry_run:
            print("  [dry] %s (%s, imp=%s, owner=%s)" % (mid[:16], cat, imp, agent))
            done += 1
            continue
        payload = {"memory": {"memory_id": mid, "modality": cat, "importance": float(imp)},
                   "owner": agent or "default", "price": 0.0, "license": "CC-BY"}
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8001/market/list",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=15)
            body = json.loads(resp.read().decode())
            if body.get("status") == "listed":
                done += 1
                print("  listed %s -> %s" % (mid[:16], body.get("asset_id")))
            else:
                failed += 1
                print("  skip %s: %s" % (mid[:16], body))
        except Exception as e:
            failed += 1
            print("  FAIL %s: %s" % (mid[:16], str(e)[:100]))
        time.sleep(0.3)

    print("MARKET-LIST: candidates=%d listed=%d skipped=%d failed=%d%s"
          % (len(rows), done, skipped, failed, " (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
