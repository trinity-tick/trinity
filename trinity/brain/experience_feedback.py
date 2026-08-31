# -*- coding: utf-8 -*-
"""trinity/brain/experience_feedback.py — 经验反馈学习（EXECUTION 268）。

借鉴 Dejavu（CVPR 2026：Experience Feedback Learning）——从过往
交互提炼泛化策略并持续反馈调整（防"经验性遗忘"——策略失效）。

与策略库（提炼）互补：策略库=提炼；反馈=验证调整。
Trinity 现在：
  apply_strategy(strategy, context): 应用策略
  feedback(strategy, outcome): 效果反馈（好→强化/差→调整）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/experience_feedback.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"strategies": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def apply_strategy(strategy: str, context: str = "") -> dict:
    """应用策略：在上下文中执行。"""
    return {"applied": str(strategy)[:50], "context": str(context)[:30],
            "phase": "apply"}


def feedback(strategy: str, outcome_score: float) -> dict:
    """效果反馈：好→强化权重；差→下调（防经验性遗忘）。"""
    st = _load()
    s = st["strategies"].get(strategy, {"score": 0.5, "uses": 0})
    s["uses"] += 1
    # 反馈学习：加权更新（EMA）
    s["score"] = round(min(1.0, max(0.0, s["score"] * 0.8 + outcome_score * 0.2)), 2)
    st["strategies"][strategy] = s
    _save(st)
    verdict = "强化" if outcome_score >= 0.6 else ("调整" if outcome_score >= 0.4 else "降权")
    return {"strategy": str(strategy)[:40], "score": s["score"],
            "uses": s["uses"], "verdict": verdict,
            "note": f"反馈学习：策略{'强化' if verdict=='强化' else '待调整'}（防遗忘）"}


def feedback_report() -> dict:
    """反馈学习状态。"""
    st = _load()
    strategies = st.get("strategies", {})
    strong = [s for s, d in strategies.items() if d.get("score", 0) >= 0.7]
    weak = [s for s, d in strategies.items() if d.get("score", 0) < 0.4]
    return {"strategies": len(strategies), "strong": strong[:3], "weak": weak[:3],
            "note": "经验反馈闭环：好用→强化/失效→调整"}
