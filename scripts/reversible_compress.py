#!/usr/bin/env python3
"""reversible_compress.py — 可逆压缩批量工具（2026-09，EXECUTION 105.14）

扫描候选记忆（长内容/低访问）→ 生成压缩摘要 + 重构提示 → 存 metadata
（compression.*，幂等：已有 compression.version 跳过）。**原 content 不动**
（纯增量；decay 将来替换时可逆还原）。

用法:
  python scripts/reversible_compress.py --dry-run --limit 5
  python scripts/reversible_compress.py --limit 20 --min-len 400
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.brain.compression import compress_with_hints  # noqa: E402


def connect_pg():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--min-len", type=int, default=300)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = connect_pg()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("""
        SELECT memory_id, content, length(content)
        FROM memories
        WHERE status = 'active'
          AND length(content) >= %s
          AND metadata->>'compression_version' IS NULL
        ORDER BY length(content) DESC
        LIMIT %s
    """, (args.min_len, args.limit))
    rows = cur.fetchall()
    print("candidates: " + str(len(rows)) + " (len >= " + str(args.min_len) + ")")
    if not rows:
        return 0
    # 批量压缩（一次 LLM 调用多条？compress_with_hints 是单条——逐条，简单可靠）
    done = failed = 0
    for mid, content, clen in rows:
        content = str(content)
        if content.startswith("enc:v1:"):
            continue  # 密文跳过（解密由 extractor 场景处理）
        t0 = time.time()
        comp = compress_with_hints(content)
        dt = time.time() - t0
        if not comp:
            print("  " + str(mid)[:20] + " COMPRESS FAILED (keep original)")
            failed += 1
            continue
        if args.dry_run:
            print("  [dry] " + str(mid)[:20] + " len=" + str(clen)
                  + " -> summary=" + str(comp["summary"])[:40]
                  + " hints=" + str(len(comp["hints"])))
            done += 1
            continue
        meta = json.dumps({
            "compression_version": comp["version"],
            "compression_summary": comp["summary"],
            "compression_hints": comp["hints"],
        }, ensure_ascii=False)
        cur.execute("""
            UPDATE memories
            SET metadata = CASE
                    WHEN jsonb_typeof(metadata) = 'object' THEN metadata || %s::jsonb
                    ELSE '{}'::jsonb || %s::jsonb
                END,
                updated_at = NOW()
            WHERE memory_id = %s
        """, (meta, meta, mid))
        print("  " + str(mid)[:20] + " compressed len=" + str(clen)
              + " -> " + str(comp["summary"])[:36] + " (" + str(round(dt, 1)) + "s)")
        done += 1
        time.sleep(0.2)
    conn.close()
    print("DONE: compressed=" + str(done) + " failed=" + str(failed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
