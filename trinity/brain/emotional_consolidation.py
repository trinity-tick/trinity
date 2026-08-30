# -*- coding: utf-8 -*-
"""trinity/brain/emotional_consolidation.py — 情绪记忆巩固（EXECUTION 189，大脑化）。

杏仁核效应：情绪唤醒的记忆比中性记忆更牢固（巩固更强、遗忘更慢）。
Trinity 现在：
  1. 扫描近期记忆 → affect.assess 标记情绪强度
  2. 高情绪记忆 → 强化（access+1 + importance 微升——情绪增强）
  3. 情绪记忆进入 forgetting 保护名单（不易被修剪）

大脑对应：杏仁核调制海马巩固——情绪增强记忆。
"""
import os
import sys
import json


def _affect_strength(text: str) -> float:
    """情绪强度（0 中性 - 1 强烈）：从情感极性评估。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect import assess
        r = assess(str(text)[:200])
        pol = r.get("polarity", "neu")
        if pol == "neu":
            return 0.0
        return min(abs(float(r.get("valence", 0))) + float(r.get("arousal", 0)), 1.0)
    except Exception:
        return 0.0


def emotional_consolidate(limit: int = 100, write: bool = True) -> dict:
    """情绪记忆巩固：扫描→标记→强化。"""
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
    cur = conn.cursor()
    # 最近记忆（跳过已有情绪标记的感知类）
    cur.execute("""
        SELECT memory_id, content, importance, access_count
        FROM memories
        WHERE status='active'
          AND category NOT IN ('perception', 'self-identity', 'dcpm-core')
        ORDER BY created_at DESC
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()

    emotional = []
    neutral = 0
    for mid, content, imp, acc in rows:
        strength = _affect_strength(content or "")
        if strength >= 0.5:
            emotional.append({"memory_id": mid, "strength": round(strength, 2),
                              "importance": float(imp or 0.5)})
            if write:
                # 杏仁核效应：情绪记忆强化（importance 微升 + access+1）
                new_imp = min(float(imp or 0.5) + 0.05, 0.95)
                cur.execute("UPDATE memories SET importance=%s, access_count=%s WHERE memory_id=%s",
                            (new_imp, (acc or 0) + 1, mid))
        else:
            neutral += 1
    conn.commit()
    conn.close()
    return {"scanned": len(rows), "emotional": len(emotional),
            "neutral": neutral, "write": write,
            "emotional_ids": [e["memory_id"][:10] for e in emotional[:5]]}


def protect_emotional_from_forgetting(limit: int = 100) -> dict:
    """情绪记忆遗忘保护：高情绪记忆 importance 提升到遗忘阈值之上。"""
    import psycopg2
    conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user="trinity", password="trinity")
    cur = conn.cursor()
    cur.execute("""
        SELECT memory_id, content FROM memories
        WHERE status='active' AND (importance < 0.3 OR importance IS NULL)
        LIMIT %s
    """, (limit,))
    rows = cur.fetchall()
    protected = 0
    for mid, content in rows:
        strength = _affect_strength(content or "")
        if strength >= 0.5:
            cur.execute("UPDATE memories SET importance=0.35 WHERE memory_id=%s", (mid,))
            protected += 1
    conn.commit()
    conn.close()
    return {"checked": len(rows), "protected": protected}
