#!/usr/bin/env python3
"""backfill_pool_status.py — 存量聚合池 source_status 回填（R8 P0-1, 2026-08-24）。

背景：聚合池 11,412 条（9,193 imported）此前无 source_status 字段——
引擎库检索只查 status='active'（1,882 条），聚合池却含已归档记忆，
导致"归档=治理生效"承诺失真（检索口径分裂）。

本脚本把引擎库 memories.status 按 content 精确匹配回填到聚合池条目：
  - 命中 active      → source_status='active'
  - 命中 archived    → source_status='archived'（此后默认检索不再命中）
  - 命中 deleted     → source_status='deleted'
  - 未命中           → 保持 None（池内自有内容，视为 active 兼容）

用法：
    python scripts/backfill_pool_status.py                # 回填 + 写盘
    python scripts/backfill_pool_status.py --dry-run      # 只统计不写
    python scripts/backfill_pool_status.py --limit 500    # 限量测试

注意：聚合池文件由 API 进程运行时持有，写盘需维护窗口（同
sync_pool_from_db_v2 约定；maintenance 的 pool-sync 任务内置 API 在线守卫）。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

TRINITY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRINITY_DIR)
sys.path.insert(0, os.path.join(TRINITY_DIR, "trinity"))
os.environ.setdefault("TRINITY_SILENT", "1")
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")  # 抑制聚合器自举，脚本轻量

SRC_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
POOL_PATH = os.path.join(TRINITY_DIR, "data", "aggregator_pool.json")


def load_pool() -> list:
    with open(POOL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("memories") or []
    return items


def build_status_map(conn, limit: int = 0) -> dict:
    """content(normalized) → status。"""
    sql = "SELECT content, status FROM memories WHERE content IS NOT NULL"
    if limit:
        sql += f" LIMIT {limit}"
    m = {}
    for content, status in conn.execute(sql).fetchall():
        key = str(content).strip()
        if key:
            m[key] = status
    return m


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--pool", default=POOL_PATH)
    parser.add_argument("--db", default=SRC_DB)
    args = parser.parse_args()

    if not os.path.exists(args.pool):
        print(f"pool not found: {args.pool}")
        return 1

    conn = sqlite3.connect(args.db)
    status_map = build_status_map(conn, args.limit)
    conn.close()
    print(f"engine status map: {len(status_map)} entries")

    items = load_pool()
    print(f"pool items: {len(items)}")

    counts = {"active": 0, "archived": 0, "deleted": 0, "unchanged": 0, "missing": 0}
    t0 = time.perf_counter()
    for it in items:
        key = str(it.get("content", "")).strip()
        if not key:
            counts["missing"] += 1
            continue
        status = status_map.get(key)
        if status is None:
            counts["missing"] += 1
            continue
        old = it.get("source_status")
        if old == status:
            counts["unchanged"] += 1
        else:
            it["source_status"] = status
            counts[status] = counts.get(status, 0) + 1

    elapsed = time.perf_counter() - t0
    print(f"scan: {elapsed:.1f}s | " + " ".join(f"{k}={v}" for k, v in counts.items()))

    if args.dry_run:
        print("DRY RUN — 未写盘")
        return 0

    data = json.load(open(args.pool, encoding="utf-8"))
    if isinstance(data, list):
        data = items
    else:
        data["memories"] = items
    with open(args.pool, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"pool written: {args.pool}")

    # 同步向量持久化文件无需动（向量与 status 无关）
    print("done. 注意：API 进程持有内存池，写盘后需重启 API（或等其内存池重新加载）生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
