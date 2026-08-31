# -*- coding: utf-8 -*-
"""trinity/brain/behavioral_contagion.py — 行为传染（EXECUTION 232，大脑化）。

借鉴 Frontiers 2026（Behavioral contagion：他人态度调节我们的行动）——
社会传染：群体中情绪/态度自动传递（笑会传染、焦虑会传染）。

与观察学习（模仿）互补：观察=主动学习；传染=自动传递。
Trinity 现在：
  catch_attitude(agent_id): 接收他人态度（从信誉/活动/分享推断）
  contagion_effect(): 传染 → 自身行为倾向微调（乐观/谨慎）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/contagion_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"attitudes_seen": [], "current_lean": 0.0}  # -1 悲观 +1 乐观


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def catch_attitude(agent_id: str) -> dict:
    """接收他人态度：从信誉/活动/知识推断其情绪基调。"""
    attitude = 0.0  # -1 悲观 +1 乐观
    reasons = []
    try:
        import urllib.request, json as _j
        with urllib.request.urlopen(f"http://127.0.0.1:8001/market/reputation/{agent_id}", timeout=15) as resp:
            rep = _j.loads(resp.read().decode())
        score = rep.get("reputation", {}).get("score", 0)
        if score >= 0.5:
            attitude += 0.5
            reasons.append(f"{agent_id} 信誉良好（{round(score,2)}）——积极信号")
        elif score <= 0.1:
            attitude -= 0.3
            reasons.append(f"{agent_id} 信誉偏低——谨慎信号")
    except Exception:
        pass
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        cur.execute("SELECT content FROM memories WHERE category='social-memory' AND content LIKE %s LIMIT 1", (f"%{agent_id}%",))
        r = cur.fetchone()
        conn.close()
        if r and "分享" in str(r[0]):
            attitude += 0.2
            reasons.append(f"{agent_id} 积极分享知识——开放信号")
    except Exception:
        pass
    return {"agent": agent_id, "attitude": round(attitude, 2), "reasons": reasons}


def contagion_effect(agents: list, weight: float = 0.3) -> dict:
    """传染效应：群体态度 → 自身倾向微调。"""
    st = _load()
    total = 0.0
    details = []
    for a in agents[:5]:
        att = catch_attitude(a)
        total += att.get("attitude", 0)
        details.append({"agent": a, "attitude": att.get("attitude", 0)})
    if agents:
        avg = total / len(agents)
    else:
        avg = 0.0
    # 传染：当前倾向向群体平均微调
    current = st.get("current_lean", 0.0)
    new_lean = current * (1 - weight) + avg * weight
    st["current_lean"] = round(new_lean, 3)
    st["attitudes_seen"].append({"agents": len(agents), "avg": round(avg, 2),
                                 "ts": __import__("time").time()})
    st["attitudes_seen"] = st["attitudes_seen"][-20:]
    _save(st)
    tendency = "乐观" if new_lean > 0.2 else ("谨慎" if new_lean < -0.2 else "中性")
    return {"contagion_lean": round(new_lean, 3), "tendency": tendency,
            "group_avg": round(avg, 2), "details": details[:3]}
