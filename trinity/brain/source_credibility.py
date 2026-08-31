# -*- coding: utf-8 -*-
"""trinity/brain/source_credibility.py — 来源可信度（EXECUTION 256，大脑化）。

借鉴 FACTWASH / Manufactured Confidence（2026）——记忆巩固可能把
"传闻"洗成"自信事实"。防御：记忆置信受来源可信度约束
（经验 > 感知 > 网络 > 传闻）。

Trinity 现在：
  credibility(source): 来源可信度评分
  adjust_confidence(memory): 按来源调整置信（防过度自信）
"""
import os
import sys
import json


# 来源可信度（经验/直接感知最高；传闻/未知最低）
SOURCE_CREDIBILITY = {
    "experience": 0.95, "action": 0.9, "perception": 0.85,
    "log": 0.8, "filesystem": 0.75, "web": 0.6, "websearch": 0.55,
    "social": 0.5, "rumor": 0.3, "unknown": 0.4,
}


def credibility(source: str) -> float:
    """来源可信度。"""
    return SOURCE_CREDIBILITY.get(str(source).lower(), SOURCE_CREDIBILITY["unknown"])


def _detect_source(content: str) -> str:
    """从内容检测来源标记。"""
    t = str(content or "")
    if "[web" in t:
        return "web"
    if "[log" in t:
        return "log"
    if "[filesystem" in t:
        return "filesystem"
    if "[social" in t or "[observational" in t:
        return "social"
    if "[action-experience" in t:
        return "experience"
    return "unknown"


def adjust_confidence(content: str, base_confidence: float = 0.7) -> dict:
    """按来源调整置信：高可信源→保持；低可信源→下调。"""
    source = _detect_source(content)
    cred = credibility(source)
    # 置信 = 基础置信 × 来源可信度（来源不可信 → 置信大幅下调）
    adjusted = base_confidence * cred
    return {"source": source, "credibility": round(cred, 2),
            "base_confidence": base_confidence,
            "adjusted_confidence": round(adjusted, 2),
            "note": f"来源[{source}]可信度{cred} → 置信{'保持' if cred >= 0.8 else '下调'}"}


def credibility_report() -> dict:
    """可信度体系报告。"""
    return {"sources": SOURCE_CREDIBILITY,
            "note": "经验>感知>网络>传闻（防传闻洗成事实）"}
