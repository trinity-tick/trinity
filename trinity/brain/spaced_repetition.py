# -*- coding: utf-8 -*-
"""trinity/brain/spaced_repetition.py — 间隔重复（EXECUTION 230，大脑化）。

艾宾浩斯遗忘曲线实用化：记忆在"最佳复习时点"复习最有效
（间隔重复——真实记忆训练原理）。Trinity 现在：
  schedule_review(): 计算各记忆遗忘预测（R=e^(-t/S)）→ 到期排序
  review_due(max): 到期记忆复习强化（优先将忘的——access+1）

与梦境（随机复习）互补：梦境=随机；间隔重复=按遗忘曲线定时。
"""
import os
import sys
import json
import math
import time


def _retention(t_elapsed_hours: float, strength: float) -> float:
    """艾宾浩斯保留率：R = e^(-t/S)。"""
    return math.exp(-t_elapsed_hours / max(strength, 1.0))


def schedule_review(limit: int = 100) -> dict:
    """遗忘预测：各记忆保留率 → 到期排序（最该复习的在前）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("""
            SELECT memory_id, left(content, 50), access_count, importance, created_at
            FROM memories
            WHERE status='active' AND created_at IS NOT NULL
              AND category NOT IN ('perception', 'self-identity', 'dcpm-core')
            ORDER BY RANDOM() LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        now = time.time()
        due = []
        for mid, content, access, importance, created_at in rows:
            # 时间经过（小时）
            try:
                from datetime import datetime
                ts = created_at
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
                else:
                    ts = ts.timestamp()
                elapsed_h = (now - ts) / 3600.0
            except Exception:
                elapsed_h = 24.0
            # 记忆强度（由访问次数决定——S = 5 + access*2）
            strength = 5.0 + (access or 0) * 2.0
            r = _retention(elapsed_h, strength)
            due.append({"memory_id": mid, "content": content,
                        "retention": round(r, 3), "elapsed_h": round(elapsed_h, 1),
                        "strength": round(strength, 1)})
        due.sort(key=lambda x: x["retention"])  # 保留率最低 = 最该复习
        return {"due": due[:10], "due_count": len(due),
                "note": "保留率最低的记忆最该复习（艾宾浩斯）"}
    except Exception as e:
        return {"error": str(e)[:80]}


def review_due(max_review: int = 5, write: bool = True) -> dict:
    """复习强化：到期记忆（保留率<0.5）→ access+1 + 重要性微升。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 直接查低保留率候选（简化：access 少 + 创建久）
        cur.execute("""
            SELECT memory_id, access_count, importance FROM memories
            WHERE status='active' AND (access_count < 3 OR access_count IS NULL)
              AND category NOT IN ('perception', 'self-identity', 'dcpm-core')
            ORDER BY created_at LIMIT %s
        """, (max_review,))
        rows = cur.fetchall()
        reviewed = 0
        for mid, access, importance in rows:
            if write:
                cur.execute("UPDATE memories SET access_count=%s, importance=%s WHERE memory_id=%s",
                            ((access or 0) + 1, min((importance or 0.5) + 0.02, 0.9), mid))
            reviewed += 1
        conn.commit()
        conn.close()
        return {"reviewed": reviewed, "write": write}
    except Exception as e:
        return {"error": str(e)[:80]}
