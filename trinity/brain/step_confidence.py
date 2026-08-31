# -*- coding: utf-8 -*-
"""trinity/brain/step_confidence.py — 步骤置信（EXECUTION 282，大脑化）。

借鉴 Critic Experience Bank（2026：Step-Level Confidence Estimation）——
推理每一步的置信估计（不是整体置信——分步可信度）。

与校准（242 整体）互补：校准=整体；本模块=步骤级。
Trinity 现在：
  estimate_step(step, context): 步骤置信（经验+证据）
  step_bank(): 步骤经验库（积累）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/step_confidence.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"steps": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def estimate_step(step: str, evidence: int = 0, familiarity: float = 0.5) -> dict:
    """步骤置信：证据 × 熟悉度 → 每步可信度。"""
    # 经验历史（同类步骤成功率）
    st = _load()
    hist = st["steps"].get(step[:20], {"ok": 0, "fail": 0})
    total = hist["ok"] + hist["fail"]
    hist_rate = hist["ok"] / total if total else 0.5
    # 置信 = 证据(0.4) + 熟悉(0.3) + 历史(0.3)
    conf = evidence * 0.4 + familiarity * 0.3 + hist_rate * 0.3
    conf = min(1.0, max(0.0, conf))
    return {"step": str(step)[:40], "confidence": round(conf, 2),
            "evidence": evidence, "familiarity": familiarity,
            "history": round(hist_rate, 2),
            "level": "high" if conf >= 0.7 else ("medium" if conf >= 0.4 else "low")}


def record_step_outcome(step: str, success: bool) -> dict:
    """记录步骤结果（经验库积累）。"""
    st = _load()
    s = st["steps"].get(step[:20], {"ok": 0, "fail": 0})
    if success:
        s["ok"] += 1
    else:
        s["fail"] += 1
    st["steps"][step[:20]] = s
    st["steps"] = dict(list(st["steps"].items())[-100:])
    _save(st)
    return {"recorded": True, "step": str(step)[:20], "ok": s["ok"], "fail": s["fail"]}


def step_report() -> dict:
    """步骤置信体系状态。"""
    st = _load()
    return {"steps_tracked": len(st.get("steps", {})),
            "note": "步骤级置信：每步可信度（Critic Experience Bank）"}
