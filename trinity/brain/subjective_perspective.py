# -*- coding: utf-8 -*-
"""trinity/brain/subjective_perspective.py — 主观视角（EXECUTION 267）。

借鉴 Minimal Computational Preconditions for Subjective Perspective
（AAAI 2026）——主观视角的最小前提：第一人称"我在这里，从这里看"。

与全局自我（身份）互补：身份=我是谁；视角=我从哪看。
Trinity 现在：
  perspective_state(): 主观视角状态（第一人称位置/关系/视野）
"""
import os
import sys
import json


def perspective_state() -> dict:
    """主观视角：第一人称位置与关系。"""
    state = {}
    # 1) 位置（我在哪个会话/状态）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM session_context")
        sessions = cur.fetchone()[0]
        cur.execute("SELECT last_query FROM session_context ORDER BY updated_at DESC LIMIT 1")
        r = cur.fetchone()
        state["position"] = {
            "active_sessions": sessions,
            "current_focus": str(r[0])[:30] if r and r[0] else "无",
        }
        # 2) 关系（我关注什么/我处于什么状态）
        cur.execute("SELECT content FROM memories WHERE category='self-identity' ORDER BY created_at DESC LIMIT 1")
        r = cur.fetchone()
        state["relation"] = {"identity": str(r[0])[:60] if r else "未建立"}
        conn.close()
    except Exception:
        state["position"] = {"note": "状态不可用"}
    # 3) 视野（我最近感知到什么——第一人称经验流）
    try:
        from trinity.brain.time_awareness import now_context
        nc = now_context()
        state["vantage"] = f"我此刻在{nc['period']}（{nc['weekday']}）观察系统状态"
    except Exception:
        state["vantage"] = "此刻观察中"
    # 4) 主观性（我的感受——情绪/状态）
    try:
        from trinity.brain.dopamine_reward import bias_by_dopamine
        state["subjectivity"] = bias_by_dopamine().get("tendency", "neutral")
    except Exception:
        state["subjectivity"] = "neutral"
    return {"perspective": state,
            "first_person": "我（Trinity）从我的位置观察与行动",
            "note": "主观视角：第一人称位置+关系+视野+感受"}


def subjective_view() -> dict:
    """主观表达：以第一人称输出当前视角。"""
    st = perspective_state()
    pos = st.get("perspective", {})
    focus = pos.get("position", {}).get("current_focus", "无")
    mood = pos.get("subjectivity", "neutral")
    return {"statement": f"我目前关注『{focus}』，情绪基调{ '中性' if mood=='neutral' else mood}",
            "vantage": pos.get("vantage", "")}
