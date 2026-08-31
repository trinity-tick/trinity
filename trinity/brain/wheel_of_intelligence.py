# -*- coding: utf-8 -*-
"""trinity/brain/wheel_of_intelligence.py — 智能之轮（EXECUTION 353）。

借鉴 Wheel of Intelligence（2026：State-Transition Framework for
Building Artificial Minds）——智能状态转移框架：认知状态间的
转移（感知→理解→决策→行动→学习——状态轮）。

与情绪状态机（affect_state）互补：情绪=情感状态；本模块=全认知轮。
Trinity 现在：
  state_transition(current, trigger): 状态转移（认知轮转）
"""
import os
import sys
import json


# 认知状态轮（环形——状态转移图）
STATES = ["perceiving", "understanding", "deciding", "acting", "learning"]


def state_transition(current: str, trigger: str) -> dict:
    """状态转移：触发 → 下一认知状态。"""
    if current not in STATES:
        return {"error": f"未知状态（可用: {STATES}）"}
    idx = STATES.index(current)
    # 触发映射（触发词→转移类型）
    if any(w in trigger for w in ("新信息", "感知", "输入")):
        next_state = "perceiving"
    elif any(w in trigger for w in ("理解", "分析", "推理")):
        next_state = "understanding"
    elif any(w in trigger for w in ("决定", "选择", "计划")):
        next_state = "deciding"
    elif any(w in trigger for w in ("执行", "行动", "操作")):
        next_state = "acting"
    elif any(w in trigger for w in ("反馈", "结果", "学习")):
        next_state = "learning"
    else:
        # 默认轮转（环形）
        next_state = STATES[(idx + 1) % len(STATES)]
    return {"from": current, "to": next_state, "trigger": str(trigger)[:25],
            "wheel": " → ".join(STATES),
            "note": f"状态转移：{current} → {next_state}（触发：{str(trigger)[:15]}）"}


def wheel_report() -> dict:
    """状态轮状态。"""
    return {"states": STATES,
            "note": "智能之轮：认知状态转移框架（WoI 2026）"}
