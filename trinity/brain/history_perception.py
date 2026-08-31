# -*- coding: utf-8 -*-
"""trinity/brain/history_perception.py — 历史感知（EXECUTION 378）。

借鉴 History-Dependent Perceptual Reorganization（2026：Same World,
Differently Given）——历史依赖的感知重组：感知受个人历史影响
（同一世界——不同历史看到不同）。

与统觉（融合）互补：统觉=多通道融合；本模块=历史影响。
Trinity 现在：
  perceive_with_history(input, history): 历史感知（感知重组）
"""
import os
import sys
import json


def perceive_with_history(input_signal: str, history: list) -> dict:
    """历史感知：感知受历史经验影响（重组）。"""
    signal = str(input_signal)
    # 历史主题提取（影响感知的重点）
    history_focus = []
    for h in history[:5]:
        t = str(h.get("topic") or "")
        if t:
            history_focus.append(t[:15])
    # 感知重组（历史重点 → 感知突显）
    emphasis = []
    for h in history_focus:
        if h and h[:8] in signal:
            emphasis.append(h)
    if emphasis:
        reorganized = f"感知强化：{signal[:30]}（历史相关——{emphasis[0]}）"
        perception = "history_biased"
    else:
        reorganized = f"中性感知：{signal[:40]}"
        perception = "neutral"
    return {"input": signal[:30], "history_focus": history_focus[:3],
            "emphasis": emphasis, "perception": perception,
            "reorganized": reorganized[:60],
            "note": f"历史感知：{'历史突显' if emphasis else '中性'}（同一世界不同呈现——History-Dependent）"}


def perception_report() -> dict:
    """感知体系状态。"""
    return {"note": "历史依赖感知重组（Same World, Differently Given）"}
