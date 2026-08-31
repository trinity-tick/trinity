# -*- coding: utf-8 -*-
"""trinity/brain/goal_commitment.py — 目标承诺（EXECUTION 257，大脑化）。

借鉴 Goals as Dynamical Attractors（2026）——目标作为动力吸引子：
稳定的目标承诺（动量保持）+ 灵活的重新评估（进展停滞时）。

Trinity 现在：
  commit(goal, importance, progress): 目标承诺强度（吸引子深度）
  update_commitment(goal, progress_delta): 进展→承诺更新（强化/松绑）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/goal_commitment.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"goals": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def commit(goal: str, importance: float = 0.7, progress: float = 0.2) -> dict:
    """目标承诺：吸引子强度（importance × 进展动量）。"""
    st = _load()
    existing = st["goals"].get(goal, {"commitment": 0, "progress": 0, "ts": time.time()})
    # 承诺 = 重要性 × (0.6 + 进展动量)
    commitment = min(1.0, importance * (0.6 + progress * 0.8))
    existing["commitment"] = round(commitment, 2)
    existing["progress"] = progress
    existing["ts"] = time.time()
    st["goals"][goal] = existing
    _save(st)
    return {"goal": str(goal)[:30], "commitment": round(commitment, 2),
            "state": "strong" if commitment >= 0.7 else ("moderate" if commitment >= 0.4 else "weak")}


def update_commitment(goal: str, progress_delta: float = 0.1) -> dict:
    """进展更新：进展好→承诺强化；停滞→重新评估（灵活）。"""
    st = _load()
    g = st["goals"].get(goal)
    if not g:
        return {"error": "goal not committed"}
    new_progress = min(1.0, g["progress"] + progress_delta)
    # 动量更新：进展 → 承诺微调（吸引子动力学）
    if progress_delta >= 0:
        g["commitment"] = min(1.0, g["commitment"] + progress_delta * 0.5)
        action = "strengthen"
    else:
        g["commitment"] = max(0.1, g["commitment"] + progress_delta * 0.5)
        action = "reassess" if g["commitment"] < 0.4 else "loosen"
    g["progress"] = round(new_progress, 2)
    st["goals"][goal] = g
    _save(st)
    return {"goal": str(goal)[:30], "progress": g["progress"],
            "commitment": g["commitment"], "action": action,
            "note": "目标进展驱动承诺（吸引子动力学）"}


def commitment_report() -> dict:
    """承诺状态报告。"""
    st = _load()
    goals = st.get("goals", {})
    strong = [g for g, d in goals.items() if d.get("commitment", 0) >= 0.7]
    return {"goals": len(goals), "strong_commitments": strong[:3],
            "note": "目标吸引子：稳定保持 + 灵活评估"}
