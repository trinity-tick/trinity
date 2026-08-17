#!/usr/bin/env python3
"""
大库 → 聚合池 同步脚本
========================
把 ~/.trinity/store/trinity_store.db（11,289 条历史记忆）导入
MemoryAggregator 共享聚合池（trinity/data/aggregator_pool.json），
使本地 API 的 hybrid 检索覆盖全部历史记忆。

用法:
    python benchmark/sync_pool_from_db.py [--limit N] [--dry-run]
    --limit N   只导入前 N 条（测速用）
    --dry-run   不持久化
"""
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

SRC_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="仅导入前 N 条（0=全部）")
    parser.add_argument("--dry-run", action="store_true", help="不持久化")
    args = parser.parse_args()

    # 1) 读大库
    conn = sqlite3.connect(SRC_DB)
    rows = conn.execute(
        "SELECT memory_id, content, category, metadata, created_at, importance "
        "FROM memories WHERE status != 'deleted'"
    ).fetchall()
    conn.close()
    if args.limit:
        rows = rows[:args.limit]
    print(f"[sync] 待导入: {len(rows)} 条 (limit={args.limit or 'all'})")

    # 2) 加载聚合池（自动发现持久化路径 = trinity/data/aggregator_pool.json）
    from trinity.agents.aggregator import MemoryAggregator
    t0 = time.perf_counter()
    agg = MemoryAggregator()
    pool_before = len(agg._pool)
    print(f"[sync] 聚合池已加载: {pool_before} 条，持久化: {agg._persist_path}")

    # 3) 导入
    t_start = time.perf_counter()
    merged = 0
    for i, (mid, content, category, metadata, created_at, importance) in enumerate(rows):
        if not content or not str(content).strip():
            continue
        md = {}
        if category:
            md["category"] = category
        if metadata:
            try:
                md["db_metadata"] = json.loads(metadata) if isinstance(metadata, str) else metadata
            except Exception:
                pass
        md["db_memory_id"] = mid
        md["db_created_at"] = str(created_at) if created_at else None
        try:
            dv = agg.ingest(str(content), source_agent="db-sync", metadata=md)
        except Exception as exc:
            print(f"[sync] 导入失败 {mid}: {exc}")
            continue
        if dv is not None and hasattr(dv, "memory_id") and dv.memory_id != mid:
            merged += 1
        if (i + 1) % 500 == 0:
            rate = (i + 1) / max(time.perf_counter() - t_start, 1e-6)
            print(f"[sync] {i+1}/{len(rows)} 条, {rate:.1f} 条/s, "
                  f"池内 {len(agg._pool)} 条, 预计剩余 {max(0, (len(rows)-i-1)/rate):.0f}s")

    # 4) 持久化
    if not args.dry_run:
        agg._save()
        print(f"[sync] 已持久化到 {agg._persist_path}")
    else:
        print("[sync] dry-run，未持久化")

    elapsed = time.perf_counter() - t_start
    print(f"[sync] 完成: 导入 {len(rows)} 条, 池 {pool_before} -> {len(agg._pool)} 条, "
          f"耗时 {elapsed:.0f}s ({len(rows)/max(elapsed,1e-6):.1f} 条/s)")


if __name__ == "__main__":
    main()
