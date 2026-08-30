#!/usr/bin/env python3
"""memory_purification.py — 主动遗忘净化闭环（2026-09，EXECUTION 105.15）

认知依据：大脑主动遗忘——污染清理（免疫式）、冲突消解、过时失效、
冗余修剪——记忆健康治理（对标 2026 Memory Survey 的 proactive forgetting）。

四类净化：
  1. duplicates  同 content_hash 多条 active → 保留最新/高价值，冗余归档
  2. conflicts   conflict_group 未消解 → 保留高 importance 版本，标记 resolved
  3. expired     ttl_seconds 到期仍 active → status=expired（复用 adapter 语义）
  4. isolated    注入隔离记忆（INJECTION_ISOLATED）复查统计（确认保持）

动作全幂等、dry-run 支持；净化审计写 purification_log 表。

用法:
  python scripts/memory_purification.py --dry-run
  python scripts/memory_purification.py --limit 50
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def connect_pg():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=50)
    args = ap.parse_args()

    conn = connect_pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purification_log (
            log_id      SERIAL PRIMARY KEY,
            kind        VARCHAR(32),
            memory_id   VARCHAR(64),
            action      VARCHAR(32),
            reason      TEXT,
            purged_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    report = {}

    # 1) duplicates: same content_hash, >1 active
    cur.execute("""
        SELECT content_hash, count(*), string_agg(memory_id::text, ',')
        FROM memories
        WHERE status = 'active' AND content_hash IS NOT NULL
        GROUP BY content_hash HAVING count(*) > 1
        ORDER BY count(*) DESC LIMIT %s
    """, (args.limit,))
    dup_groups = cur.fetchall()
    report["duplicates"] = {"groups": len(dup_groups)}
    dup_purged = 0
    for chash, cnt, ids in dup_groups:
        id_list = ids.split(",")
        # 保留 importance 最高的一条
        cur.execute("""
            SELECT memory_id, importance_score::float8, importance::float8,
                   created_at::text
            FROM memories WHERE memory_id = ANY(%s)
        """, (id_list,))
        rows = [(str(r[0]), float(r[1] or r[2] or 0), str(r[3] or ""))
                for r in cur.fetchall()]
        rows.sort(key=lambda x: (-x[1], x[2]))
        keep = rows[0][0]
        for mid, imp, ts in rows[1:]:
            if args.dry_run:
                print("  [dry] dup " + str(mid)[:20] + " keep=" + str(keep)[:20])
                dup_purged += 1
                continue
            cur.execute(
                "UPDATE memories SET status='archived', updated_at=NOW() "
                "WHERE memory_id=%s AND status='active'", (mid,))
            if cur.rowcount > 0:
                cur.execute(
                    "INSERT INTO purification_log (kind, memory_id, action, reason) "
                    "VALUES ('duplicate', %s, 'archive', %s)",
                    (mid, "同内容冗余（保留 " + str(keep)[:24] + "）"))
                dup_purged += 1
    report["duplicates"]["purged"] = dup_purged

    # 2) conflicts: unresolved conflict groups
    cur.execute("""
        SELECT memory_id, conflict_group_id, importance_score::float8,
               importance::float8
        FROM memories
        WHERE conflict_group_id IS NOT NULL
          AND is_resolved::boolean = FALSE
          AND status = 'active'
        ORDER BY conflict_group_id LIMIT %s
    """, (args.limit,))
    conflict_rows = cur.fetchall()
    report["conflicts"] = {"open": len(conflict_rows)}
    groups = {}
    for mid, gid, isc, imp in conflict_rows:
        groups.setdefault(str(gid), []).append(
            (str(mid), float(isc or imp or 0)))
    resolved = 0
    for gid, members in groups.items():
        if len(members) == 1:
            # 单成员组：无竞争，直接标记 resolved（防 open 噪音）
            if not args.dry_run:
                cur.execute(
                    "UPDATE memories SET is_resolved=TRUE, updated_at=NOW() "
                    "WHERE memory_id=%s", (members[0][0],))
            resolved += 1
            continue
        members.sort(key=lambda x: -x[1])
        keep = members[0][0]
        for mid, imp in members[1:]:
            if args.dry_run:
                print("  [dry] conflict " + str(mid)[:20] + " keep=" + str(keep)[:20])
                resolved += 1
                continue
            cur.execute(
                "UPDATE memories SET is_resolved=TRUE, updated_at=NOW() "
                "WHERE memory_id=%s", (mid,))
            cur.execute(
                "INSERT INTO purification_log (kind, memory_id, action, reason) "
                "VALUES ('conflict', %s, 'resolve', %s)",
                (mid, "冲突消解（保留高价值 " + str(keep)[:24] + "）"))
            resolved += 1
    report["conflicts"]["resolved"] = resolved

    # 3) expired: ttl passed but still active
    cur.execute("""
        SELECT memory_id, ttl_seconds, created_at::text
        FROM memories
        WHERE status = 'active' AND ttl_seconds IS NOT NULL
          AND created_at::timestamptz + (ttl_seconds || ' seconds')::interval
              < NOW()
        LIMIT %s
    """, (args.limit,))
    expired_rows = cur.fetchall()
    report["expired"] = {"found": len(expired_rows)}
    exp_purged = 0
    for mid, ttl, created in expired_rows:
        if args.dry_run:
            print("  [dry] expired " + str(mid)[:20] + " ttl=" + str(ttl))
            exp_purged += 1
            continue
        cur.execute(
            "UPDATE memories SET status='expired', updated_at=NOW() "
            "WHERE memory_id=%s AND status='active'", (mid,))
        if cur.rowcount > 0:
            cur.execute(
                "INSERT INTO purification_log (kind, memory_id, action, reason) "
                "VALUES ('expired', %s, 'expire', %s)",
                (mid, "TTL 到期主动失效（ttl=" + str(ttl) + "s）"))
            exp_purged += 1
    report["expired"]["purged"] = exp_purged

    # 4) isolated: injection-isolated count (review only)
    cur.execute("""
        SELECT count(*) FROM memories
        WHERE status = 'archived'
          AND metadata->>'injection_scan' IS NOT NULL
    """)
    report["isolated_kept"] = cur.fetchone()[0]

    conn.close()
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
