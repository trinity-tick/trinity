# -*- coding: utf-8 -*-
"""trinity/brain/opponent_awareness.py — 对手意识（EXECUTION 296，大脑化）。

借鉴 History-Informed Opponent Awareness（2026：Evolving in the
Agent Jungle）——用交互历史感知对手行为模式（历史信息→
预测对手→进化竞争）。

与 ToM（心智推断）互补：ToM=当前推断；本模块=历史模式。
Trinity 现在：
  history_aware(agent_id): 历史感知（交互记录→行为模式/策略）
"""
import os
import sys
import json


def history_aware(agent_id: str) -> dict:
    """历史感知：交互历史 → 对手行为模式。"""
    patterns = {"cooperative": 0, "competitive": 0, "neutral": 0}
    insights = []
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 交互历史（社会记忆）
        cur.execute("""
            SELECT content FROM memories
            WHERE category='social-memory' AND content LIKE %s
            ORDER BY created_at DESC LIMIT 10
        """, (f"%{agent_id}%",))
        rows = [str(r[0]) for r in cur.fetchall()]
        # 行为模式分类
        for r in rows:
            if any(w in r for w in ("协作", "分享", "帮助", "合作")):
                patterns["cooperative"] += 1
            elif any(w in r for w in ("竞争", "争夺", "冲突", "对抗")):
                patterns["competitive"] += 1
            else:
                patterns["neutral"] += 1
        conn.close()
        # 模式判定
        if patterns["cooperative"] >= 2:
            mode = "cooperative"
            insights.append(f"{agent_id} 历史上偏向合作——可协作")
        elif patterns["competitive"] >= 2:
            mode = "competitive"
            insights.append(f"{agent_id} 历史上偏向竞争——需谨慎")
        else:
            mode = "unclear"
            insights.append(f"{agent_id} 历史模式不明确——保持观察")
    except Exception as e:
        return {"error": str(e)[:80]}
    return {"agent": agent_id, "mode": mode, "patterns": patterns,
            "insights": insights,
            "note": f"历史感知：{agent_id} 行为模式 = {mode}"}
