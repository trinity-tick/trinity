# -*- coding: utf-8 -*-
"""trinity/brain/continuous_feedback.py — 连续内部状态反馈（EXECUTION 271）。

借鉴 ALICE（2026：Continuous Internal States Feedback Mechanism）——
内部状态连续反馈到行为调节（自主终身学习：状态→行为→新状态）。

Trinity 现在：
  internal_state(): 聚合内部状态（情绪/多巴胺/健康/能量）
  feedback_loop(): 连续反馈（状态→调节建议→更新）
"""
import os
import sys
import json


def internal_state() -> dict:
    """聚合内部状态。"""
    state = {}
    # 情绪状态
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT affect FROM session_context WHERE id='ctx:default'")
        r = cur.fetchone()
        if r and r[0]:
            import ast
            aff = ast.literal_eval(r[0]) if isinstance(r[0], str) else r[0]
            state["emotion"] = aff.get("polarity", "neu")
            state["valence"] = round(float(aff.get("valence", 0)), 2)
        conn.close()
    except Exception:
        state["emotion"] = "neu"
    # 多巴胺（奖赏水平）
    try:
        from trinity.brain.dopamine_reward import dopamine_level
        state["dopamine"] = round(dopamine_level(), 2)
    except Exception:
        state["dopamine"] = 0.5
    # 健康（内部完整性）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE embedding IS NULL AND status='active'")
        state["integrity"] = "ok" if cur.fetchone()[0] < 10 else "degraded"
        conn.close()
    except Exception:
        state["integrity"] = "ok"
    return state


def feedback_loop() -> dict:
    """连续反馈：内部状态 → 行为调节建议 → 状态更新。"""
    st = internal_state()
    adjustments = []
    # 情绪调节建议
    if st.get("emotion") == "neg" and st.get("valence", 0) < -0.5:
        adjustments.append({"area": "emotion", "action": "regulate",
                            "note": "消极过度——建议情绪调节"})
    # 多巴胺调节（探索倾向）
    if st.get("dopamine", 0.5) >= 0.65:
        adjustments.append({"area": "exploration", "action": "boost",
                            "note": "奖赏充足——适合探索"})
    elif st.get("dopamine", 0.5) <= 0.35:
        adjustments.append({"area": "conservation", "action": "conserve",
                            "note": "奖赏低迷——建议保守"})
    # 健康调节
    if st.get("integrity") == "degraded":
        adjustments.append({"area": "self_heal", "action": "heal",
                            "note": "完整性下降——建议自愈"})
    return {"state": st, "adjustments": adjustments,
            "continuous": True,
            "note": "连续内部状态反馈（状态→调节→新状态）"}
