#!/usr/bin/env python3
"""replay_consolidation.py — 海马体重放式巩固（2026-09，EXECUTION 105 第 2 轮）

认知依据：海马体在睡眠中【重放】高价值记忆——重新激活强化突触、
把相关片段整合进核心记忆（系统整合理论，Squire & Alvarez 1995）。
对标 2025 Bio-realistic Synthetic Hippocampus 的 replay 思想。

实现（PG 主存储，metadata jsonb 状态，不动 schema）：
  1. 候选：active + 高价值（importance::float8 >= 0.5 或 value_model=v1）+ replay_count < 3；
  2. 重放：取候选记忆 → 检索同 category 相关片段（content_tsv 相似，非自身）→
     LLM 生成【重放整合摘要】（把片段信息并入核心记忆的叙述）；
  3. 巩固信号：replay_count+1、last_replayed_at、replay_summary、access_count+1
     （重新激活）；原 content 不动（保持审计链）；
  4. 幂等：replay_count >= 3 跳过；LLM 失败 → 仅重新激活（无摘要）。

用法:
  python scripts/replay_consolidation.py --limit 3 --dry-run
  python scripts/replay_consolidation.py --limit 10
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.brain.value_encoder import llm_chat  # noqa: E402


def connect():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
    )


def replay_summary(core_content, fragments):
    """LLM 生成重放整合摘要（把片段并入核心记忆的叙述）。失败返回 None。"""
    if not fragments:
        return None
    frag_text = "\n".join("- " + str(f)[:200] for f in fragments[:3])
    prompt = (
        "你是记忆巩固系统。正在【重放】一条核心记忆，并把相关片段的信息整合进去。\n"
        "输出一段整合后的记忆叙述（150 字内）：\n"
        "1. 保留核心记忆的主干；\n"
        "2. 并入片段中的新细节（不编造）；\n"
        "3. 标注来源（片段编号）。\n"
        "核心记忆：" + str(core_content)[:400] + "\n"
        "相关片段：\n" + frag_text
    )
    return llm_chat(prompt, max_tokens=400, temperature=0.3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-importance", type=float, default=0.5)
    args = ap.parse_args()

    conn = connect()
    conn.autocommit = True
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    cur.execute("""
        SELECT memory_id, content, category,
               COALESCE(importance_score::float8, importance::float8, 0.0) AS imp,
               COALESCE((metadata->>'replay_count')::int, 0) AS rc
        FROM memories
        WHERE status = 'active'
          AND COALESCE(importance_score::float8, importance::float8, 0.0) >= %s
          AND COALESCE((metadata->>'replay_count')::int, 0) < 3
        ORDER BY imp DESC, created_at DESC
        LIMIT %s
    """, (args.min_importance, args.limit))
    rows = cur.fetchall()
    print(f"replay candidates: {len(rows)} (imp >= {args.min_importance})")

    done = 0
    for mid, content, category, imp, rc in rows:
        if content is None or not str(content).strip():
            continue
        # 相关片段：同 category + 非自身 + 近期
        cur.execute("""
            SELECT content FROM memories
            WHERE status = 'active'
              AND memory_id <> %s
              AND category = %s
              AND created_at::timestamptz > NOW() - INTERVAL '180 days'
            ORDER BY importance_score::float8 DESC NULLS LAST, created_at DESC
            LIMIT 3
        """, (mid, category or "general"))
        frags = [r[0] for r in cur.fetchall() if r[0]]
        if args.dry_run:
            print(f"  [dry] {str(mid)[:24]} imp={imp:.2f} rc={rc} frags={len(frags)}")
            done += 1
            continue
        summary = None
        if frags:
            summary = replay_summary(content, frags)
        meta = {
            "replay_count": rc + 1,
            "last_replayed_at": now,
        }
        if summary:
            meta["replay_summary"] = summary[:800]
        cur.execute("""
            UPDATE memories
            SET metadata = CASE
                    WHEN jsonb_typeof(metadata) = 'object' THEN metadata || %s::jsonb
                    ELSE '{}'::jsonb || %s::jsonb
                END,
                access_count = access_count + 1,
                last_accessed_at = %s::timestamptz,
                updated_at = %s::timestamptz
            WHERE memory_id = %s
        """, (json.dumps(meta, ensure_ascii=False), json.dumps(meta, ensure_ascii=False), now, now, mid))
        print(f"  {str(mid)[:24]} replayed rc={rc}->{rc+1} frags={len(frags)} "
              f"summary={'Y' if summary else 'N'}")
        done += 1
        time.sleep(0.2)

    print(f"DONE: replayed={done}")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
