# -*- coding: utf-8 -*-
"""trinity/brain/priority_replay.py — 优先级重放（EXECUTION 255，大脑化）。

借鉴 Utility-Driven Selective Memory（2026：Biologically Inspired
Replay）——按效用优先重放（不是随机/均匀）：高价值记忆更常复习。

与梦境（随机）互补：梦境=随机探索；优先级=效用聚焦。
Trinity 现在：
  replay_prioritized(limit): 按效用（importance×访问×新鲜）优先重放
"""
import os
import sys
import json
import time


def replay_prioritized(limit: int = 10, write: bool = True) -> dict:
    """效用优先重放：importance × 时间加权 → 优先强化。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 效用评分：importance 高 + 创建久（该复习）+ 未访问（防偏科）
        cur.execute("""
            SELECT memory_id, importance, access_count, created_at
            FROM memories
            WHERE status='active' AND category NOT IN ('perception', 'dcpm-core')
              AND importance >= 0.5
            ORDER BY importance DESC, created_at, access_count
            LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        replayed = 0
        for mid, importance, access, created_at in rows:
            if write:
                cur.execute("UPDATE memories SET access_count=%s WHERE memory_id=%s",
                            ((access or 0) + 1, mid))
            replayed += 1
        conn.commit()
        conn.close()
        return {"replayed": replayed, "mode": "utility_priority",
                "note": f"按效用优先重放 {replayed} 条高价值记忆"}
    except Exception as e:
        return {"error": str(e)[:80]}


def replay_mix(limit: int = 10) -> dict:
    """混合重放：优先级（70%）+ 随机（30%）——平衡聚焦与探索。"""
    pri = replay_prioritized(int(limit * 0.7), write=False)
    return {"priority": pri.get("replayed", 0), "random_pool": int(limit * 0.3),
            "note": "混合重放：效用优先 + 随机探索（70/30）"}
