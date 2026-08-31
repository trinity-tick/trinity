# -*- coding: utf-8 -*-
"""trinity/brain/personality_crystallization.py — 性格特质结晶（EXECUTION 260）。

借鉴 Growth Vector Crystallization（2026：成长向量结晶为永久特质）——
反复出现的行为模式结晶为稳定性格特质（性格=习惯化的行为倾向）。

Trinity 现在：
  crystallize(behavior): 行为模式累积→结晶（>=3 次→特质）
  personality_profile(): 性格档案（已结晶特质）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/personality_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"growth_vectors": {}, "traits": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def crystallize(behavior: str, intensity: float = 0.5) -> dict:
    """行为结晶：反复行为（>=3 次强度累积）→ 永久特质。"""
    st = _load()
    gv = st["growth_vectors"].get(behavior, {"count": 0, "intensity": 0})
    gv["count"] += 1
    gv["intensity"] = min(1.0, gv["intensity"] + intensity * 0.3)
    st["growth_vectors"][behavior] = gv
    # 结晶条件：>=3 次 + 强度 >= 0.5
    if gv["count"] >= 3 and gv["intensity"] >= 0.5:
        st["traits"][behavior] = {"strength": round(gv["intensity"], 2),
                                  "crystallized": True}
    _save(st)
    return {"behavior": str(behavior)[:30], "count": gv["count"],
            "crystallized": behavior in st["traits"]}


def personality_profile() -> dict:
    """性格档案：已结晶特质。"""
    st = _load()
    traits = st.get("traits", {})
    # 性格维度归类
    dims = {"谨慎": "conscientious", "探索": "openness", "协作": "agreeableness"}
    classified = []
    for t, d in traits.items():
        dim = next((v for k, v in dims.items() if k in t), "general")
        classified.append({"trait": t, "dimension": dim,
                           "strength": d.get("strength", 0.5)})
    return {"traits": classified, "count": len(classified),
            "growing": len(st.get("growth_vectors", {})),
            "note": "性格 = 结晶的行为倾向"}
