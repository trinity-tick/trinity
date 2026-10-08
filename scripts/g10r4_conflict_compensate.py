#!/usr/bin/env python -X utf8
# -*- coding: utf-8 -*-
"""G10R4 / t136 **一次性冲突补偿脚本**（运维用；**默认 dry-run**）。

用途：把 `status='active' AND conflict_group_id IS NULL` 的行重跑 `_assign_conflicts`，
      补齐 G7C 崩溃窗口（`TRINITY_CONFLICT_ASYNC=on` 时队列只在内存）可能丢失的判定。

⚠️⚠️ **本脚本【不消除】那个窗口** —— 它只把「静默丢失」变成「可发现、可修复」。
       窗口内那一次判定**依然丢**；补偿发生在**事后**。

用法（**默认只看不写**）：
    # 1) 体检（只读）：有多少 NULL 行、多少"可疑"
    python scripts/g10r4_conflict_compensate.py --db <path> --report
    # 2) dry-run（默认）：会扫哪些行、预计改多少 —— **不写库**
    python scripts/g10r4_conflict_compensate.py --db <path> --limit 200
    # 3) 真跑（显式 --apply）：
    python scripts/g10r4_conflict_compensate.py --db <path> --limit 200 --apply
    # 4) 续跑：带上 --after-rowid <上次 last_rowid>
    python scripts/g10r4_conflict_compensate.py --db <path> --apply --after-rowid 130391

⭐ 安全设计：
  · **默认 dry-run**（不写）；写必须显式 `--apply`；
  · **默认限流 50 行/次**（`--limit 0` 才不限流）；
  · **`--db` 必须显式给**（**不默认为生产库** ⇒ 防误跑）；
  · 输出 `scanned/changed/no_change/errors/remaining_total`，**剩余量是全局真值**。
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# L1 静默失败治理（t162/G19）：**吞但计数** —— 与 docs/SILENT_FAILURE_BUDGETS.json 的 `_policy` 一致。
try:
    from trinity._swallow import swallow
except Exception:                      # 极早期/无 trinity 时退化为空操作（同 sqlite_pg_mirror.py 的写法）
    def swallow(*_a, **_k):            # type: ignore[misc]
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="G10R4 冲突判定补偿（默认 dry-run）")
    ap.add_argument("--db", required=True, help="SQLite 库路径（**必填**，不默认生产库）")
    ap.add_argument("--limit", type=int, default=50, help="单次最多扫多少行（0=不限流，危险）")
    ap.add_argument("--after-rowid", type=int, default=0, help="游标（续跑用）")
    ap.add_argument("--apply", action="store_true", help="⭐ 真的写库（默认 **不写**）")
    ap.add_argument("--report", action="store_true", help="只体检：报 NULL 计数后退出")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(json.dumps({"error": "db not found", "db": args.db}, ensure_ascii=False))
        return 2

    from trinity.adapters.sqlite import SQLiteAdapter
    ad = SQLiteAdapter(db_path=args.db)
    ad.connect()
    try:
        c = ad._conn
        null_total = int(c.execute(
            "SELECT count(*) FROM memories WHERE status='active' "
            "AND conflict_group_id IS NULL").fetchone()[0])
        all_total = int(c.execute("SELECT count(*) FROM memories").fetchone()[0])
        out = {"db": args.db, "timepoint": time.strftime("%Y-%m-%d %H:%M:%S"),
               "rows_total": all_total, "active_null_group": null_total,
               "apply": bool(args.apply), "limit": args.limit,
               "after_rowid": args.after_rowid}
        if args.report:
            out["mode"] = "report-only（未扫、未写）"
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0

        if not args.apply:
            # dry-run：只列出"会扫哪些行"，**不调用判定**（因此绝不写库）
            tg = ad._conflict_compensate_targets(limit=args.limit, after_rowid=args.after_rowid)
            out.update({"mode": "dry-run（未写库）", "would_scan": len(tg),
                        "would_scan_sample": [t["memory_id"] for t in tg[:5]],
                        "note": "⭐ 预计改多少**只有真跑才知道**（取决于 token 重叠是否 ≥ 阈值）"})
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 0

        res = ad.compensate_missing_conflicts(limit=args.limit, after_rowid=args.after_rowid)
        out.update({"mode": "apply（已写库）", **res})
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    finally:
        try:
            ad.disconnect()
        except Exception as _e:        # t162/G19：原为静默 `pass` ⇒ 改为"吞但计数"
            swallow(__name__ + ":disconnect", _e)


if __name__ == "__main__":
    sys.exit(main())
