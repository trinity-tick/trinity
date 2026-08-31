# -*- coding: utf-8 -*-
"""trinity/brain/fractal_cognition.py — 分形认知（EXECUTION 362）。

借鉴 DCA（2026：Distributed Cognitive Architecture——Atomic Agents,
Fractal Composition, Convergence）——分形认知组合：原子组件
分形组合成更大认知单元（自相似结构——复合可再复合）。

与主权分层（层自治）互补：主权=层；本模块=分形组合。
Trinity 现在：
  fractal_compose(atoms): 分形组合（原子→复合单元）
"""
import os
import sys
import json


# 原子认知单元库
ATOMS = {
    "retrieve": "记忆检索",
    "reason": "逻辑推理",
    "decide": "决策选择",
    "act": "行动执行",
    "learn": "经验学习",
    "reflect": "反思评估",
}


def fractal_compose(atoms: list, depth: int = 1) -> dict:
    """分形组合：原子 → 复合单元（自相似——可再复合）。"""
    # 组合（原子→单元）
    unit = []
    for a in atoms[:6]:
        if a in ATOMS:
            unit.append({"atom": a, "function": ATOMS[a]})
    # 分形（复合单元可再组合——自相似）
    fractal_depth = min(depth, 3)
    structure = unit
    for d in range(2, fractal_depth + 1):
        # 每层：复合单元作为新原子（自相似）
        structure = [{"atom": f"unit{d-1}_{idx}", "function": f"复合单元（{len(unit)} 原子）"}
                     for idx in range(min(len(unit), 2))]
    return {"atoms": [u["atom"] for u in unit], "depth": fractal_depth,
            "unit_size": len(unit),
            "converged": len(unit) >= 2,
            "note": f"分形组合：{len(unit)} 原子 → 深度 {fractal_depth} 复合（自相似——可再复合）"}


def fractal_report() -> dict:
    """分形体系状态。"""
    return {"atoms": list(ATOMS.keys()),
            "note": "DCA：原子分形组合（自相似认知结构——收敛）"}
