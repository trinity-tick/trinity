# -*- coding: utf-8 -*-
"""trinity/brain/regret_learning.py — 后悔学习（EXECUTION 231，大脑化）。

借鉴 Psychological Regret Model（PRM）——决策后悔：实际结果 vs
反事实结果比较 → 后悔信号 → 调整未来决策（"早知道就..."）。

与反事实（209 想象）互补：反事实=设想；后悔=评估并学习。
Trinity 现在：
  evaluate_regret(decision, outcome, alternative): 后悔评估
  learn_from_regret(): 后悔 → 决策调整（避免重复）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/regret_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"regrets": [], "adjustments": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def evaluate_regret(decision: str, outcome_score: float, alternative_score: float) -> dict:
    """后悔评估：实际 vs 反事实（替代方案）结果比较。"""
    diff = alternative_score - outcome_score
    if diff > 0:
        level = "regret" if diff >= 0.3 else "mild_regret"
        signal = -min(diff, 1.0)
    else:
        level = "satisfied"
        signal = min(-diff, 1.0) * 0.5
    return {"decision": decision, "outcome": round(outcome_score, 2),
            "alternative": round(alternative_score, 2),
            "gap": round(diff, 2), "level": level, "signal": round(signal, 2)}


def learn_from_regret(decision: str, outcome_score: float, alternative_score: float) -> dict:
    """后悔学习：评估 → 记录 → 决策调整建议。"""
    ev = evaluate_regret(decision, outcome_score, alternative_score)
    st = _load()
    st["regrets"].append({"decision": decision, "level": ev["level"],
                          "gap": ev["gap"], "ts": __import__("time").time()})
    st["regrets"] = st["regrets"][-30:]
    adjustment = None
    if ev["level"] == "regret":
        st["adjustments"] += 1
        adjustment = f"避免再次选择『{decision}』（反事实表明替代方案更优）"
    _save(st)
    return {"evaluation": ev, "adjustment": adjustment,
            "regret_count": len([r for r in st["regrets"] if r["level"] == "regret"]),
            "total_adjustments": st["adjustments"]}


def regret_report() -> dict:
    """后悔学习报告（是否在改进）。"""
    st = _load()
    regrets = [r for r in st.get("regrets", []) if r["level"] == "regret"]
    return {"regrets_learned": len(regrets),
            "adjustments_made": st.get("adjustments", 0),
            "improving": st.get("adjustments", 0) > 0}
