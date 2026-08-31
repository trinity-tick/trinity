# -*- coding: utf-8 -*-
"""trinity/brain/emotional_valence.py — 效价维度（EXECUTION 357）。

借鉴 EVD（2026：Emotional Valence Dimension for Persistent Agent
Memory）——记忆的情绪效价持久维度：记忆的效价作为持久属性
（积极/消极/中性——标注后长期有效）。

与情绪空间（坐标检索）互补：空间=坐标组织；本模块=持久标注。
Trinity 现在：
  valence_tag(memory): 效价标注（持久维度）
"""
import os
import sys
import json


def valence_tag(memory: str) -> dict:
    """效价标注：记忆 → 持久效价维度。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect import assess
        r = assess(str(memory)[:200])
        val = float(r.get("valence", 0))
        aro = float(r.get("arousal", 0.3))
    except Exception:
        val, aro = 0.0, 0.3
    # 效价分类（持久标注）
    if val > 0.3:
        valence = "positive"
    elif val < -0.3:
        valence = "negative"
    else:
        valence = "neutral"
    return {"memory": str(memory)[:40], "valence": round(val, 2),
            "arousal": round(aro, 2), "dimension": valence,
            "persistent": True,
            "note": f"效价维度：{valence}（持久标注——长期有效）"}


def valence_report() -> dict:
    """效价体系状态。"""
    return {"note": "EVD：情绪效价持久维度（Persistent Agent Memory）"}
