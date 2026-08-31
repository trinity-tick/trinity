# -*- coding: utf-8 -*-
"""trinity/brain/observational_learning.py — 观察学习（EXECUTION 215，大脑化）。

借鉴 social/observational learning（lex-social-learning 等）——
从其他 Agent 的行为中学习（模仿/观察学习：人类文化的传播基础）。

Trinity 现在：
  observe_agent(agent_id): 观察 agent 行为模式（活动/交易/分享/检索）
  learn_from(agent_id): 把观察到的有效模式写入学习记忆
  （"我看到 agent-A 这样做" → 未来参考）

与社会记忆（194 知识传播）互补：传播=知识共享；观察学习=行为模式学习。
"""
import os
import sys
import json


def observe_agent(agent_id: str) -> dict:
    """观察 Agent 行为模式。"""
    pattern = {"agent": agent_id, "activities": [], "sharing": 0, "trades": 0}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 活动（审计）
        cur.execute("SELECT action, count(*) FROM audit_log WHERE agent_id=%s GROUP BY action ORDER BY count(*) DESC LIMIT 5", (agent_id,))
        pattern["activities"] = [{"action": r[0], "count": r[1]} for r in cur.fetchall()]
        # 分享
        cur.execute("SELECT count(*) FROM memories WHERE category='social-memory' AND content LIKE %s", (f"%{agent_id}%",))
        pattern["sharing"] = cur.fetchone()[0]
        conn.close()
    except Exception:
        pass
    # 市场交易（via API）
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:8001/market/reputation/{agent_id}", timeout=15) as resp:
            rep = json.loads(resp.read().decode())
        pattern["trades"] = rep.get("ledger_events", 0)
        pattern["reputation"] = round(rep.get("reputation", {}).get("score", 0), 3)
    except Exception:
        pass
    return pattern


def learn_from(agent_id: str, what: str = "") -> dict:
    """观察学习：把 agent 的有效行为模式写入学习记忆。"""
    obs = observe_agent(agent_id)
    learnings = []
    if obs.get("activities"):
        top = obs["activities"][0]
        learnings.append(f"{agent_id} 高频活动是 {top['action']}（{top['count']}次）")
    if obs.get("reputation") is not None and obs["reputation"] >= 0.4:
        learnings.append(f"{agent_id} 信誉良好（{obs['reputation']}）——其行为值得参考")
    if obs.get("sharing", 0) > 0:
        learnings.append(f"{agent_id} 活跃分享知识（{obs['sharing']}次）")
    if not learnings:
        return {"learned": False, "note": "无显著模式"}
    try:
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        text = f"[observational] 我观察学习了：{'；'.join(learnings[:3])}"
        m.ingest(text[:250], category="observational-learning",
                 tags=["social", "learning", agent_id], importance=0.65,
                 wait_backfill=True)
        return {"learned": True, "learnings": learnings[:3]}
    except Exception as e:
        return {"learned": False, "error": str(e)[:80]}
