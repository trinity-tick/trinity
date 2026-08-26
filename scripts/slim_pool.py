#!/usr/bin/env python3
"""slim_pool.py — 聚合池瘦身（R9 建议 P0, 2026-08-24）。

背景：聚合池 11,412 条中 9,828 条（86%）是 source_status=archived 的
归档记忆——R8 起默认检索已过滤（include_archived=False），但池文件与
内存仍臃肿（13.4MB JSON），且 archived 条目永远不被默认检索命中。

策略：移除 archived 条目（保留 active / merged / None 兼容条目），
同步裁剪 relations 图与向量索引；池从此 = 引擎库 active 口径的镜像，
与 R8 的检索过滤语义完全一致（include_archived 参数保留，池内已无
archived 时自然无历史命中——历史检索走引擎库权威数据）。

用法：
    python scripts/slim_pool.py --dry-run     # 预览统计
    python scripts/slim_pool.py                # 执行（备份 + 过滤 + 重建索引）
    python scripts/slim_pool.py --keep-days 30 # 保留近 30 天 archived（可选）

注意：聚合池文件由 API 进程运行时持有（内存池 + 脏写持久化），执行需
维护窗口（API 停止）；maintenance 的 pool-sync 任务内置 API 在线守卫，
本脚本应与其同窗口执行。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

TRINITY_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, TRINITY_DIR)
sys.path.insert(0, os.path.join(TRINITY_DIR, "trinity"))
os.environ.setdefault("TRINITY_SILENT", "1")
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")  # 抑制聚合器自举

DATA_DIR = os.path.join(TRINITY_DIR, "data")
POOL_PATH = os.path.join(DATA_DIR, "aggregator_pool.json")
VEC_PATH = os.path.join(DATA_DIR, "aggregator_vectors.pkl")
BACKUP_DIR = os.path.join(DATA_DIR, "backups_pool_slim")


def load_pool() -> dict:
    with open(POOL_PATH, encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-days", type=int, default=0,
                        help="保留最近 N 天内的 archived（默认 0 = 全部移除）")
    parser.add_argument("--pool", default=POOL_PATH)
    parser.add_argument("--vec", default=VEC_PATH)
    args = parser.parse_args()

    if not os.path.exists(args.pool):
        print(f"pool not found: {args.pool}")
        return 1

    data = load_pool()
    memories = data if isinstance(data, list) else data.get("memories") or []
    relations = data.get("relations", {}) if isinstance(data, dict) else {}
    stats = data.get("stats", {}) if isinstance(data, dict) else {}

    total = len(memories)
    from collections import Counter
    status_dist = Counter(str(m.get("source_status", "None")) for m in memories)

    # ── 过滤条件 ──
    cutoff = time.time() - args.keep_days * 86400 if args.keep_days > 0 else None
    keep, removed = [], 0
    removed_recent = 0
    for m in memories:
        st = m.get("source_status")
        if st == "archived":
            if cutoff is not None and (m.get("updated_at") or 0) >= cutoff:
                keep.append(m)
                removed_recent += 1
                continue
            removed += 1
            continue
        keep.append(m)

    print(f"pool: {total} 条 | status: {dict(status_dist)}")
    print(f"移除 archived: {removed}{f'（保留近 {args.keep_days} 天 {removed_recent} 条）' if cutoff else ''} | 保留: {len(keep)}")

    # relations 图裁剪（只保留 keep 记忆的边）
    keep_ids = {m.get("memory_id") for m in keep if m.get("memory_id")}
    kept_relations = {}
    removed_edges = 0
    for mid, edges in relations.items():
        if mid in keep_ids:
            kept_edges = {t: lbl for t, lbl in edges.items() if t in keep_ids}
            removed_edges += len(edges) - len(kept_edges)
            if kept_edges:
                kept_relations[mid] = kept_edges
    print(f"relations: {sum(len(v) for v in relations.values())} 边 → {sum(len(v) for v in kept_relations.values())} 边（裁掉 {removed_edges}）")

    if args.dry_run:
        print("DRY RUN — 未写盘")
        return 0

    # ── 备份 ──
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    for src, name in ((args.pool, "aggregator_pool.json"), (args.vec, "aggregator_vectors.pkl")):
        if os.path.exists(src):
            dst = os.path.join(BACKUP_DIR, f"{name}.{ts}")
            shutil.copy2(src, dst)
            print(f"backup: {dst} ({os.path.getsize(dst)//1024} KB)")

    # ── 写池 ──
    new_data = {
        "version": data.get("version", "6.99.0") if isinstance(data, dict) else "6.99.0",
        "timestamp": time.time(),
        "slimmed_at": ts,
        "removed_archived": removed,
        "memories": keep,
        "relations": kept_relations,
        "stats": stats,
    }
    with open(args.pool, "w", encoding="utf-8") as f:
        json.dump(new_data, f, ensure_ascii=False, indent=2)
    print(f"pool written: {args.pool} ({os.path.getsize(args.pool)//1024} KB, 原 {os.path.getsize(args.pool)//1024 if False else '见备份'})")

    # ── 重建向量索引（对保留记忆重新 embed）──
    # 原索引含被移除 ID，faiss 索引无法就地删除 → 重建（1,600 条量级秒级）。
    try:
        from trinity.embeddings import create_engine
        import numpy as np
        eng = create_engine(backend="auto", use_cache=True)
        ids, vectors = [], []
        for m in keep:
            mid = m.get("memory_id")
            content = str(m.get("content", ""))[:512]
            if not mid or not content:
                continue
            try:
                v = eng.embed(content)
                if v is not None and hasattr(v, "tolist"):
                    vectors.append(np.asarray(v, dtype=np.float32))
                    ids.append(mid)
            except Exception:
                continue
        if vectors:
            import pickle
            mat = np.stack(vectors)
            vec_data = {
                "dim": int(mat.shape[1]),
                "id_map": {i: mid for i, mid in enumerate(ids)},
                "vectors": mat.tolist(),
                "slimmed": True,
            }
            with open(args.vec, "wb") as f:
                pickle.dump(vec_data, f)
            print(f"vector index rebuilt: {len(ids)} 条, dim={mat.shape[1]} -> {args.vec}")
        else:
            print("WARN: no vectors embedded, vector index not rebuilt (pool still valid for FTS/keyword)")
    except Exception as exc:
        print(f"WARN: vector rebuild failed (pool still valid for FTS/keyword): {exc}")

    print("done. 注意：API 进程持有内存池，写盘后需重启 API 生效（supervisor 5 分钟内自动拉起）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
