# -*- coding: utf-8 -*-
"""trinity/brain/dual_cognitive_loop.py — 双认知回路（EXECUTION 328）。

借鉴 Dual Cognitive Loop（2026：Rationality Meets Irrationality——
Incentivizing for Social Simulation）——理性与不理性双回路
（理性计算 + 不理性冲动——两者都受激励——真实行为）。

与快慢决策（System1/2 路径）互补：快慢=路径选择；本模块=回路激励。
Trinity 现在：
  dual_loop(decision): 双回路评估（理性分 vs 冲动分——平衡决策）
"""
import os
import sys
import json


def dual_loop(decision: str, rationality: float = 0.6,
              impulse: float = 0.4, context_risk: float = 0.3) -> dict:
    """双回路评估：理性 vs 冲动 → 平衡决策。"""
    # 理性回路（计算：价值×可行×风险规避）
    rational_score = rationality * (1 - context_risk) * 0.8 + 0.2
    # 不理性回路（冲动：即时满足×情绪）
    impulse_score = impulse * context_risk + impulse * 0.3
    # 激励平衡（社会模拟：两回路都受激励）
    total = rational_score + impulse_score
    rational_weight = rational_score / max(total, 0.01)
    impulse_weight = impulse_score / max(total, 0.01)
    # 决策：理性主导/冲动主导/平衡
    if rational_weight >= 0.7:
        mode = "rational_dominant"
    elif impulse_weight >= 0.5:
        mode = "impulse_dominant"
    else:
        mode = "balanced"
    return {"decision": str(decision)[:30],
            "rational": round(rational_score, 2),
            "impulse": round(impulse_score, 2),
            "weights": {"rational": round(rational_weight, 2),
                        "impulse": round(impulse_weight, 2)},
            "mode": mode,
            "note": f"双回路：理性 {round(rational_weight,2)} / 冲动 {round(impulse_weight,2)} → {mode}"}


def loop_balance() -> dict:
    """回路平衡状态（社会模拟真实性）。"""
    return {"note": "双认知回路激励：理性与不理性平衡（Dual Cognitive Loop）"}
