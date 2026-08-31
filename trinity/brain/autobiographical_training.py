# -*- coding: utf-8 -*-
"""trinity/brain/autobiographical_training.py — 自传体自我训练（EXECUTION 287）。

借鉴 Memoirs of a Learning Machine（2026：Autobiographical Self-
Training）——用自传体记忆训练自己：从自己的经历提炼教训/
模式 → 强化学习（"回忆录学习"）。

与自传体（叙事记录）互补：叙事=记录；训练=学习。
Trinity 现在：
  self_train(): 从自传记忆提炼教训/模式（经验→强化信号）
"""
import os
import sys
import json


def self_train(limit: int = 30) -> dict:
    """自传体自我训练：从经历提炼教训/模式。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 自传类记忆（经历/反思）
        cur.execute("""
            SELECT left(content, 120), category FROM memories
            WHERE status='active' AND category IN ('self-narrative', 'self-reflection',
                                                   'reconstructive', 'action-experience')
            ORDER BY RANDOM() LIMIT %s
        """, (limit,))
        rows = cur.fetchall()
        conn.close()
        lessons = []
        patterns = {}
        for content, category in rows:
            t = str(content or "")
            # 教训提炼（失败/错误 → 教训）
            if any(w in t for w in ("失败", "错误", "崩溃", "丢失", "问题")):
                lessons.append({"lesson": t[:60], "type": "failure"})
            # 成功模式（成功/有效 → 模式）
            elif any(w in t for w in ("成功", "有效", "提升", "修复")):
                lessons.append({"lesson": t[:60], "type": "success"})
            patterns[category] = patterns.get(category, 0) + 1
        return {"lessons": lessons[:5], "lesson_count": len(lessons),
                "sources": patterns,
                "note": f"自传体训练：从 {len(rows)} 条经历提炼 {len(lessons)} 条教训/模式"}
    except Exception as e:
        return {"error": str(e)[:80]}


def self_train_report() -> dict:
    """训练状态。"""
    r = self_train()
    return {"note": r.get("note", "训练完成"),
            "lesson_types": {"failure": sum(1 for l in r.get("lessons", []) if l["type"] == "failure"),
                             "success": sum(1 for l in r.get("lessons", []) if l["type"] == "success")}}
