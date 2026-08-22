#!/usr/bin/env python3
"""
大库 → 聚合池 续传脚本 v2（2026-08-14；2026-08-21 新增 watermark 增量模式）
==========================================================================
承接 v1（已入库 6,272 条）：跳过已在聚合池的 content，补导入剩余大库记忆，
并绕过 os.replace 文件锁（直接写文件）。

2026-08-21（P0-2，借鉴 codex watermark 陈旧检测）：默认改为 **watermark 增量**：
- 在源库建 sync_watermarks(source, watermark, updated_at) 表；
- watermark = 上次处理到的最大 rowid（SQLite 隐式自增列，插入序单调——memory_id
  前缀/长度不统一、updated_at 格式混用，均不可作 watermark）；
- 每次只扫描 rowid > watermark 的新增/变更行，避免全表扫描 1.3 万+ 条；
- 每 500 条与结束时事务性更新 watermark（崩溃后从上次水位续跑）；
- content 去重保留为兜底（防重复导入）。

注意：聚合池文件由 API 进程在运行时持有（内存池 + 脏写持久化）。本脚本直接
写盘，**需在 supervisor/API/uvicorn 停止的维护窗口运行**（否则内存池后续
持久化会覆盖本脚本写入）。maintenance 的 pool-sync 任务内置该守卫（API 在线
时 SKIP）。

用法：
    python benchmark/sync_pool_from_db_v2.py                 # watermark 增量
    python benchmark/sync_pool_from_db_v2.py --limit 500     # 限量（测试）
    python benchmark/sync_pool_from_db_v2.py --no-watermark  # 全量 + content 跳过
    python benchmark/sync_pool_from_db_v2.py --reset-watermark  # 清水位后全量
"""
import argparse
import json
import os
import pickle
import sqlite3
import sys
import time

TRINITY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRINITY_DIR)
sys.path.insert(0, os.path.join(TRINITY_DIR, "trinity"))
os.environ.setdefault("TRINITY_SILENT", "1")

SRC_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
WATERMARK_SOURCE = "db-pool-sync"
WATERMARK_BATCH = 500


