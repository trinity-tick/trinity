# -*- coding: utf-8 -*-
"""trinity/brain/compositional_generalization.py — 组合泛化（EXECUTION 323）。

借鉴 AGEL-Comp（2026：Neuro-Symbolic Framework for Compositional
Generalization）——组合泛化：已学组件的组合 → 新任务
（从已知组件泛化到新组合——神经符号框架）。

与梦境重组（随机组合）互补：梦境=探索；本模块=泛化能力。
Trinity 现在：
  compose(components, task): 组合泛化（组件匹配→新任务方案）
"""
import os
import sys
import json


# 组件库（已学能力）
COMPONENTS = {
    "backup": {"capability": "数据保护", "situations": ["升级", "迁移", "删除", "高风险"]},
    "test": {"capability": "验证", "situations": ["迁移", "升级", "新功能"]},
    "index": {"capability": "性能优化", "situations": ["查询慢", "性能", "优化"]},
    "cache": {"capability": "加速", "situations": ["性能", "频繁", "重复"]},
    "monitor": {"capability": "观测", "situations": ["故障", "异常", "监控"]},
}


def compose(components: list, task: str) -> dict:
    """组合泛化：组件匹配任务 → 组合方案（新组合泛化）。"""
    t = str(task)
    # 匹配可用组件
    available = {c: COMPONENTS[c] for c in components if c in COMPONENTS}
    # 按任务情境匹配组件
    matched = []
    for name, comp in available.items():
        if any(s in t for s in comp["situations"]):
            matched.append({"component": name, "capability": comp["capability"]})
    # 组合方案（顺序：保护→验证→优化）
    order = {"backup": 0, "test": 1, "monitor": 1, "index": 2, "cache": 2}
    matched.sort(key=lambda x: order.get(x["component"], 3))
    if not matched and available:
        matched = [{"component": n, "capability": c["capability"]} for n, c in available.items()]
    return {"task": t[:30], "composition": matched,
            "composed": len(matched) >= 2,
            "plan": " → ".join([m["component"] for m in matched]) or "无方案",
            "note": f"组合泛化：{' → '.join(m['capability'] for m in matched)}（新组合）"}
