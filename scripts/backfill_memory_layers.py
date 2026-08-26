#!/usr/bin/env python3
"""backfill_memory_layers.py — memory_layer 历史回填（R8 P0-2, 2026-08-24）。

背景：13,439 条记忆中 11,990 条（89%）memory_layer 为 NULL——四层记忆
分层此前只覆盖 active 子集（tiers 只处理 active+LIMIT），历史记忆永不分层，
"四层记忆模型对齐共识"名不副实。

本脚本用 LayerClassifier（纯规则启发式，无 LLM，快）对全部 NULL 记忆批量
分类并回填 memory_layer 列：
  - semantic / episodic（LayerClassifier 输出）
  - 规则兜底：category 已有分层语义时直接采用

用法：
    python scripts/backfill_memory_layers.py                 # 回填
    python scripts/backfill_memory_layers.py --dry-run       # 只统计
    python scripts/backfill_memory_layers.py --limit 500     # 限量测试
    python scripts/backfill_memory_layers.py --batch 2000    # 批大小（默认 1000）

幂等：只处理 memory_layer IS NULL 的记忆；重复运行安全。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

SRC_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")
DEFAULT_BATCH = 1000

_CATEGORY_LAYER = {
    "episodic": "episodic", "event": "episodic", "experience": "episodic",
    "session": "episodic",
    "semantic": "semantic", "fact": "semantic", "context": "semantic",
    "knowledge": "semantic", "rule": "semantic", "preference": "semantic",
    "policy": "semantic", "decision": "semantic", "insight": "semantic",
    "general": None,  # 走分类器
}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH)
    parser.add_argument("--db", default=SRC_DB)
    args = parser.parse_args()

    from trinity.memory.layer_classifier import LayerClassifier
    classifier = LayerClassifier()  # 无 LLM，纯规则

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # 只取 NULL 的记忆
    rows = conn.execute(
        "SELECT rowid, content, category FROM memories WHERE memory_layer IS NULL"
    ).fetchall()
    if args.limit:
        rows = rows[:args.limit]
    total = len(rows)
    print(f"NULL-layer memories: {total}")

    counts = {"semantic": 0, "episodic": 0, "category_hint": 0, "empty": 0}
    t0 = time.perf_counter()
    done = 0
    while done < total:
        batch = rows[done:done + args.batch]
        for row in batch:
            content = str(row["content"] or "").strip()
            category = str(row["category"] or "general")
            if not content:
                counts["empty"] += 1
                continue
            hint = _CATEGORY_LAYER.get(category)
            if hint:
                layer = hint
                counts["category_hint"] += 1
            else:
                try:
                    layer = classifier.classify(content, category)
                except Exception:
                    layer = "semantic"  # 兜底
            counts[layer] = counts.get(layer, 0) + 1
            if not args.dry_run:
                conn.execute(
                    "UPDATE memories SET memory_layer=? WHERE rowid=?",
                    (layer, row["rowid"]),
                )
        done += len(batch)
        if not args.dry_run:
            conn.commit()
        rate = done / max(time.perf_counter() - t0, 1e-6)
        print(f"  processed {done}/{total} ({rate:.0f}/s)")

    elapsed = time.perf_counter() - t0
    print(f"done in {elapsed:.1f}s | " + " ".join(f"{k}={v}" for k, v in counts.items()))

    if not args.dry_run:
        # 校验
        remaining = conn.execute(
            "SELECT COUNT(*) c FROM memories WHERE memory_layer IS NULL"
        ).fetchone()["c"]
        print(f"remaining NULL-layer: {remaining}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