def _ensure_watermark_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sync_watermarks ("
        " source TEXT PRIMARY KEY, watermark TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    conn.commit()


def _read_watermark(conn) -> str:
    row = conn.execute(
        "SELECT watermark FROM sync_watermarks WHERE source=?", (WATERMARK_SOURCE,)
    ).fetchone()
    return row[0] if row else "0"


def _advance_watermark(rowid) -> None:
    """推进水位：独立短连接 + 短事务，避免长事务锁源库。"""
    wconn = sqlite3.connect(SRC_DB)
    try:
        _ensure_watermark_table(wconn)
        wconn.execute(
            "INSERT INTO sync_watermarks (source, watermark, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET watermark=excluded.watermark, "
            "updated_at=excluded.updated_at",
            (WATERMARK_SOURCE, str(rowid), time.time()),
        )
        wconn.commit()
    finally:
        wconn.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-watermark", action="store_true",
                        help="全量扫描（content 跳过兜底），不按 watermark 过滤")
    parser.add_argument("--reset-watermark", action="store_true",
                        help="清空水位（下次从 rowid=0 全量）")
    args = parser.parse_args()

    from trinity.agents import aggregator as agg_mod
    from trinity.agents.aggregator import MemoryAggregator

    # ── monkey-patch: 直接写文件（绕过 tmp+os.replace 的 DELETE 锁）──
    def _direct_save(self):
        if not self._persist_path:
            return
        with self._lock:
            data = {
                "version": "6.99.0",
                "timestamp": time.time(),
                "memories": [dv.to_dict(full=True) for dv in self._pool.values()],
                "relations": {mid: dict(edges) for mid, edges in self._relations_graph.items()},
                "stats": dict(self._stats),
            }
        persist_dir = os.path.dirname(self._persist_path)
        os.makedirs(persist_dir, exist_ok=True)
        with open(self._persist_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        # 向量索引直接写
        if self._faiss_index is not None and self._index_id_map:
            vec_path = os.path.join(persist_dir, agg_mod.VECTOR_PERSIST_FILENAME)
            vec_data = {"dim": self._vector_dim, "id_map": self._index_id_map}
            try:
                if agg_mod._HAS_FAISS:
                    import faiss
                    faiss.write_index(self._faiss_index, vec_path)
                else:
                    vec_data["vectors"] = self._faiss_index.tolist()
                    with open(vec_path, "wb") as f:
                        pickle.dump(vec_data, f)
            except Exception as exc:
                print(f"[sync] 向量持久化失败(继续): {exc}")
        print(f"[sync] 直接写盘完成: {len(self._pool)} 条")

    MemoryAggregator._save = _direct_save
    # 关闭 debounce 定时器干扰：把脏计数阈值调大
    agg_mod.PERSIST_MAX_DIRTY = 10**9

    # ── 1. 读大库（watermark 增量 or 全量）──
    conn = sqlite3.connect(SRC_DB)
    _ensure_watermark_table(conn)
    if args.reset_watermark:
        conn.execute("DELETE FROM sync_watermarks WHERE source=?", (WATERMARK_SOURCE,))
        conn.commit()
        print("[sync] watermark 已重置")
    watermark = _read_watermark(conn)
    use_watermark = not args.no_watermark
    if use_watermark:
        rows = conn.execute(
            "SELECT rowid, memory_id, content, category, created_at FROM memories "
            "WHERE status != 'deleted' AND rowid > ? ORDER BY rowid",
            (watermark,),
        ).fetchall()
        print(f"[sync] watermark 增量: 上次水位 rowid={watermark}, 待处理 {len(rows)} 条")
    else:
        rows = conn.execute(
            "SELECT rowid, memory_id, content, category, created_at FROM memories "
            "WHERE status != 'deleted'"
        ).fetchall()
        print(f"[sync] 全量扫描: {len(rows)} 条 (watermark 关闭)")
    conn.close()
    if args.limit:
        rows = rows[:args.limit]

    # ── 2. 加载聚合池 ──
    t0 = time.perf_counter()
    agg = MemoryAggregator()
    pool_before = len(agg._pool)
    print(f"[sync] 聚合池已加载: {pool_before} 条, 持久化: {agg._persist_path}")

    # ── 3. 构建已存在 content 集合（跳过已导入，兜底）──
    existing = {str(dv.content).strip() for dv in agg._pool.values()}
    print(f"[sync] 已有 content 集合: {len(existing)}")

    # ── 4. 补导入 + watermark 推进 ──
    t_start = time.perf_counter()
    skipped = 0
    added = 0
    last_rowid = watermark if use_watermark else "0"
    for i, (rowid, mid, content, category, created_at) in enumerate(rows):
        last_rowid = str(rowid)
        text = str(content).strip() if content else ""
        if not text:
            continue
        if text in existing:
            skipped += 1
            continue
        md = {"category": category} if category else {}
        agg.ingest(text, source_agent="db-sync", metadata=md)
        added += 1
        if use_watermark and (i + 1) % WATERMARK_BATCH == 0:
            _advance_watermark(last_rowid)
        if (i + 1) % 500 == 0:
            rate = (i + 1) / max(time.perf_counter() - t_start, 1e-6)
            print(f"[sync] 扫描 {i+1}/{len(rows)} (跳过 {skipped}, 新增 {added}), "
                  f"{rate:.0f} 条/s, 池 {len(agg._pool)}, 水位 rowid={last_rowid}, "
                  f"预计剩余 {max(0, (len(rows)-i-1)/rate):.0f}s")

    # ── 5. 持久化 + 最终 watermark ──
    agg._save()
    if use_watermark and rows:
        _advance_watermark(last_rowid)
    elapsed = time.perf_counter() - t_start
    print(f"[sync] 完成: 扫描 {len(rows)}, 跳过 {skipped}, 新增 {added}, "
          f"池 {pool_before} -> {len(agg._pool)}, 耗时 {elapsed:.0f}s"
          + (f", 水位 rowid={last_rowid}" if use_watermark else ""))


if __name__ == "__main__":
    main()
