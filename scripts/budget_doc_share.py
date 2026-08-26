#!/usr/bin/env python3
"""budget_doc_share.py — 检索面 token 预算分层治理（R6 P0-②, 2026-08-24）。

背景：本地实测 active 检索面 1,889 条中 462 条（24%）是 doc:* 类文档
dump——检索噪声污染（Raft 查询 top-10 中 70% 被文档命中）。对齐 2026
"记忆按 token 预算分层、超限先淘汰低价值项"共识。

本脚本：检查 active 面 doc:* 占比，超阈值时治理——
  - 报告模式（默认）：只统计 + 列出超限候选（按 importance 排序）；
  - 治理模式（--enforce）：把超出预算的 doc 条目归档（status=archived，
    移出 active 检索面——doc 类已默认被检索排除（R6 P0-①），归档是
    让"存储面"也收敛，避免未来 include_docs 检索时被淹没）。

用法：
    python scripts/budget_doc_share.py                    # 报告
    python scripts/budget_doc_share.py --enforce          # 治理（归档超限 doc）
    python scripts/budget_doc_share.py --max-share 0.15   # 阈值（默认 0.10）
    python scripts/budget_doc_share.py --dry-run          # 预览治理动作
幂等：重复运行只处理当前超限部分。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

SRC_DB = os.environ.get("TRINITY_DB_PATH") or os.path.expanduser(
    "~/.trinity/store/trinity_store.db"
)
DEFAULT_MAX_SHARE = 0.10  # doc 占 active 检索面上限（10%）


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=SRC_DB)
    parser.add_argument("--max-share", type=float, default=DEFAULT_MAX_SHARE)
    parser.add_argument("--enforce", action="store_true", help="治理模式：归档超限 doc")
    parser.add_argument("--dry-run", action="store_true", help="预览治理动作不落库")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}")
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) c FROM memories WHERE status='active'").fetchone()["c"]
    doc_total = conn.execute(
        "SELECT COUNT(*) c FROM memories WHERE status='active' "
        "AND (category LIKE 'doc:%' OR category LIKE 'doc_%')"
    ).fetchone()["c"]
    share = doc_total / max(total, 1)
    print(f"active: {total} | doc:*: {doc_total} ({share*100:.1f}%) | 阈值: {args.max_share*100:.0f}%")

    if share <= args.max_share:
        print(f"OK — doc 占比在预算内（{share*100:.1f}% ≤ {args.max_share*100:.0f}%）")
        conn.close()
        return 0

    # 超限：按 importance 升序找应归档的 doc（保留高价值）
    over_count = int(doc_total - args.max_share * total)
    candidates = conn.execute(
        "SELECT memory_id, category, importance, length(content) AS len FROM memories "
        "WHERE status='active' AND (category LIKE 'doc:%' OR category LIKE 'doc_%') "
        "ORDER BY importance ASC, len DESC LIMIT ?",
        (over_count,),
    ).fetchall()
    print(f"超限 {over_count} 条（按 importance 升序候选）:")
    for c in candidates[:5]:
        print(f"  - {c['memory_id']} [{c['category']}] imp={c['importance']} len={c['len']}")

    if not args.enforce or args.dry_run:
        mode = "DRY-RUN" if args.dry_run else "REPORT"
        print(f"{mode} — 未归档（--enforce 执行）")
        conn.close()
        return 0

    conn.close()
    # 治理：归档候选（短连接 + 事务）
    conn = sqlite3.connect(args.db, timeout=15)
    ids = [c["memory_id"] for c in candidates]
    cur = conn.execute(
        "UPDATE memories SET status='archived' WHERE memory_id IN ({}) "
        "AND status='active'".format(",".join("?" * len(ids))),
        ids,
    )
    conn.commit()
    print(f"archived {cur.rowcount} doc memories (budget governance)")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
