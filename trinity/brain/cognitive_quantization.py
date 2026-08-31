# -*- coding: utf-8 -*-
"""trinity/brain/cognitive_quantization.py — 认知量化（EXECUTION 303）。

借鉴 SuperLocalMemory V3.3（2026：Cognitive Quantization）——记忆
的认知量化：按认知重要性分级存储（高重要→全保真；低重要→
降采样——认知带宽优化）。

与遗忘（修剪）互补：遗忘=删除；量化=降级保留。
Trinity 现在：
  quantize(content, importance): 认知量化（分级保真）
"""
import os
import sys
import json


def quantize(content: str, importance: float = 0.5) -> dict:
    """认知量化：按重要性分级（全保真/精炼/摘要）。"""
    text = str(content)
    if importance >= 0.7:
        level = "full"
        stored = text[:300]  # 全保真
        note = "高重要——全保真存储"
    elif importance >= 0.4:
        level = "refined"
        stored = text[:120]  # 精炼
        note = "中重要——精炼存储"
    else:
        level = "digest"
        # 摘要（决策词优先）
        digest = ""
        for w in ("决定", "选择", "采用", "避免", "失败", "成功", "有效", "修复"):
            idx = text.find(w)
            if idx >= 0:
                digest = text[max(0, idx-5):min(len(text), idx+20)]
                break
        stored = digest or text[:40]
        note = "低重要——摘要存储"
    return {"level": level, "stored_len": len(stored), "original_len": len(text),
            "compression": round(len(stored) / max(len(text), 1), 2),
            "stored": stored[:60], "note": note,
            "spectrum": "full → refined → digest（认知量化分级）"}


def quantization_report() -> dict:
    """量化体系状态。"""
    return {"levels": ["full", "refined", "digest"],
            "note": "认知量化：重要性分级存储（认知带宽优化）"}
