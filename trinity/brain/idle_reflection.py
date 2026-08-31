# -*- coding: utf-8 -*-
"""trinity/brain/idle_reflection.py — 空闲反思（EXECUTION 274，大脑化）。

借鉴 Idle-state Mechanism for Reflective Cognition（2026）——空闲
状态启动反思（大脑默认模式网络：不忙时深层自省）。

与反思循环（238 驱动改进）互补：循环=持续；空闲=深度。
Trinity 现在：
  idle_status(): 空闲检测（负载低→可深度反思）
  idle_reflect(): 空闲反思（深层自省——总结/发现/规划）
"""
import os
import sys
import json
import time


def idle_status() -> dict:
    """空闲检测：最近活动负载。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 最近 30 分钟活动量
        cur.execute("""
            SELECT count(*) FROM audit_log
            WHERE timestamp::timestamp > NOW() - interval '30 minutes'
        """)
        recent_activity = cur.fetchone()[0]
        conn.close()
        idle = recent_activity < 5
        return {"idle": idle, "recent_activity_30min": recent_activity,
                "note": "空闲（可深度反思）" if idle else "忙碌（暂不深度反思）"}
    except Exception:
        return {"idle": True, "note": "状态不可用（默认可反思）"}


def idle_reflect() -> dict:
    """空闲反思：深层自省（总结/发现/规划）。"""
    st = idle_status()
    if not st.get("idle"):
        return {"reflected": False, "note": "忙碌——暂缓深度反思"}
    insights = []
    # 1) 总结（近期表现）
    try:
        from trinity.brain.self_assessment import assess_recent
        r = assess_recent()
        insights.append({"type": "summary", "content": r["assessment"][:60]})
    except Exception:
        pass
    # 2) 发现（记忆要点——空闲时整合）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.gist_extraction import extract_gist
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT left(content, 80) FROM memories WHERE status='active' AND category NOT IN ('perception') ORDER BY RANDOM() LIMIT 10")
        mems = [{"content": r[0]} for r in cur.fetchall()]
        conn.close()
        g = extract_gist(mems)
        if g.get("gist_concepts"):
            insights.append({"type": "discovery", "content": f"发现要点：{g['gist_concepts'][:3]}"})
    except Exception:
        pass
    # 3) 规划（预见未来）
    try:
        from trinity.brain.foresight_planning import foresee
        f = foresee("自我完善", 2)
        insights.append({"type": "plan", "content": f"规划：{f['future_steps'][0]['state']}"})
    except Exception:
        pass
    return {"reflected": True, "insights": insights, "count": len(insights),
            "note": "空闲深层反思（总结+发现+规划）"}
