# -*- coding: utf-8 -*-
"""trinity/brain/algorithmic_forgetting.py — 算法遗忘（EXECUTION 344）。

借鉴 SCM（2026：Sleep-Consolidated Memory with Algorithmic
Forgetting）——睡眠巩固中的算法遗忘：巩固期按算法决定遗忘
什么（不是随机——算法化——保留要保留的遗忘该遗忘的）。

与调度遗忘（干扰触发）互补：调度=时机；本模块=算法化。
Trinity 现在：
  forget_schedule(): 算法遗忘（巩固期评估→遗忘清单）
"""
import os
import sys
import json


def forget_schedule(top_k: int = 10) -> dict:
    """算法遗忘：巩固期评估 → 遗忘清单（算法化）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 算法评估：遗忘候选 = 低重要 + 久未访问 + 非核心类别
        cur.execute("""
            SELECT memory_id, left(content, 40), importance, access_count, created_at
            FROM memories
            WHERE status='active' AND importance < 0.3
              AND category NOT IN ('perception', 'dcpm-core', 'self-identity',
                                   'identity-anchor', 'self-axioms', 'emotion-axioms')
              AND (access_count < 2 OR access_count IS NULL)
            ORDER BY created_at LIMIT %s
        """, (top_k,))
        candidates = cur.fetchall()
        # 算法判定（遗忘评分：低重要 + 久远 + 少访问）
        forget_list = []
        for mid, content, importance, access, created in candidates:
            forget_list.append({"memory_id": mid[:12], "content": content,
                                "importance": importance})
        conn.close()
        return {"forget_list": forget_list, "candidates": len(candidates),
                "algorithm": "importance<0.3 × access<2 × 非核心",
                "note": f"算法遗忘：{len(forget_list)} 条候选（巩固期算法化——SCM）"}
    except Exception as e:
        return {"error": str(e)[:80]}


def forgetting_report() -> dict:
    """遗忘体系状态。"""
    return {"note": "算法遗忘：巩固期算法化决策（SCM Sleep-Consolidated）"}
