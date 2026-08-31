# -*- coding: utf-8 -*-
"""trinity/brain/belief_dynamics.py — 信念锚定（EXECUTION 320）。

借鉴 ScioMind（2026：Anchoring-Based Belief Dynamics）——信念的
锚定动力学：初始印象锚定 + 后续证据微调（锚定效应——初始
信息权重高）。

与 DCPM（归纳信念）互补：DCPM=信念系统；本模块=锚定更新。
Trinity 现在：
  anchor_belief(belief, evidence): 锚定更新（初始锚定+证据微调）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/belief_dynamics.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"beliefs": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def anchor_belief(belief: str, evidence: float = 0.0, anchor_strength: float = 0.7) -> dict:
    """锚定更新：初始锚定（强）→ 证据微调（弱）。"""
    st = _load()
    key = str(belief)[:40]
    b = st["beliefs"].get(key, {"value": None, "updates": 0, "anchored": False})
    if b["value"] is None:
        # 初始锚定（锚定强度高）
        b["value"] = anchor_strength if evidence == 0 else max(0.3, min(0.9, evidence * 0.5 + anchor_strength * 0.5))
        b["anchored"] = True
        b["updates"] = 1
        note = "初始锚定（印象形成）"
    else:
        # 后续微调（锚定效应——变化小）
        adjust = (evidence - b["value"]) * 0.1  # 锚定：微调幅度小
        b["value"] = round(min(1.0, max(0.0, b["value"] + adjust)), 2)
        b["updates"] += 1
        note = f"锚定微调（第{b['updates']}次——变化小）"
    st["beliefs"][key] = b
    _save(st)
    return {"belief": key, "value": b["value"], "updates": b["updates"],
            "anchored": b["anchored"], "note": note}


def belief_report() -> dict:
    """信念状态。"""
    st = _load()
    beliefs = st.get("beliefs", {})
    return {"beliefs": len(beliefs),
            "avg_strength": round(sum(b["value"] for b in beliefs.values()) / max(len(beliefs), 1), 2),
            "note": "锚定信念动力学（初始锚定+微调——ScioMind）"}
