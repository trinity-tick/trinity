# -*- coding: utf-8 -*-
"""trinity/brain/rate_distortion.py — 率失真记忆（EXECUTION 286，大脑化）。

借鉴 Rate-Distortion Framework（2026：Remember the Decision, Not
the Description）——记忆压缩的最优取舍：保留决策核心，丢弃描述
细节（率失真——保真度与简洁的平衡）。

与重构（回忆综合）互补：重构=综合；本模块=压缩取舍。
Trinity 现在：
  compress(memory): 率失真压缩（决策/行动核心 vs 描述细节）
"""
import os
import sys
import json


# 决策词（触发核心保留）
DECISION_WORDS = ("决定", "选择", "采用", "避免", "执行", "改为", "更新",
                  "备份", "删除", "放弃", "继续", "停止")


def compress(memory: str, fidelity: float = 0.7) -> dict:
    """率失真压缩：按保真度取舍（保留决策核心）。"""
    content = str(memory)
    # 提取决策/行动核心（决策词附近内容优先保留）
    decision_parts = []
    for w in DECISION_WORDS:
        idx = content.find(w)
        if idx >= 0:
            start = max(0, idx - 8)
            end = min(len(content), idx + 22)
            decision_parts.append(content[start:end].strip())
    if decision_parts:
        kept = "；".join(decision_parts[:3])
        note = "已保留决策核心（丢弃描述细节）"
    else:
        keep_len = max(20, int(len(content) * fidelity))
        kept = content[:keep_len]
        note = "无决策词——按保真度截取"
    return {"original_len": len(content), "kept": kept[:80],
            "kept_len": len(kept),
            "compression_ratio": round(len(kept) / max(len(content), 1), 2),
            "note": note}


def distill_decision(memory: str) -> dict:
    """决策蒸馏：从记忆提取"决定什么+结果"。"""
    c = compress(memory, 0.8)
    return {"decision": c["kept"][:60], "compressed": c["compression_ratio"],
            "note": "记住决策，不记描述（Rate-Distortion）"}
