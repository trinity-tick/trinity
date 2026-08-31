# -*- coding: utf-8 -*-
"""trinity/brain/selective_inattention.py — 刻意忽略（EXECUTION 324）。

借鉴 Selective Attention or Inattention（2026：Efficient Perception
in Embodied Agents）——刻意忽略：主动忽略低价值信号（注意的
反面——防信息过载——高效感知）。

与注意力（选择/反射）互补：注意=选重要；本模块=主动忽略。
Trinity 现在：
  ignore(signal, value): 刻意忽略评估（低价值→忽略/高价值→保留）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/selective_inattention.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ignored": {}, "kept": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


# 低价值信号词（重复/噪音类）
LOW_VALUE_WORDS = ("心跳", "状态正常", "例行", "已确认", "默认", "无变化")


def ignore(signal: str, value: float = 0.5) -> dict:
    """刻意忽略：低价值信号 → 忽略（高效感知）。"""
    text = str(signal)
    low_value = any(w in text for w in LOW_VALUE_WORDS)
    if low_value or value < 0.25:
        st = _load()
        key = text[:40]
        st["ignored"][key] = st["ignored"].get(key, 0) + 1
        _save(st)
        return {"ignored": True, "reason": "低价值" if low_value else "低权重",
                "note": f"刻意忽略：『{text[:25]}』（防信息过载）"}
    st = _load()
    st["kept"] += 1
    _save(st)
    return {"ignored": False, "reason": "高价值", "note": "保留——值得注意"}


def inattention_report() -> dict:
    """忽略效率：感知过滤效果。"""
    st = _load()
    ignored_total = sum(st.get("ignored", {}).values())
    return {"ignored_total": ignored_total, "kept": st.get("kept", 0),
            "efficiency": round(ignored_total * 100 / max(ignored_total + st.get("kept", 0), 1), 1),
            "note": f"刻意忽略：已过滤 {ignored_total} 条低价值信号（{round(ignored_total*100/max(ignored_total+st.get('kept',0),1),1)}% 降噪）"}
