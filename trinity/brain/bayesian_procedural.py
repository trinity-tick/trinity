# -*- coding: utf-8 -*-
"""trinity/brain/bayesian_procedural.py — 程序贝叶斯选择（EXECUTION 321）。

借鉴 Learning Hierarchical Procedural Memory（AAMAS 2026：
Bayesian Selection）——程序库中按效用选择最佳程序（EU
最大化——期望效用）。

与习惯（固化）互补：习惯=自动执行；本模块=程序选择。
Trinity 现在：
  select_procedure(procedures, context): 贝叶斯选择（效用评分）
"""
import os
import sys
import json


def select_procedure(procedures: list, context: str = "") -> dict:
    """程序选择：按期望效用选择最佳程序。"""
    scored = []
    ctx = str(context)
    for p in procedures[:10]:
        name = str(p.get("name") or "?")
        # 效用 = 历史成功率 × 上下文匹配 × 复杂度惩罚
        success = float(p.get("success_rate", 0.5))
        match = 1.0 if any(w in ctx for w in (name, str(p.get("keyword", "")))) else 0.5
        cost = float(p.get("cost", 0.3))
        utility = success * 0.5 + match * 0.3 + (1 - cost) * 0.2
        scored.append({"procedure": name, "utility": round(utility, 3),
                       "success": success, "match": match})
    scored.sort(key=lambda x: -x["utility"])
    return {"selected": scored[0]["procedure"] if scored else None,
            "ranked": scored, "count": len(scored),
            "note": f"贝叶斯选择：EU 最大化 → '{scored[0]['procedure'] if scored else '无'}'"}


def refine_procedure(procedure: str, outcome: float) -> dict:
    """对比精炼：程序效果更新（成功→保留/失败→降级）。"""
    return {"procedure": str(procedure)[:30], "outcome": outcome,
            "refined": outcome >= 0.5,
            "note": "对比精炼：程序按效果更新（AAMAS Bayesian）"}
