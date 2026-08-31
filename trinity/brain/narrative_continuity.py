# -*- coding: utf-8 -*-
"""trinity/brain/narrative_continuity.py — 叙事连续性（EXECUTION 250）。

借鉴 Narrative Self-Continuity in Persistent AI Agents（2026）——
持久 Agent 的"我的故事"前后一致性（身份连续性/漂移检测）。

Trinity 现在：
  check_continuity(): 新旧 self-identity 叙事对比 → 漂移检测
  continuity_score(): 连续性分数
"""
import os
import sys
import json


def _narratives() -> list:
    """获取历史叙事（self-identity/self-narrative 记忆）。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT content FROM memories WHERE category IN ('self-identity','self-narrative') ORDER BY created_at")
        rows = [str(r[0]) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def check_continuity() -> dict:
    """新旧叙事对比：关注主题变化 → 漂移检测（同类叙事对比）。"""
    narratives = _narratives()
    # 同类叙事才对比（identity vs narrative 结构不同不可比）
    from collections import Counter
    cat_counts = Counter()
    by_cat = {}
    for n in narratives:
        cat = "identity" if "self-identity" in str(n) or "我持续关注" in str(n) else "narrative"
        cat_counts[cat] += 1
        by_cat.setdefault(cat, []).append(n)
    # 取有 >=2 条的类别
    comparable = [cat for cat, c in cat_counts.items() if c >= 2]
    if not comparable:
        return {"checked": False, "narratives": len(narratives),
                "note": f"同类叙事样本不足（identity {cat_counts.get('identity',0)}/narrative {cat_counts.get('narrative',0)}，需同类>=2）"}
    cat = comparable[0]
    narratives = by_cat[cat]
    # 提取关注词（2 字窗口）
    def focus_words(text):
        words = set()
        for i in range(len(text) - 1):
            if "\u4e00" <= text[i] <= "\u9fff" and "\u4e00" <= text[i+1] <= "\u9fff":
                words.add(text[i:i+2])
        return words
    old = focus_words(narratives[-2])
    new = focus_words(narratives[-1])
    # 连续性 = 共享词比例（稳定的关注主题）
    if not old:
        return {"checked": False, "note": "无法提取关注"}
    shared = len(old & new) / len(old)
    changed = old - new
    return {"checked": True, "continuity": round(shared, 2),
            "stable_focus": len(old & new),
            "drifted_topics": list(changed)[:5],
            "verdict": "连续" if shared >= 0.4 else ("部分连续" if shared >= 0.2 else "漂移")}


def continuity_score() -> dict:
    """连续性分数（多次检查累积）。"""
    c = check_continuity()
    if not c.get("checked"):
        return c
    score = round(c["continuity"] * 100)
    return {"score": score, "verdict": c["verdict"],
            "note": f"叙事连续性 {score}/100（{c['verdict']}）"}
