# -*- coding: utf-8 -*-
"""trinity/brain/fast_slow_decision.py — 快慢决策（EXECUTION 248，大脑化）。

借鉴 DSADF（Thinking Fast and Slow for Decision Making）——决策的
双系统：System1（快/直觉/低风险）vs System2（慢/深思/高风险）。

与 DCPM（快写慢归纳）互补：DCPM=记忆层；本模块=决策层。
Trinity 现在：
  decide(query, risk, familiarity): 快慢选择（低风险熟悉→快；
    高风险/陌生→慢路径——内心独白+排练+推理）
"""
import os
import sys
import json


def decide(query: str, risk: float = 0.3, familiarity: float = 0.5) -> dict:
    """快慢决策：按风险与熟悉度选择路径。"""
    # System1 条件：低风险 + 熟悉
    fast_condition = risk < 0.4 and familiarity >= 0.6
    if fast_condition:
        return {"mode": "system1_fast", "query": str(query)[:40],
                "reason": "低风险+熟悉——直接决策",
                "steps": ["直觉响应"]}
    # System2 条件：高风险或陌生
    if risk >= 0.7 or familiarity < 0.3:
        steps = ["内心独白（多声音讨论）", "世界排练（模拟结果）",
                 "情景推理（证据→结论）"]
        return {"mode": "system2_deep", "query": str(query)[:40],
                "reason": "高风险或陌生——深度思考",
                "steps": steps}
    # 中间：System1 + 快速校验
    return {"mode": "system1_checked", "query": str(query)[:40],
            "reason": "中等风险——快决策+校验",
            "steps": ["直觉响应", "快速校验"]}


def decision_report(query: str) -> dict:
    """决策模式报告（快慢分布）。"""
    modes = {"system1_fast": 0, "system2_deep": 0, "system1_checked": 0}
    # 模拟评估多种场景
    for risk, fam in ((0.2, 0.8), (0.9, 0.2), (0.5, 0.5), (0.3, 0.7), (0.8, 0.6)):
        r = decide(query, risk, fam)
        modes[r["mode"]] += 1
    return {"distribution": modes,
            "note": "低风险快/高风险慢——决策自适应"}
