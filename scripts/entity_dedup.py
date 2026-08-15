#!/usr/bin/env python3
"""
Trinity — 实体去重（Entity Resolution, 2026-08-15）
====================================================
对齐 Neo4j/Graphiti 的 embedding-based entity resolution：把 11k 实体中
的同义/相似实体合并（别名归一化 + embedding 余弦相似），并迁移关系引用。

  python scripts/entity_dedup.py --dry-run          # 预览候选（默认）
  python scripts/entity_dedup.py                    # 执行合并（先归一化，再 embedding 相似）
  python scripts/entity_dedup.py --no-embed         # 只做归一化合并（快）
  python scripts/entity_dedup.py --threshold 0.93   # 调 embedding 相似阈值

合并规则：保留 frequency 高者；relations.subject_id/object_id 重指向；
被合并实体软删（DELETE，名称记入保留实体的 summary 别名）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRINITY_ROOT = os.path.dirname(_SCRIPT_DIR)
if _TRINITY_ROOT not in sys.path:
    sys.path.insert(0, _TRINITY_ROOT)

DEFAULT_SQLITE = os.path.expanduser("~/.trinity/store/trinity_store.db")

_NORM_RE = re.compile(r"[\s\-_/\\\.:：,，;；'\"()\[\]{}]+")


def _norm(name: str) -> str:
    return _NORM_RE.sub("", str(name or "")).lower()


def _load_entities(conn) -> List[Dict[str, Any]]:
    rows = conn.execute("SELECT entity_id, name, type, frequency, embedding, summary FROM entities").fetchall()
    return [
        {"id": r[0], "name": r[1], "type": r[2], "freq": r[3] or 0, "embedding": r[4], "summary": r[5]}
        for r in rows
    ]


def _find_norm_groups(entities: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    buckets: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for e in entities:
        buckets.setdefault((e["type"] or "", _norm(e["name"])), []).append(e)
    return [v for v in buckets.values() if len(v) > 1]


def _find_embed_pairs(entities: List[Dict[str, Any]], threshold: float,
                      no_embed: bool, progress_every: int = 2000) -> List[Tuple[Dict[str, Any], Dict[str, Any], float]]:
    """embedding 余弦相似合并候选（分块点积，仅报告相似对）。"""
    import numpy as np
    if no_embed:
        return []
    try:
        from trinity.embeddings.engine import create_engine
        enc = create_engine(backend="auto")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] embedding engine unavailable: {e}")
        return []

    names = [e["name"] for e in entities]
    vecs: List[Optional[np.ndarray]] = []
    for i, n in enumerate(names):
        if i % progress_every == 0:
            print(f"  embedding encode {i}/{len(names)}")
        try:
            v = enc.encode(n)
            vecs.append(np.asarray(v, dtype=np.float32))
        except Exception:  # noqa: BLE001
            vecs.append(None)

    pairs: List[Tuple[Dict[str, Any], Dict[str, Any], float]] = []
    BLOCK = 1024
    for s in range(0, len(entities), BLOCK):
        a_idx = list(range(s, min(s + BLOCK, len(entities))))
        A = np.stack([vecs[i] for i in a_idx if vecs[i] is not None]) if any(vecs[i] is not None for i in a_idx) else None
        if A is None or len(A) == 0:
            continue
        Amap = [i for i in a_idx if vecs[i] is not None]
        for t in range(s + 1, len(entities), BLOCK):
            b_idx = list(range(t, min(t + BLOCK, len(entities))))
            B = np.stack([vecs[j] for j in b_idx if vecs[j] is not None]) if any(vecs[j] is not None for j in b_idx) else None
            if B is None or len(B) == 0:
                continue
            Bmap = [j for j in b_idx if vecs[j] is not None]
            sim = A @ B.T  # (len(Amap), len(Bmap))
            for r_i, i in enumerate(Amap):
                for c_j, j in enumerate(Bmap):
                    if sim[r_i, c_j] >= threshold:
                        pairs.append((entities[i], entities[j], float(sim[r_i, c_j])))
    return pairs


def _merge_group(conn, group: List[Dict[str, Any]], merged: List[Dict[str, Any]],
                 merge_embed: bool) -> None:
    group.sort(key=lambda e: -e["freq"])
    keep = group[0]
    for dup in group[1:]:
        # 迁移关系引用
        conn.execute("UPDATE relations SET subject_id=? WHERE subject_id=?", (keep["id"], dup["id"]))
        conn.execute("UPDATE relations SET object_id=? WHERE object_id=?", (keep["id"], dup["id"]))
        alias = dup["name"]
        if keep["summary"]:
            conn.execute("UPDATE entities SET summary=summary || '; alias:' || ? WHERE entity_id=?", (alias, keep["id"]))
        else:
            conn.execute("UPDATE entities SET summary=? WHERE entity_id=?", (f"alias:{alias}", keep["id"]))
        conn.execute("DELETE FROM entities WHERE entity_id=?", (dup["id"],))
        merged.append({
            "keep": keep["id"], "keep_name": keep["name"], "merged": dup["id"],
            "merged_name": dup["name"], "reason": "norm" if not merge_embed else "embed",
        })


def main() -> int:
    parser = argparse.ArgumentParser(description="Trinity entity resolution/dedup")
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE)
    parser.add_argument("--dry-run", action="store_true", help="只报告候选不合并")
    parser.add_argument("--no-embed", action="store_true", help="跳过 embedding 相似合并（只归一化）")
    parser.add_argument("--threshold", type=float, default=0.93)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    import sqlite3
    conn = sqlite3.connect(args.sqlite_path, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        entities = _load_entities(conn)
        print(f"entities loaded: {len(entities)}")

        norm_groups = _find_norm_groups(entities)
        print(f"normalized-name groups: {len(norm_groups)}")

        merged: List[Dict[str, Any]] = []
        if args.dry_run:
            print("DRY RUN — 候选：")
            for g in norm_groups[:20]:
                print("  norm group:", [e["name"] for e in g])
            if not args.no_embed:
                t0 = time.time()
                pairs = _find_embed_pairs(entities, args.threshold, False)
                print(f"  embed similar pairs (>= {args.threshold}): {len(pairs)} ({time.time()-t0:.1f}s)")
                for a, b, s in pairs[:20]:
                    print(f"    {a['name']!r} ~ {b['name']!r} ({s:.3f})")
            print("merged: 0 (dry-run)")
            return 0

        # 执行：先归一化（无 embedding 依赖，安全），再 embedding 相似
        for g in norm_groups:
            _merge_group(conn, g, merged, merge_embed=False)
        conn.commit()
        print(f"normalized merges: {len(merged)}")

        if not args.no_embed:
            # 重新加载（归一化已合并部分）
            entities = _load_entities(conn)
            t0 = time.time()
            pairs = _find_embed_pairs(entities, args.threshold, False)
            print(f"embed similar pairs: {len(pairs)} ({time.time()-t0:.1f}s)")
            seen = set()
            for a, b, s in pairs:
                key = tuple(sorted((a["id"], b["id"])))
                if key in seen:
                    continue
                seen.add(key)
                # 与已有合并目标冲突时跳过（简化：直接合并，b 并入 a）
                _merge_group(conn, [a, b], merged, merge_embed=True)
            conn.commit()
            print(f"embed merges: {len(merged) - (len(merged) - len([m for m in merged if m['reason']=='norm'] or []))}")

        stats = {
            "run_at": datetime.now(timezone.utc).isoformat(),
            "entities_before": len(entities),
            "merged_count": len(merged),
            "merges": merged[:500],
        }
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(stats, f, indent=2, ensure_ascii=False, default=str)
        else:
            print(json.dumps({"merged_count": len(merged)}, ensure_ascii=False))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
