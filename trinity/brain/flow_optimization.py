# -*- coding: utf-8 -*-
"""trinity/brain/flow_optimization.py — 流程优化（EXECUTION 374）。

借鉴 AgentFlow（2026：In-the-Flow Agentic System Optimization）——
流程中优化：管线步骤随使用效果调整（工具规划联合优化——
流程学习）。

与管线（阶段流）互补：管线=执行；本模块=流程学习。
Trinity 现在：
  optimize_flow(steps, outcome): 流程优化（步骤权重调整）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/flow_optimization.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"step_weights": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def optimize_flow(steps: list, outcome: float = 0.5) -> dict:
    """流程优化：步骤效果 → 权重调整（流程学习）。"""
    st = _load()
    lr = 0.2
    adjusted = []
    for step in steps[:8]:
        name = str(step)[:20]
        w = st["step_weights"].get(name, 0.5)
        # 流程中优化（效果→步骤权重）
        new_w = min(1.0, max(0.0, w + (outcome - 0.5) * lr))
        st["step_weights"][name] = new_w
        adjusted.append({"step": name, "weight": round(new_w, 2)})
    _save(st)
    return {"outcome": round(outcome, 2), "adjusted": adjusted,
            "note": f"流程优化：{len(adjusted)} 步权重调整（{'正向强化' if outcome >= 0.6 else '负向弱化'}）"}


def flow_report() -> dict:
    """流程状态。"""
    st = _load()
    return {"steps": len(st.get("step_weights", {})),
            "note": "AgentFlow：流程中优化（工具规划联合学习）"}
