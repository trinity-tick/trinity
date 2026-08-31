# -*- coding: utf-8 -*-
"""trinity/brain/execute_distill_verify.py — 执行-蒸馏-验证（EXECUTION 263）。

借鉴 Execute-Distill-Verify（2026：Escaping the Self-Confirmation
Trap）——经验学习三阶段：执行→蒸馏→验证（只学验证过的经验，
防自我确认偏差——"我以为有效"≠"验证有效"）。

Trinity 现在：
  execute(plan): 执行动作
  distill(experience): 蒸馏候选经验（提炼）
  verify(experience, evidence): 验证（有证据支持才采纳）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/edv_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"verified": [], "rejected": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def execute(plan: str) -> dict:
    """执行阶段：执行计划动作。"""
    return {"executed": str(plan)[:60], "phase": "execute"}


def distill(experience: str) -> dict:
    """蒸馏阶段：提炼候选经验（从原始经验提取可复用规律）。"""
    return {"candidate": f"经验规律：{str(experience)[:80]}", "phase": "distill",
            "note": "已蒸馏为候选经验（待验证）"}


def verify(candidate: str, evidence_sources: int = 0,
           consistency: float = 0.5) -> dict:
    """验证阶段：有证据支持（>=1 源 + 一致性 >=0.6）才采纳。"""
    st = _load()
    supported = evidence_sources >= 1 and consistency >= 0.6
    entry = {"candidate": str(candidate)[:80], "evidence": evidence_sources,
             "consistency": consistency, "ts": __import__("time").time()}
    if supported:
        st["verified"].append(entry)
        verdict = "verified"
    else:
        st["rejected"].append(entry)
        verdict = "rejected"
    st["verified"] = st["verified"][-30:]
    st["rejected"] = st["rejected"][-30:]
    _save(st)
    return {"verdict": verdict, "candidate": entry["candidate"][:50],
            "note": ("验证通过——采纳为经验" if supported
                     else "证据不足——拒绝（防自我确认陷阱）")}


def edv_status() -> dict:
    """三阶段状态：验证通过/拒绝比例。"""
    st = _load()
    v = len(st.get("verified", []))
    r = len(st.get("rejected", []))
    total = v + r
    return {"verified": v, "rejected": r,
            "acceptance_rate": round(v * 100 / max(total, 1), 1) if total else 0,
            "note": "执行-蒸馏-验证（只学验证过的经验）"}
