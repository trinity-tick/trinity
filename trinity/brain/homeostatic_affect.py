# -*- coding: utf-8 -*-
"""trinity/brain/homeostatic_affect.py — 稳态情感控制（EXECUTION 326）。

借鉴 Gubernaut（2026：Deterministic Homeostatic Controller for
Affect-Regulated Agents——跨模型族验证 15/16 更冷静）——确定性
稳态控制：情绪偏离基线 → 确定性拉回（防情绪漂移）。

与情绪调节（策略）互补：策略=方法；本模块=稳态控制。
Trinity 现在：
  regulate(value, baseline): 稳态调节（确定性拉回）
"""
import os
import sys
import json


def regulate(value: float, baseline: float = 0.5,
             homeostatic_rate: float = 0.4) -> dict:
    """稳态调节：偏离基线 → 确定性拉回（Gubernaut）。"""
    deviation = value - baseline
    # 稳态控制（确定性回归）
    if abs(deviation) < 0.1:
        return {"value": round(value, 2), "baseline": baseline,
                "regulated": False, "deviation": round(deviation, 2),
                "note": "在稳态范围内——无需调节"}
    pull = deviation * homeostatic_rate  # 确定性拉回
    new_value = value - pull
    return {"value": round(new_value, 2), "baseline": baseline,
            "regulated": True, "deviation": round(deviation, 2),
            "pull": round(pull, 2),
            "note": f"稳态调节：偏离 {round(deviation,2)} → 拉回 {round(pull,2)}（确定性）"}


def affect_sequence(values: list, baseline: float = 0.5) -> dict:
    """情感序列：连续稳态调节（防漂移——Gubernaut 验证）。"""
    regulated = []
    current = baseline
    for v in values[:10]:
        r = regulate(v, current, 0.4)
        current = r["value"]
        regulated.append({"input": v, "output": r["value"], "regulated": r["regulated"]})
    drift = max(v["output"] for v in regulated) - min(v["output"] for v in regulated)
    return {"sequence": regulated, "final_drift": round(drift, 3),
            "calm": drift < 0.3,
            "note": f"稳态情感序列：漂移控制到 {round(drift,3)}（{'冷静' if drift < 0.3 else '需加强'}）"}
