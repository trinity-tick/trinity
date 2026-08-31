# -*- coding: utf-8 -*-
"""trinity/brain/emotion_regulation.py — 情绪调节（EXECUTION 211，大脑化）。

前额叶-杏仁核回路：大脑的认知重评（emotion regulation）——调节
情绪极端化（过度消极/过度积极都回稳）。Trinity 现在：
  regulate(state): 认知重评——valence 钳位（极端→回稳）+ 情绪标签
  regulated_bias(): 调节后的检索偏置（避免极端偏置）

与状态机（积累）互补：状态机=记录；调节=稳态管理。
"""
import os
import sys
import json


def regulate(state: dict, clamp: float = 0.6) -> dict:
    """认知重评：情绪状态钳位回稳（极端 → 调节）。"""
    if not state:
        return {"regulated": False, "note": "no state"}
    valence = float(state.get("valence") or 0)
    arousal = float(state.get("arousal") or 0)
    polarity = state.get("polarity", "neu")

    # 钳位（前额叶抑制杏仁核过度反应）
    orig_v = valence
    valence = max(-clamp, min(clamp, valence))
    # 唤醒度调节（过度激动 → 缓和）
    orig_a = arousal
    arousal = arousal * 0.9 if arousal > 0.8 else arousal
    # 调节后的极性（近零 → neutral）
    if abs(valence) < 0.15:
        polarity = "neu"
    elif valence > 0:
        polarity = "pos"
    else:
        polarity = "neg"

    changed = abs(valence - orig_v) > 0.001 or abs(arousal - orig_a) > 0.001
    return {
        "regulated": True,
        "before": {"valence": round(orig_v, 2), "arousal": round(orig_a, 2)},
        "after": {"valence": round(valence, 2), "arousal": round(arousal, 2),
                  "polarity": polarity},
        "clamped": abs(valence) == clamp or abs(orig_v) > clamp,
        "changed": changed,
    }


def regulated_bias(state: dict) -> dict:
    """调节后的检索偏置（避免极端偏置）。"""
    r = regulate(state)
    if not r.get("regulated"):
        return {"bias": None, "note": "no state"}
    after = r["after"]
    bias = {"value_boost": 0.0}
    if after["polarity"] == "neg" and abs(after["valence"]) >= 0.3:
        bias["category_hint"] = "incident"
        bias["value_boost"] = round(abs(after["valence"]) * 0.15, 2)
    else:
        bias["category_hint"] = None
    return {"bias": bias, "regulated_from": r.get("before", {}).get("valence")}
