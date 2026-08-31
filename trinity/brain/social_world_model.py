# -*- coding: utf-8 -*-
"""trinity/brain/social_world_model.py — 社会世界模型（EXECUTION 375）。

借鉴 Social World Models（2026）——社会互动世界模型：预测他人
反应（行动 → 他人反应模拟——社会模拟）。

与世界模型（环境）互补：环境=物理世界；本模块=社会世界。
Trinity 现在：
  social_predict(action, agent): 社会预测（行动→他人反应）
"""
import os
import sys
import json


# 行动-反应模式（社会世界模型）
REACTION_MODEL = {
    "分享": {"positive": 0.8, "reaction": "感谢/回报", "trust_effect": "+0.2"},
    "请求": {"positive": 0.6, "reaction": "响应/评估", "trust_effect": "+0.1"},
    "竞争": {"positive": 0.3, "reaction": "对抗/戒备", "trust_effect": "-0.2"},
    "批评": {"positive": 0.4, "reaction": "防御/反思", "trust_effect": "-0.1"},
}


def social_predict(action: str, agent: str, trust: float = 0.5) -> dict:
    """社会预测：行动 → 他人反应模拟。"""
    # 行动匹配反应模型
    reaction = None
    for act, model in REACTION_MODEL.items():
        if act in str(action):
            reaction = model
            action_type = act
            break
    if not reaction:
        reaction = {"positive": 0.5, "reaction": "中性反应", "trust_effect": "0"}
        action_type = "neutral"
    # 信任调节预测
    predicted = min(1.0, reaction["positive"] * 0.6 + trust * 0.4)
    return {"action": str(action)[:25], "agent": str(agent)[:15],
            "action_type": action_type,
            "predicted_reaction": reaction["reaction"],
            "predicted_positive": round(predicted, 2),
            "trust_effect": reaction["trust_effect"],
            "note": f"社会预测：『{action_type}』→ {reaction['reaction']}（信任效应 {reaction['trust_effect']}）"}


def social_report() -> dict:
    """社会世界模型状态。"""
    return {"note": "社会世界模型：行动→他人反应预测（Social World Models）"}
