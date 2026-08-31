# -*- coding: utf-8 -*-
"""trinity/brain/learn_to_remember.py — 学会记住（EXECUTION 346）。

借鉴 Learning How to Remember（ACL 2026：Meta-Cognitive Management
for Structured and Transferable Agent Memory）——学会"如何记住"：
内容特征 → 记忆方法决策（结构化/可迁移——方法学习）。

与元认知记忆策略（监控）互补：策略=监控调整；本模块=方法学习。
Trinity 现在：
  remember_policy(content): 记忆方法决策（内容→方法）
"""
import os
import sys
import json


def remember_policy(content: str) -> dict:
    """记忆方法决策：内容特征 → 最适方法。"""
    t = str(content)
    # 特征分析
    features = {}
    features["length"] = len(t)
    features["has_numbers"] = any(ch.isdigit() for ch in t)
    features["has_decision"] = any(w in t for w in ("决定", "选择", "采用", "避免"))
    features["has_emotion"] = any(w in t for w in ("重要", "失败", "成功", "紧急"))
    # 方法决策（特征→方法）
    if features["has_decision"]:
        method = "decision_memory"
        note = "含决策——决策记忆（率失真保留核心）"
    elif features["has_emotion"]:
        method = "emotional_memory"
        note = "含情感——情绪记忆（显著性编码）"
    elif features["length"] > 100:
        method = "structured_memory"
        note = "长内容——结构化记忆（要点+分层）"
    elif features["has_numbers"]:
        method = "factual_memory"
        note = "含数据——事实记忆（精确存储）"
    else:
        method = "general_memory"
        note = "常规——通用记忆"
    return {"content": t[:40], "features": features, "method": method,
            "note": note,
            "transferable": True,
            "note2": "记忆方法可迁移（ACL 2026——学会如何记住）"}
