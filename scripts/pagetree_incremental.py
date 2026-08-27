# -*- coding: utf-8 -*-
"""pagetree_incremental.py — 页树增量更新（2026-08-27）。

查上次构建（built_at）后新增的记忆 → incremental_update 归属现有簇。
用法: python scripts/pagetree_incremental.py [--dry-run]
"""
import os, sys, time, argparse
_TRINITY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    from trinity import Trinity
    from trinity.retrieval.pagetree import MemoryPageTree
    import os as _os
    tree_path = _os.path.expanduser("~/.trinity/store/pagetree.json")
    tree = MemoryPageTree.load(tree_path)
    if tree is None:
        print("no existing tree — run full build first")
        return 1
    # 2026-08-27: UTC 1h 窗口（created_at 是 UTC，built_at 是本地——直接比较会时区错位）
    import datetime as _dt
    window = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")
    mem = Trinity(adapter="sqlite")
    rows = mem._adapter.get_all_memories(limit=5000, offset=0)
    new = [r for r in rows if str(r.get("created_at") or "") > window and r.get("status") == "active"]
    print(f"window={window} | new since: {len(new)}")
    if not new:
        print("nothing to update")
        return 0
    if args.dry_run:
        print("dry-run: would add", len(new), "records")
        return 0
    t0 = time.time()
    # 排除已在树中的（memory_index 覆盖）
    new = [r for r in new if r.get("memory_id") not in (tree.memory_index or {})]
    res = tree.incremental_update(new)
    import datetime as _dt2
    # 2026-08-27: 向量增量——只嵌入新增簇（秒级）
    _new_ids = res.get("new_cluster_ids") or []
    if _new_ids:
        t1 = time.time()
        try:
            from trinity.embeddings.engine import create_engine as _ce
            _eng = _ce(backend="auto", use_cache=True)
            for _cid in _new_ids:
                _node = tree.clusters.get(_cid)
                if not _node:
                    continue
                _summ = (_node.get("summary") or "").strip() or " ".join(_node.get("sample", []))[:300]
                _v = _eng.embed("[" + str(_node.get("category", "")) + "] " + _summ)
                tree._node_vectors[_cid] = [float(x) for x in _v]
            print("vectors for", len(_new_ids), "new clusters in", round(time.time()-t1, 1), "s")
        except Exception as _ve:
            print("vector embed skipped:", type(_ve).__name__, str(_ve)[:80])
    tree.built_at = _dt2.datetime.now(_dt2.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    tree.save(tree_path)
    print(f"incremental: {res} in {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
