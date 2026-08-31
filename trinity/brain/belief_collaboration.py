# -*- coding: utf-8 -*-
"""trinity/brain/belief_collaboration.py — 信念协作（EXECUTION 329）。

借鉴 Belief-Driven Multi-Agent Collaboration（WWW 2026：
Approximate Perfect Bayesian Equilibrium）——信念驱动的协作：
按对伙伴的信念（可信/能力）更新协作策略（贝叶斯均衡——
最优协作投入）。

与协调（任务分派）互补：分派=角色分配；本模块=信念驱动。
Trinity 现在：
  collaborate(agent, belief): 信念协作（信念→投入/策略）
"""
import os
import sys
import json


def collaborate(agent: str, belief: dict) -> dict:
    """信念协作：对伙伴信念 → 协作策略。"""
    trust = float(belief.get("trust", 0.5))
    capability = float(belief.get("capability", 0.5))
    # 协作投入（信念加权——贝叶斯均衡近似）
    investment = trust * 0.5 + capability * 0.5
    if investment >= 0.7:
        strategy = "deep_collab"
        note = f"高信念——深度协作（信任 {round(trust,2)}×能力 {round(capability,2)}）"
    elif investment >= 0.4:
        strategy = "moderate"
        note = f"中等信念——适度协作"
    else:
        strategy = "guarded"
        note = f"低信念——谨慎协作（先验证）"
    return {"agent": str(agent)[:20], "trust": round(trust, 2),
            "capability": round(capability, 2),
            "investment": round(investment, 2), "strategy": strategy,
            "note": note}


def belief_update(agent: str, interaction_result: float,
                  current_belief: dict) -> dict:
    """信念更新：交互结果 → 信念调整（贝叶斯更新）。"""
    trust = float(current_belief.get("trust", 0.5))
    # 贝叶斯式更新（结果加权）
    new_trust = trust * 0.7 + interaction_result * 0.3
    return {"agent": str(agent)[:20], "trust_old": round(trust, 2),
            "trust_new": round(new_trust, 2),
            "delta": round(new_trust - trust, 2),
            "note": f"信念更新：信任 {round(trust,2)} → {round(new_trust,2)}（结果 {interaction_result}）"}
