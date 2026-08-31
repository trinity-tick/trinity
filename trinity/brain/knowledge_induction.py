# -*- coding: utf-8 -*-
"""trinity/brain/knowledge_induction.py — 自主知识归纳（EXECUTION 306）。

借鉴 Robo-Cortex（2026：Autonomous Knowledge Induction）——从经验
自主归纳知识（不需要显式触发——自动提取规律→新知识）。

与 DCPM（归纳信念）互补：DCPM=信念系统；本模块=知识提取。
Trinity 现在：
  induct(experiences): 自主归纳（模式提取→知识规则）
"""
import os
import sys
import json


def induct(experiences: list, min_support: int = 2) -> dict:
    """自主归纳：从经验提取规律（模式支持度）。"""
    # 提取经验中的"动作-结果"模式
    patterns = {}
    for e in experiences[:50]:
        content = str(e.get("content") or "")
        result = "success" if any(w in content for w in ("成功", "有效", "提升", "修复")) else (
            "failure" if any(w in content for w in ("失败", "错误", "崩溃", "丢失")) else "neutral")
        # 提取动作词（决策词附近）
        action = None
        for w in ("备份", "升级", "优化", "索引", "缓存", "删除", "重构", "测试"):
            if w in content:
                action = w
                break
        if action:
            key = f"{action}->{result}"
            patterns[key] = patterns.get(key, 0) + 1
    # 归纳规则（支持度 >= min_support）
    rules = []
    for pattern, support in patterns.items():
        if support >= min_support:
            action, result = pattern.split("->")
            rules.append({"rule": f"执行『{action}』往往导致『{result}』",
                          "support": support, "confidence": round(support / max(len(experiences), 1), 2)})
    rules.sort(key=lambda x: -x["support"])
    return {"rules": rules[:5], "rule_count": len(rules),
            "source_experiences": len(experiences),
            "note": f"自主归纳：从 {len(experiences)} 条经验提取 {len(rules)} 条规律"}


def induction_report() -> dict:
    """归纳状态。"""
    r = induct([])
    return {"note": "自主知识归纳（Robo-Cortex——从经验自动提取规律）"}
