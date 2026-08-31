# -*- coding: utf-8 -*-
"""trinity/brain/retention_influence.py — 保留-影响分离（EXECUTION 304）。

借鉴 DREAM v3（2026：Dynamic Retention Episodic Architecture——
Separation of Retention and Influence）——记忆的"保留"与"影响"
分离：记忆可以保留（不删除）但降低影响（不干扰行为）。

与治理（内容可靠）互补：治理=质量；本模块=作用分离。
Trinity 现在：
  separate(memory): 保留-影响分离（设置影响权重）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/retention_influence.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"separated": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def separate(memory: str, influence_weight: float = 0.2) -> dict:
    """保留-影响分离：记忆保留但影响受限。"""
    st = _load()
    key = str(memory)[:40]
    st["separated"][key] = {
        "retained": True,
        "influence": min(1.0, max(0.0, influence_weight)),
        "ts": __import__("time").time(),
    }
    st["separated"] = dict(list(st["separated"].items())[-50:])
    _save(st)
    level = "low_influence" if influence_weight <= 0.3 else ("moderate" if influence_weight <= 0.6 else "active")
    return {"memory": key, "retained": True, "influence": influence_weight,
            "level": level,
            "note": f"保留但影响 {influence_weight}（{level}——不干扰决策）"}


def influence_check(memory: str) -> dict:
    """影响检查：记忆当前影响级别。"""
    st = _load()
    key = str(memory)[:40]
    entry = st["separated"].get(key)
    if not entry:
        return {"memory": key, "retained": True, "influence": 1.0,
                "level": "active", "note": "未分离——正常影响"}
    return {"memory": key, "retained": entry["retained"],
            "influence": entry["influence"],
            "level": "low_influence" if entry["influence"] <= 0.3 else "active",
            "note": "保留但影响受限（不干扰决策）"}
