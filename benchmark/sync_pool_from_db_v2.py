#!/usr/bin/env python3
"""
大库 → 聚合池 续传脚本 v2
==========================
承接 v1（已入库 6,272 条）：跳过已在聚合池的 content，
补导入剩余大库记忆，并绕过 os.replace 文件锁（直接写文件）。

前置：supervisor / API / uvicorn 已停止，聚合池文件无写竞争。
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0)
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

    # ── 1. 读大库 ──
    conn = sqlite3.connect(SRC_DB)
    rows = conn.execute(
        "SELECT memory_id, content, category, created_at "
        "FROM memories WHERE status != 'deleted'"
    ).fetchall()
    conn.close()
    if args.limit:
        rows = rows[:args.limit]
    print(f"[sync] 大库共 {len(rows)} 条")

    # ── 2. 加载聚合池（当前 6,303 条）──
    t0 = time.perf_counter()
    agg = MemoryAggregator()
    pool_before = len(agg._pool)
    print(f"[sync] 聚合池已加载: {pool_before} 条, 持久化: {agg._persist_path}")

    # ── 3. 构建已存在 content 集合（跳过已导入）──
    existing = {str(dv.content).strip() for dv in agg._pool.values()}
    print(f"[sync] 已有 content 集合: {len(existing)}")

    # ── 4. 补导入 ──
    t_start = time.perf_counter()
    skipped = 0
    added = 0
    for i, (mid, content, category, created_at) in enumerate(rows):
        text = str(content).strip() if content else ""
        if not text:
            continue
        if text in existing:
            skipped += 1
            continue
        md = {"category": category} if category else {}
        agg.ingest(text, source_agent="db-sync", metadata=md)
        added += 1
        if (i + 1) % 500 == 0:
            rate = (i + 1) / max(time.perf_counter() - t_start, 1e-6)
            print(f"[sync] 扫描 {i+1}/{len(rows)} (跳过 {skipped}, 新增 {added}), "
                  f"{rate:.0f} 条/s, 池 {len(agg._pool)}, 预计剩余 "
                  f"{max(0, (len(rows)-i-1)/rate):.0f}s")

    # ── 5. 持久化（直接写）──
    agg._save()
    elapsed = time.perf_counter() - t_start
    print(f"[sync] 完成: 扫描 {len(rows)}, 跳过 {skipped}, 新增 {added}, "
          f"池 {pool_before} -> {len(agg._pool)}, 耗时 {elapsed:.0f}s")


if __name__ == "__main__":
    main()
