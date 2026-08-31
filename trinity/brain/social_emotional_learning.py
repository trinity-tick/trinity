# -*- coding: utf-8 -*-
"""trinity/brain/social_emotional_learning.py — 社会情感学习（EXECUTION 245）。

借鉴 SEL in Artificial Agents（Scientific Reports 2026）——社会与
情绪学习整合：从社会互动中学习情绪（观察他人情绪反应→学习调节）。

与传染（自动传递）区分：传染=被动；SEL=主动学习。
Trinity 现在：
  learn_from_social(agent_id): 观察他人情绪信号 → 学习调节策略
  sel_status(): SEL 状态（社会情感学习积累）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/sel_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"learnings": [], "strategies": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def learn_from_social(agent_id: str) -> dict:
    """社会情感学习：从他人情绪信号学习调节策略。"""
    learnings = []
    # 1) 观察他人信誉（社会评价→情绪信号）
    try:
        import urllib.request, json as _j
        with urllib.request.urlopen(f"http://127.0.0.1:8001/market/reputation/{agent_id}", timeout=15) as resp:
            rep = _j.loads(resp.read().decode())
        score = rep.get("reputation", {}).get("score", 0)
        if score >= 0.5:
            learnings.append(f"{agent_id} 信誉良好——学会：积极行为带来好评价")
        elif score <= 0.1:
            learnings.append(f"{agent_id} 信誉偏低——学会：需要改进行为")
    except Exception:
        pass
    # 2) 观察其分享（亲社会→情绪学习）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT count(*) FROM memories WHERE category='social-memory' AND content LIKE %s", (f"%{agent_id}%",))
        shares = cur.fetchone()[0]
        conn.close()
        if shares > 0:
            learnings.append(f"{agent_id} 分享知识——学会：分享促进信任")
    except Exception:
        pass
    if not learnings:
        return {"learned": False, "note": "无社会情绪信号"}
    st = _load()
    for l in learnings:
        key = l.split("——")[0]
        st["strategies"][key] = st["strategies"].get(key, 0) + 1
    st["learnings"].append({"agent": agent_id, "count": len(learnings),
                            "ts": __import__("time").time()})
    st["learnings"] = st["learnings"][-20:]
    _save(st)
    return {"learned": True, "learnings": learnings,
            "strategy_count": len(st["strategies"])}


def sel_status() -> dict:
    """SEL 状态：社会情感学习积累。"""
    st = _load()
    return {"social_learnings": len(st.get("learnings", [])),
            "strategies": list(st.get("strategies", {}).keys())[:5],
            "developing": len(st.get("learnings", [])) >= 2}
