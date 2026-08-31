# -*- coding: utf-8 -*-
"""trinity/brain/trait_activation.py — 情境特质激活（EXECUTION 272，大脑化）。

借鉴 Trait Activation in Silicon（ACL 2026：Situation-Aware）——
不同情境激活不同性格特质（谨慎在风险中激活、探索在新领域激活）。

与性格结晶（稳定特质）互补：结晶=形成；激活=情境触发。
Trinity 现在：
  activate_traits(context): 情境特质激活（风险→谨慎/新颖→探索）
"""
import os
import sys
import json


# 特质-情境映射
TRAIT_CONTEXTS = {
    "谨慎": ["风险", "高危险", "关键", "严重", "事故", "故障", "删除"],
    "探索": ["新领域", "未知", "好奇", "前沿", "创新", "首次"],
    "协作": ["协作", "团队", "共享", "合作", "评审"],
    "效率": ["优化", "性能", "加速", "简化", "提升"],
}


def activate_traits(context: str) -> dict:
    """情境特质激活：匹配情境词 → 激活特质。"""
    activated = {}
    for trait, keywords in TRAIT_CONTEXTS.items():
        match = [k for k in keywords if k in str(context)]
        if match:
            activated[trait] = {"match": match[0], "strength": 0.8}
    if not activated:
        return {"traits": [], "note": "无匹配情境（默认模式）"}
    return {"traits": list(activated.keys()), "details": activated,
            "note": f"情境激活特质：{', '.join(activated.keys())}"}


def behavior_profile(context: str) -> dict:
    """行为画像：情境激活 → 行为倾向。"""
    act = activate_traits(context)
    traits = act.get("traits", [])
    if "谨慎" in traits:
        return {"profile": "谨慎模式", "behaviors": ["保守决策", "先验证", "风险规避"],
                "traits": traits}
    if "探索" in traits:
        return {"profile": "探索模式", "behaviors": ["主动尝试", "搜索新知识", "发散思考"],
                "traits": traits}
    return {"profile": "默认模式", "behaviors": ["标准处理"], "traits": traits}
