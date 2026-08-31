# -*- coding: utf-8 -*-
"""trinity/brain/feeling_first.py — 先感受后表达（EXECUTION 288，大脑化）。

借鉴 Feeling First, Speaking Second（2026：Dual-Process Cognitive-
Affective）——表达前先评估情感（感受→调整→表达），防止情绪化
输出。

与内心独白（讨论）互补：独白=多声思考；本模块=情感检查。
Trinity 现在：
  feel_first(response, affect): 先感受后表达（情绪调整）
"""
import os
import sys
import json


def feel_first(response: str, valence: float = 0.0,
               arousal: float = 0.3) -> dict:
    """先感受后表达：生成回应前评估情绪 → 调整表达。"""
    checks = []
    adjusted = response
    # 1) 强消极（valence < -0.5）→ 缓和表达
    if valence < -0.5:
        adjusted = f"（先缓和情绪）{str(response)[:80]}"
        checks.append({"issue": "strong_negative", "action": "soften"})
    # 2) 高唤醒（arousal > 0.8）→ 降速表达
    if arousal > 0.8:
        adjusted = f"（平静后表达）{str(response)[:80]}"
        checks.append({"issue": "high_arousal", "action": "calm"})
    # 3) 中性 → 正常表达
    if not checks:
        checks.append({"issue": "neutral", "action": "normal"})
    return {"feeling": {"valence": round(valence, 2), "arousal": round(arousal, 2)},
            "checks": checks, "expressed": adjusted[:100],
            "note": "先感受后表达（情绪检查→调整→输出）"}


def emotional_gate(response: str) -> dict:
    """情感门：表达前自动评估（默认中性）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.affect import assess
        r = assess(str(response)[:200])
        val = float(r.get("valence", 0))
        aro = float(r.get("arousal", 0.3))
        return feel_first(response, val, aro)
    except Exception:
        return feel_first(response, 0.0, 0.3)
