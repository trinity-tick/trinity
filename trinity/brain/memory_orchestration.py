# -*- coding: utf-8 -*-
"""trinity/brain/memory_orchestration.py — 记忆编排（EXECUTION 356）。

借鉴 SaliMory（2026：Orchestrating Cognitive Memory for
Conversational Agents）——记忆编排：对话场景下按显著性编排
记忆（哪些记忆进对话——编排而非全给）。

与认知管线（处理编排）互补：管线=处理流；本模块=记忆编排。
Trinity 现在：
  orchestrate(query, salience): 记忆编排（显著性→编排）
"""
import os
import sys
import json


def orchestrate(query: str, salience: dict) -> dict:
    """记忆编排：按显著性选择对话记忆。"""
    # 显著性排序（高显著优先）
    ranked = sorted(salience.items(), key=lambda x: -x[1])
    # 编排（取 top 显著 + 相关）
    orchestrated = [{"memory": str(k)[:30], "salience": round(v, 2)}
                    for k, v in ranked[:4] if v >= 0.3]
    # 对话可用性（编排记忆数量）
    if len(orchestrated) >= 3:
        level = "rich_context"
    elif len(orchestrated) >= 1:
        level = "focused"
    else:
        level = "minimal"
    return {"query": str(query)[:25], "orchestrated": orchestrated,
            "context_level": level,
            "note": f"记忆编排：{len(orchestrated)} 条显著记忆 → {level}（SaliMory）"}


def orchestration_report() -> dict:
    """编排状态。"""
    return {"note": "SaliMory：对话记忆编排（显著性驱动——不全部给）"}
