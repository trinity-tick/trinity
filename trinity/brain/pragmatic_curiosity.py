# -*- coding: utf-8 -*-
"""trinity/brain/pragmatic_curiosity.py — 实用好奇心（EXECUTION 252，大脑化）。

借鉴 Pragmatic Curiosity（2026：Active Inference 混合学习优化）——
好奇不只是探索未知，还要"值得"（实用信息价值）。

与好奇心（185 探索）互补：好奇=动机；实用=价值评估（探索过滤）。
Trinity 现在：
  pragmatic_value(topic): 好奇主题的实用价值（目标相关×可用性）
  curiosity_filter(): 好奇过滤（高价值才探索）
"""
import os
import sys
import json


def pragmatic_value(topic: str, goal_focus: str = "") -> dict:
    """实用价值评估：目标相关度 × 可用性 × 新颖度。"""
    # 目标相关（与当前关注/目标匹配）
    goal_rel = 0.5
    if goal_focus and goal_focus in topic:
        goal_rel = 1.0
    else:
        # 与全局自我关注匹配
        try:
            import psycopg2
            conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                    user="trinity", password="trinity", connect_timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT content FROM memories WHERE category='self-identity' LIMIT 1")
            r = cur.fetchone()
            conn.close()
            if r and str(topic)[:20] in str(r[0]):
                goal_rel = 0.8
        except Exception:
            pass
    # 可用性（知识覆盖低 = 探索收益高）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE content ILIKE %s", (f"%{topic[:15]}%",))
        cover = cur.fetchone()[0]
        conn.close()
        utility = min(1.0, cover / 10.0)
    except Exception:
        utility = 0.5
    value = goal_rel * 0.5 + (1 - utility) * 0.3 + 0.2
    return {"topic": str(topic)[:30], "goal_rel": round(goal_rel, 2),
            "knowledge_gap": round(1 - utility, 2),
            "value": round(value, 2),
            "worth_exploring": value >= 0.6}


def curiosity_filter(topics: list, min_value: float = 0.6) -> dict:
    """好奇过滤：只探索高价值主题（实用好奇心）。"""
    evaluated = []
    for t in topics[:10]:
        topic = t.get("topic", t) if isinstance(t, dict) else t
        v = pragmatic_value(str(topic))
        evaluated.append({"topic": str(topic)[:30], "value": v["value"],
                          "explore": v["worth_exploring"]})
    worthy = [e for e in evaluated if e["explore"]]
    return {"worthy": worthy, "filtered_out": len(evaluated) - len(worthy),
            "note": "只探索高价值好奇（实用好奇心）"}
