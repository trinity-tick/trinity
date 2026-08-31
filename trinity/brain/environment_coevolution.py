# -*- coding: utf-8 -*-
"""trinity/brain/environment_coevolution.py — 环境共进化（EXECUTION 279）。

借鉴 Self-Evolving Agents Survey（2026：Model-Centric to
Environment-Driven Co-Evolution）——从环境反馈进化：外部信号
→ 进化方向调整（不只是内部策略）。

与内部进化（策略/反思）互补：内部=自省；环境=外部驱动。
Trinity 现在：
  environment_signal(): 环境信号（外部变化/新信息/用户反馈）
  coevolve(): 共进化（环境信号→进化方向）
"""
import os
import sys
import json


def environment_signal() -> dict:
    """环境信号：外部变化检测。"""
    signals = []
    # 1) 新信息流（感知量）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("""
            SELECT count(*) FROM memories
            WHERE category='perception' AND created_at::timestamp > NOW() - interval '24 hours'
        """)
        new_info = cur.fetchone()[0]
        signals.append({"type": "information", "value": new_info,
                        "meaning": "新信息流入" if new_info > 10 else "信息平稳"})
        # 2) 用户反馈（互动量）
        cur.execute("""
            SELECT count(*) FROM audit_log
            WHERE timestamp::timestamp > NOW() - interval '24 hours'
        """)
        interaction = cur.fetchone()[0]
        signals.append({"type": "interaction", "value": interaction,
                        "meaning": "互动活跃" if interaction > 20 else "互动平缓"})
        conn.close()
    except Exception:
        signals.append({"type": "state", "value": 0, "meaning": "不可用"})
    return {"signals": signals, "count": len(signals)}


def coevolve() -> dict:
    """共进化：环境信号 → 进化方向建议。"""
    env = environment_signal()
    directions = []
    for s in env.get("signals", []):
        if s["type"] == "information" and s.get("value", 0) > 10:
            directions.append({"area": "knowledge", "action": "expand",
                               "note": "信息活跃——加强知识整合"})
        elif s["type"] == "interaction" and s.get("value", 0) <= 20:
            directions.append({"area": "engagement", "action": "proactive",
                               "note": "互动平缓——建议主动交互"})
    if not directions:
        directions.append({"area": "stable", "action": "consolidate",
                           "note": "环境平稳——巩固现有"})
    return {"environment": env, "evolution_directions": directions,
            "note": "环境驱动共进化（模型中心→环境驱动）"}
