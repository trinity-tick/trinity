# -*- coding: utf-8 -*-
"""trinity/brain/generation_timing.py — 生成时机（EXECUTION 276，大脑化）。

借鉴 Mem-π（2026：Learning When and What to Generate）——生成
时机决策：何时值得生成新记忆（价值/新颖/机会评估）。

与生成式记忆（合成方法）互补：合成=如何生成；时机=何时生成。
Trinity 现在：
  should_generate(context_value, novelty, opportunity): 生成决策
"""
import os
import sys
import json


def should_generate(context_value: float = 0.5, novelty: float = 0.5,
                    opportunity: float = 0.5) -> dict:
    """生成决策：价值×新颖×机会 → 值得/等待/跳过。"""
    score = context_value * 0.4 + novelty * 0.35 + opportunity * 0.25
    if score >= 0.65:
        return {"decision": "generate", "score": round(score, 2),
                "note": "高价值+新颖+好时机——值得生成"}
    if score >= 0.4:
        return {"decision": "wait", "score": round(score, 2),
                "note": "中等——等待更好时机"}
    return {"decision": "skip", "score": round(score, 2),
            "note": "低价值——跳过（避免记忆噪音）"}


def generation_policy(context: str, accumulated_value: float = 0.5) -> dict:
    """生成策略：上下文 + 累积价值 → 决策。"""
    # 新颖性（与现有记忆不同？）
    novelty = 0.5
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.surprise_encoding import surprise_boost
        sb = surprise_boost(context)
        novelty = sb.get("novelty", 0.5)
    except Exception:
        pass
    return should_generate(accumulated_value, novelty,
                           opportunity=0.6 if accumulated_value >= 0.7 else 0.4)


def timing_report() -> dict:
    """时机策略状态。"""
    return {"note": "生成时机：价值×新颖×机会（Mem-π）"}
