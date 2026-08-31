# -*- coding: utf-8 -*-
"""trinity/brain/dopamine_gated_memory.py — 奖赏门控记忆（EXECUTION 273）。

借鉴 D-MEM（2026：Dopamine-Gated Agentic Memory via Reward Prediction
Error Routing）——奖赏预测误差（RPE）决定记忆强化路径（RPE 高→
强化记忆；RPE 低→跳过——多巴胺门控）。

与多巴胺（水平状态）互补：水平=状态；门控=记忆路由。
Trinity 现在：
  gate_by_reward(memory, rpe): RPE → 强化/跳过/弱化（记忆路由）
"""
import os
import sys
import json


def gate_by_reward(memory: str, rpe: float = 0.0) -> dict:
    """奖赏门控：RPE 决定记忆处理。"""
    # RPE > 0.5：意外奖赏 → 强化（记得牢）
    # RPE -0.5~0.5：正常 → 标准处理
    # RPE < -0.5：预期落空 → 弱化（教训）
    if rpe > 0.5:
        return {"gate": "strengthen", "rpe": round(rpe, 2),
                "action": "强化记忆（意外奖赏——多巴胺门控）",
                "importance_boost": 0.2}
    if rpe < -0.5:
        return {"gate": "weaken", "rpe": round(rpe, 2),
                "action": "弱化（预期落空——教训信号）",
                "importance_boost": -0.1}
    return {"gate": "normal", "rpe": round(rpe, 2),
            "action": "标准处理", "importance_boost": 0.0}


def reward_routing(experiences: list) -> dict:
    """奖赏路由：批量经验按 RPE 分派。"""
    routes = {"strengthen": [], "normal": [], "weaken": []}
    for e in experiences[:10]:
        rpe = float(e.get("rpe", 0))
        g = gate_by_reward(str(e.get("content", ""))[:40], rpe)
        routes[g["gate"]].append({"content": str(e.get("content", ""))[:30],
                                  "rpe": round(rpe, 2)})
    return {"routes": {k: len(v) for k, v in routes.items()},
            "note": "奖赏预测误差路由记忆（D-MEM）"}


def gating_report() -> dict:
    """门控状态。"""
    return {"note": "多巴胺门控记忆：RPE 决定强化/弱化路径"}
