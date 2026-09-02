#!/usr/bin/env python3
"""cognition_agent.py — 主动主体性循环（2026-09，EXECUTION 105.22）
# NOTICE(EXECUTION 458C): 通用主动主体（任务 cognition-agent）——分工见 docs/RUNNER_MAP.md §2。

Trinity 从"响应式"到"主动式"：每轮扫描——
  1. 开放知识缺口（gaps open）→ 主动思考（检索相关记忆 + 生成建议）
  2. 最近感知事件（tool_error 感知记忆）→ 主动分析（经验沉淀）
主动思考结果写回记忆（category=proactive_thought），水位幂等。

用法:
  python scripts/cognition_agent.py --dry-run
  python scripts/cognition_agent.py --limit 3
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trinity.cognition.engine import think  # noqa: E402


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
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=3)
    args = ap.parse_args()

    conn = connect_pg()
    conn.autocommit = True
    cur = conn.cursor()
    triggers = []
    # 1) 开放缺口（最近 7 天，未处理）
    cur.execute("""
        SELECT gap_id, query FROM gaps
        WHERE status = 'open' AND detected_at > NOW() - INTERVAL '7 days'
        ORDER BY detected_at DESC LIMIT %s
    """, (args.limit,))
    for gid, q in cur.fetchall():
        triggers.append({"type": "gap", "id": gid, "query": str(q)[:120]})
    # 2) 最近感知错误事件（24h，未沉淀为主动思考）
    cur.execute("""
        SELECT memory_id, content FROM memories
        WHERE category = 'perception' AND status = 'active'
          AND created_at::timestamptz > NOW() - INTERVAL '24 hours'
          AND metadata->>'proactive_thought' IS NULL
        ORDER BY created_at DESC LIMIT %s
    """, (args.limit,))
    for mid, content in cur.fetchall():
        triggers.append({"type": "perception", "id": str(mid),
                         "query": str(content)[:150]})
    conn.close()
    print("triggers: " + str(len(triggers)))
    if not triggers:
        return 0
    done = 0
    for tr in triggers[:args.limit]:
        goal = tr["query"]
        result = think(goal, session_id="proactive-agent")
        print("  [" + tr["type"] + "] " + goal[:50])
        print("    reasoning: " + str(result.get("reasoning"))[:100])
        if args.dry_run:
            done += 1
            continue
        # 写回：主动思考沉淀 + 标记处理
        try:
            conn = connect_pg()
            conn.autocommit = True
            cur = conn.cursor()
            import hashlib
            content = "主动思考[" + tr["type"] + "]: " + str(result.get("reasoning"))[:400]
            cur.execute("""
                INSERT INTO memories
                    (memory_id, session_id, persona_id, tenant_id, agent_id,
                     content, importance, importance_score, status, category,
                     modality, content_hash, created_at, updated_at)
                VALUES (uuid_generate_v4(), 'proactive-agent', 'default', 'default', 'cognition',
                        %s, 0.5, 0.5, 'active', 'proactive_thought', 'text',
                        encode(sha256(%s::bytea), 'hex'), NOW(), NOW())
            """, (content, content))
            if tr["type"] == "perception":
                cur.execute(
                    "UPDATE memories SET metadata = CASE "
                    "WHEN jsonb_typeof(metadata)='object' THEN metadata || "
                    "'{\"proactive_thought\":true}'::jsonb ELSE "
                    "'{\"proactive_thought\":true}'::jsonb END "
                    "WHERE memory_id=%s", (tr["id"],))
            else:
                cur.execute(
                    "UPDATE gaps SET status='resolved', resolution=%s, "
                    "resolved_at=NOW() WHERE gap_id=%s",
                    ("主动思考已覆盖", tr["id"]))
            conn.close()
            done += 1
        except Exception as e:
            print("    writeback fail: " + str(e)[:80])
        time.sleep(0.5)
    print("DONE: proactive=" + str(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
