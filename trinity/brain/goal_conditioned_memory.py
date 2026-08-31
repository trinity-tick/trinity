# -*- coding: utf-8 -*-
"""trinity/brain/goal_conditioned_memory.py — 目标条件化记忆（EXECUTION 251）。

借鉴 LOCI（Focused Retention: Goal-Conditioned Switched Decay）——
目标相关记忆防衰减（目标驱动保留：与目标相关的记忆不修剪）。

与遗忘（修剪）/情绪（保护）互补：遗忘=价值；情绪=情感；目标=目标相关。
Trinity 现在：
  protect_by_goal(goal): 目标相关记忆 → 防衰减保护（importance 提升）
  goal_retention(): 目标记忆保留状态
"""
import os
import sys
import json


def protect_by_goal(goal: str, limit: int = 20) -> dict:
    """目标保护：与目标相关的记忆提升重要性（防衰减）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 目标相关记忆（词命中）
        cur.execute("""
            SELECT memory_id, content FROM memories
            WHERE status='active' AND content ILIKE %s
              AND category NOT IN ('perception', 'dcpm-core')
            ORDER BY RANDOM() LIMIT %s
        """, (f"%{goal[:20]}%", limit))
        rows = cur.fetchall()
        protected = 0
        for mid, content in rows:
            # 重要性提升到保护阈值（>0.3 防遗忘）
            cur.execute("UPDATE memories SET importance = GREATEST(importance, 0.4) WHERE memory_id=%s", (mid,))
            protected += 1
        conn.commit()
        conn.close()
        return {"protected": protected, "goal": str(goal)[:30],
                "note": f"与目标『{goal[:20]}』相关的 {protected} 条记忆已保护"}
    except Exception as e:
        return {"error": str(e)[:80]}


def goal_retention(goal: str = "") -> dict:
    """目标记忆保留状态。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE importance >= 0.4 AND status='active'")
        protected_total = cur.fetchone()[0]
        conn.close()
        return {"protected_memories": protected_total,
                "goal": goal or "all",
                "note": "受保护记忆（>=0.4 防遗忘）"}
    except Exception as e:
        return {"error": str(e)[:80]}
