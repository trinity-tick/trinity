# -*- coding: utf-8 -*-
"""trinity/brain/silent_scholar.py — 沉默学者（EXECUTION 363）。

借鉴 Silent Scholar Problem（2026：Probabilistic Framework for
Breaking Epistemic Asymmetry）——打破认知不对称：有知识但
不表达 = 沉默学者（检测知识-表达差距→主动表达）。

与未知意识（识别不知道）互补：未知=知道自己不知道；本模块=知道但不表达。
Trinity 现在：
  detect_silence(knowledge, expression): 沉默检测（差距分析）
"""
import os
import sys
import json


def detect_silence(knowledge: int = 0, expression: int = 0,
                   domain: str = "") -> dict:
    """沉默检测：知识量 vs 表达量差距。"""
    # 知识-表达比（不对称度）
    gap = knowledge - expression
    ratio = expression / max(knowledge, 1)
    if knowledge >= 10 and ratio < 0.3:
        state = "silent_scholar"  # 有知识不表达
        action = "主动表达知识"
    elif knowledge >= 10 and ratio < 0.7:
        state = "partially_silent"
        action = "增加表达频率"
    elif knowledge < 10:
        state = "learning_phase"
        action = "先积累知识"
    else:
        state = "expressive"
        action = "保持表达"
    return {"domain": str(domain)[:20], "knowledge": knowledge,
            "expression": expression, "gap": gap,
            "ratio": round(ratio, 2), "state": state, "action": action,
            "note": f"沉默检测：知识 {knowledge} vs 表达 {expression} → {state}（{action}）"}


def silence_report() -> dict:
    """沉默体系状态。"""
    return {"note": "打破认知不对称：沉默学者检测（主动表达）"}
