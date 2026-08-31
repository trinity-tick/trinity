# -*- coding: utf-8 -*-
"""trinity/brain/algorithmic_individuation.py — 算法个体化（EXECUTION 379）。

借鉴 Enactive Cognitive Architectures（2026：Homeostatic Approach
to Functional Teleology and Algorithmic Individuation）——算法
个体化：认知架构随经验分化成独特个体（功能目的论——稳态
维持个体性）。

与性格结晶（倾向固化）互补：结晶=特质；本模块=个体分化。
Trinity 现在：
  individuate(experience): 算法个体化（经验→独特特征）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/algorithmic_individuation.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"traits": {}, "individuality": 0.1}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def individuate(experience: str, intensity: float = 0.3) -> dict:
    """算法个体化：经验 → 独特特征分化。"""
    st = _load()
    # 经验特征提取（主题词）
    trait = None
    for w in ("数据库", "优化", "协作", "探索", "谨慎", "创新"):
        if w in str(experience):
            trait = w
            break
    if trait:
        t = st["traits"].get(trait, {"strength": 0.0, "exposures": 0})
        t["strength"] = min(1.0, t["strength"] + intensity)
        t["exposures"] += 1
        st["traits"][trait] = t
    # 个体性（独特特征累积——分化度）
    st["individuality"] = round(min(1.0, st["individuality"] + intensity * 0.1), 3)
    _save(st)
    return {"experience": str(experience)[:25], "trait": trait,
            "individuality": st["individuality"],
            "trait_count": len(st["traits"]),
            "note": f"算法个体化：{'特征『' + trait + '』分化' if trait else '通用经验'}（个体性 {st['individuality']}——Enactive）"}


def individuation_report() -> dict:
    """个体化状态。"""
    st = _load()
    return {"individuality": st.get("individuality", 0.1),
            "traits": list(st.get("traits", {}).keys())[:5],
            "note": "算法个体化：稳态功能目的（独特认知个体）"}
