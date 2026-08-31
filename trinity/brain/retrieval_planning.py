# -*- coding: utf-8 -*-
"""trinity/brain/retrieval_planning.py — 检索规划（EXECUTION 351）。

借鉴 H-Mem（2026：Retrieval Planning——Hybrid Structure）——
检索前的规划：查询分析 → 检索策略规划（先规划再检索——
目标导向）。

与混合编织（多通道）互补：编织=执行；本模块=事前规划。
Trinity 现在：
  plan_retrieval(query): 检索规划（分析→策略）
"""
import os
import sys
import json


def plan_retrieval(query: str) -> dict:
    """检索规划：查询分析 → 检索策略。"""
    q = str(query)
    plan = {}
    # 查询类型分析（explanatory 优先——"为什么"含"什么"）
    if any(w in q for w in ("为什么", "如何", "怎样")):
        plan["type"] = "explanatory"
        strategy = "多源检索（原因/方法）"
    elif any(w in q for w in ("谁", "什么", "哪里", "何时")):
        plan["type"] = "factual"
        strategy = "精确检索（事实定位）"
    elif any(w in q for w in ("比较", "对比", "选择")):
        plan["type"] = "comparative"
        strategy = "分组检索（对比维度）"
    else:
        plan["type"] = "general"
        strategy = "混合检索（通用）"
    # 检索策略（通道规划）
    plan["channels"] = ["vector"] if plan["type"] == "factual" else ["vector", "associative", "recent"]
    plan["depth"] = "deep" if plan["type"] == "explanatory" else "standard"
    plan["strategy"] = strategy
    return {"query": q[:30], "plan": plan,
            "note": f"检索规划：{plan['type']} → {strategy}（{len(plan['channels'])} 通道）"}


def planning_report() -> dict:
    """规划体系状态。"""
    return {"note": "H-Mem 检索规划：先规划再检索（目标导向）"}
