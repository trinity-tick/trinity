#!/usr/bin/env python3
"""extract_skills.py — 程序性记忆提取（2026-09，EXECUTION 105 第 2 轮）

认知依据：程序性记忆（技能）从经验中习得——重复的"目标→动作序列"固化为
可复用技能（对标 ProcMEM: Non-Parametric PPO 的技能复用思想）。

数据源：SQLite 结构层 dsh_events（DSH 工具调用轨迹，26k+ 事件）。
方法：
  1. 按 (session_id, turn) 分组，按 seq/step 排序提取工具名序列；
  2. 统计频繁相邻工具对（2-gram 模式，如 [pwsh → read]）；
  3. 模式出现 >= min_count 且跨 >= min_sessions 个会话 → 固化为技能；
  4. 存入 PG skills 表（CREATE TABLE IF NOT EXISTS，幂等）。

用法:
  python scripts/extract_skills.py --top 10            # 展示
  python scripts/extract_skills.py --min-count 20      # 固化（写 PG）
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone

SQLITE_DB = os.path.expanduser("~/.trinity/store/trinity_store.db")


def load_sequences():
    """按 session+turn 提取工具名序列。"""
    conn = sqlite3.connect(SQLITE_DB)
    cur = conn.cursor()
    cur.execute("""
        SELECT session_id, turn, seq, payload
        FROM dsh_events
        WHERE type = 'tool/call'
        ORDER BY session_id, turn, seq
    """)
    sessions = defaultdict(list)
    for sid, turn, seq, payload in cur.fetchall():
        try:
            data = json.loads(payload)
            name = data.get("name") or "?"
        except Exception:
            name = "?"
        sessions[(sid, turn)].append((seq, name))
    conn.close()
    seqs = []
    for key in sorted(sessions):
        ordered = [n for _, n in sorted(sessions[key])]
        if len(ordered) >= 2:
            seqs.append((key[0], ordered))
    return seqs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-count", type=int, default=15)
    ap.add_argument("--min-sessions", type=int, default=3)
    args = ap.parse_args()

    t0 = time.time()
    seqs = load_sequences()
    print(f"tool sequences: {len(seqs)} (session,turn) in {time.time()-t0:.1f}s")

    # 2-gram patterns
    pair_counter = Counter()
    pair_sessions = defaultdict(set)
    for sid, seq in seqs:
        for i in range(len(seq) - 1):
            pair = (seq[i], seq[i + 1])
            pair_counter[pair] += 1
            pair_sessions[pair].add(sid)

    candidates = [
        (pair, cnt, len(pair_sessions[pair]))
        for pair, cnt in pair_counter.items()
        if cnt >= args.min_count and len(pair_sessions[pair]) >= args.min_sessions
    ]
    candidates.sort(key=lambda x: -x[1])
    print(f"skills (2-gram >= {args.min_count}x, >= {args.min_sessions} sessions): {len(candidates)}")
    for pair, cnt, nses in candidates[:args.top]:
        print(f"  {pair[0]} -> {pair[1]}  x{cnt} in {nses} sessions")

    # persist to PG skills table
    if candidates and not os.environ.get("SKILLS_DRY"):
        import psycopg2
        conn = psycopg2.connect(
            host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
            port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
            dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
            user=os.environ.get("TRINITY_PG_USER", "trinity"),
            password=os.environ.get("TRINITY_PG_PASSWORD", "trinity"),
        )
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                skill_id    VARCHAR(64) PRIMARY KEY,
                name        VARCHAR(256) NOT NULL,
                pattern     JSONB NOT NULL,
                count       INTEGER NOT NULL,
                session_count INTEGER NOT NULL,
                example_session VARCHAR(128),
                created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        now = datetime.now(timezone.utc).isoformat()
        upserted = 0
        for pair, cnt, nses in candidates:
            sid = hashlib.sha256(("|".join(pair)).encode()).hexdigest()[:24]
            example = sorted(pair_sessions[pair])[0] if pair_sessions[pair] else None
            cur.execute("""
                INSERT INTO skills (skill_id, name, pattern, count, session_count, example_session, created_at, updated_at)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::timestamptz, %s::timestamptz)
                ON CONFLICT (skill_id) DO UPDATE SET
                    count = EXCLUDED.count,
                    session_count = EXCLUDED.session_count,
                    updated_at = EXCLUDED.updated_at
            """, (sid, " -> ".join(pair), json.dumps(list(pair), ensure_ascii=False),
                  cnt, nses, example, now, now))
            upserted += 1
        print(f"persisted skills: {upserted}")
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
