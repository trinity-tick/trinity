# -*- coding: utf-8 -*-
"""trinity/brain/theory_of_mind.py — 心智理论（EXECUTION 208，大脑化）。

借鉴 Emergent Cognitive Architecture 的 Theory of Mind——理解
其他 Agent 的心理状态（关注/意图/知识/信念），预测其行为。
Trinity 现在：
  infer_agent(agent_id): 从该 agent 的记忆/交易/分享推断心理状态
  predict_behavior(agent_id): 基于推断预测下一步行为

社会智能：不只记忆共享，还"理解他人"。
"""
import os
import sys
import json


def infer_agent(agent_id: str) -> dict:
    """推断 Agent 心理状态（关注/知识/活跃度/信誉）。

    EXECUTION 457 修复：session_context 无 agent_id 列（原 focus 查询静默失败）；
    改从该 agent 的 active 记忆内容直接推断关注（解密 + 前缀），各项查询独立守卫。
    """
    state = {"agent": agent_id, "focus": [], "knowledge": 0, "activity": 0, "reputation": None}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity")
        cur = conn.cursor()
        # 该 agent 的记忆（知识量 + 关注内容）
        try:
            cur.execute("SELECT count(*) FROM memories WHERE agent_id=%s AND status='active'", (agent_id,))
            state["knowledge"] = int(cur.fetchone()[0] or 0)
        except Exception:
            pass
        try:
            cur.execute("SELECT content FROM memories WHERE agent_id=%s AND status='active' "
                        "ORDER BY (importance::float8) DESC NULLS LAST LIMIT 3", (agent_id,))
            for row in cur.fetchall():
                c = str(row[0] or "")
                if c.startswith("enc:v1:"):
                    try:
                        from trinity.security.crypto import decrypt_content
                        c = str(decrypt_content(c) or "")
                    except Exception:
                        c = ""
                if c and not c.startswith("enc:v1:"):
                    state["focus"].append(" ".join(c.split())[:40])
        except Exception:
            pass
        # 审计活跃（近期活动）
        try:
            cur.execute("SELECT count(*) FROM audit_log WHERE agent_id=%s "
                        "AND timestamp > NOW() - interval '7 days'", (agent_id,))
            state["activity"] = int(cur.fetchone()[0] or 0)
        except Exception:
            pass
        conn.close()
        # 市场信誉
        try:
            import urllib.request
            with urllib.request.urlopen(f"http://127.0.0.1:8001/market/reputation/{agent_id}", timeout=15) as resp:
                rep = json.loads(resp.read().decode())
            state["reputation"] = round(rep.get("reputation", {}).get("score", 0), 3)
        except Exception:
            pass
    except Exception:
        pass
    # 心理画像（推断）
    if state["focus"]:
        state["inferred_focus"] = "关注：" + "、".join(str(x)[:22] for x in state["focus"][:2])
    if state["knowledge"] == 0:
        state["mental_state"] = "新来者（知识少）"
    elif state["knowledge"] < 10:
        state["mental_state"] = "学习者（知识积累中）"
    else:
        state["mental_state"] = "经验者（知识丰富）"
    return state


def predict_behavior(agent_id: str) -> dict:
    """预测 Agent 下一步行为（基于推断）。"""
    st = infer_agent(agent_id)
    prediction = []
    if st.get("focus"):
        prediction.append(f"可能继续关注：{st['focus'][0]}")
    if st.get("knowledge", 0) < 5:
        prediction.append("可能寻求更多知识（学习型）")
    if st.get("activity", 0) > 20:
        prediction.append("高活跃——可能频繁交互")
    if st.get("reputation") is not None and st["reputation"] < 0.3:
        prediction.append("信誉偏低——可能改进行为")
    return {"agent": agent_id, "predictions": prediction[:3],
            "based_on": st.get("mental_state")}
