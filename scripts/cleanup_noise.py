#!/usr/bin/env python3
"""cleanup_noise.py — 检索面噪声清理（R6 P1-④, 2026-08-24）。

背景：本地实测 active 集存在少量噪声——
  1. 极短内容（<15 字符，11 条）：'Test memory' / '回归-get' 等；
  2. 压测/自动关联残留（13 条）：stress/locktest/[自动关联] 标记；
  3. doc dump 大段（doc:* 已在 R6 P0-① 检索排除，此处标记大段候选）。

本脚本（默认 dry-run 报告，--enforce 治理）：
  - 识别极短/测试残留 → 归档（status=archived，移出 active 检索面）；
  - 报告 doc:* 大段（>3000 字符）数量（治理由 budget_doc_share 负责）。
幂等：只处理 active 且匹配的条目。
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

SRC_DB = os.environ.get("TRINITY_DB_PATH") or os.path.expanduser(
    "~/.trinity/store/trinity_store.db"
)

# 压测/自动关联/测试标记——只匹配**内容**，标签类在 SQL 层用 tags 精确匹配
# （2026-08-24 修正：内容含"压测修复"是正常决策记录，不能用子串误伤）
_CONTENT_NOISE_MARKERS = [
    "locktest", "[自动关联]", "LONG-STRESS",
    "Test memory", "PP PROBE", "MULTI-PROC", "benchmark_agent",
]
# 极短阈值（2026-08-24 修正：中文短句（偏好/决策）是正常记忆，需 len>=8
# 且非纯 ASCII 才判噪声；ASCII 测试残留 <15 判噪声）
MIN_LEN_ASCII = 15
MIN_LEN_CJK = 0  # 中文不按长度判噪声（'用户偏好暗色模式' 是有效记忆）
# doc dump 大段阈值
DOC_DUMP_LEN = 3000


def _is_noise_content(r) -> bool:
    """内容级噪声判定（不含标签类——标签在 SQL 处理）。"""
    content = str(r["content"] or "")
    low = content.lower()
    # 压缩摘要（compressed_*）保留原文片段，不做内容级匹配
    if (r["category"] or "").startswith("compressed_"):
        return False
    for m in _CONTENT_NOISE_MARKERS:
        if m.lower() in low:
            return True
    # ASCII 短残留（'Test memory'/'回归-get' 等英文短串）
    if len(content) < MIN_LEN_ASCII and content.isascii() and content.strip():
        return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=SRC_DB)
    parser.add_argument("--enforce", action="store_true", help="归档噪声（默认报告）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"db not found: {args.db}")
        return 1

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT memory_id, category, tags, content, length(content) AS len FROM memories "
        "WHERE status='active'"
    ).fetchall()
    conn.close()

    short = [r for r in rows if r["len"] < MIN_LEN_ASCII and str(r["content"] or "").isascii()]
    content_marked = [r for r in rows if _is_noise_content(r) and r not in short]
    # 标签级噪声（压测标签的明确测试条目）
    tag_marked = [
        r for r in rows
        if ("locktest" in (r["tags"] or "") or "stress" in (r["tags"] or ""))
        and not str(r["content"] or "").startswith("[trinity-hermes-sync]")
        and "压测" not in str(r["content"] or "")[:80]
    ]
    doc_dumps = [r for r in rows if (r["category"] or "").startswith("doc") and r["len"] > DOC_DUMP_LEN]

    combined = []
    seen = set()
    for r in short + content_marked + tag_marked:
        if r["memory_id"] not in seen:
            seen.add(r["memory_id"])
            combined.append(r)

    print(f"active: {len(rows)} | 极短ASCII: {len(short)} | 内容标记: {len(content_marked)} "
          f"| 标签标记: {len(tag_marked)} | doc dump(>{DOC_DUMP_LEN}): {len(doc_dumps)}")
    for r in combined[:10]:
        print(f"  - {r['memory_id']} [{r['category']}] {str(r['content'])[:45]!r}")

    if not args.enforce or args.dry_run:
        mode = "DRY-RUN" if args.dry_run else "REPORT"
        print(f"{mode} — 未归档（--enforce 执行）")
        return 0

    ids = [r["memory_id"] for r in combined]
    if not ids:
        print("无噪声可清理")
        return 0
    conn = sqlite3.connect(args.db, timeout=15)
    cur = conn.execute(
        "UPDATE memories SET status='archived' WHERE memory_id IN ({}) "
        "AND status='active'".format(",".join("?" * len(ids))),
        ids,
    )
    conn.commit()
    print(f"archived {cur.rowcount} noise memories")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
