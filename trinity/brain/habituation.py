# -*- coding: utf-8 -*-
"""trinity/brain/habituation.py — 习惯化（EXECUTION 280，大脑化）。

借鉴 cognitive-science perception framework（2026：Habituation）——
重复刺激 → 反应减弱（感知适应：同一信号重复出现降低响应，
防噪音疲劳；新异信号保持警觉）。

Trinity 现在：
  exposure(signal): 暴露追踪（信号频率）
  habituate(signal): 习惯化（重复→响应减弱/新异→警觉）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/habituation_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"exposures": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def exposure(signal: str) -> dict:
    """暴露追踪：记录信号出现次数。"""
    st = _load()
    s = st["exposures"].get(signal, {"count": 0, "last": 0})
    s["count"] += 1
    s["last"] = time.time()
    st["exposures"][signal] = s
    st["exposures"] = dict(list(st["exposures"].items())[-100:])
    _save(st)
    return {"signal": str(signal)[:30], "count": s["count"]}


def habituate(signal: str) -> dict:
    """习惯化：重复次数 → 响应水平（减弱/正常/警觉）。"""
    st = _load()
    s = st["exposures"].get(signal, {"count": 0})
    count = s.get("count", 0)
    if count >= 10:
        level = "habituated"
        response = 0.2  # 响应大幅减弱（已习惯）
    elif count >= 5:
        level = "reduced"
        response = 0.5  # 响应减弱
    elif count >= 2:
        level = "noticing"
        response = 0.8  # 开始注意
    else:
        level = "novel"
        response = 1.0  # 新异——保持警觉
    return {"signal": str(signal)[:30], "count": count, "level": level,
            "response": response,
            "note": f"暴露 {count} 次 → {level}（{'响应减弱' if count>=5 else '保持警觉' if count<2 else '开始注意'}）"}


def habituation_report() -> dict:
    """习惯化状态：感知适应水平。"""
    st = _load()
    exposures = st.get("exposures", {})
    habituated = sum(1 for s in exposures.values() if s.get("count", 0) >= 10)
    return {"signals_tracked": len(exposures), "habituated": habituated,
            "note": "习惯化：重复信号响应减弱（防噪音疲劳）"}
