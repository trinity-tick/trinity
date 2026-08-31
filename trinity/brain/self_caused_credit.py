# -*- coding: utf-8 -*-
"""trinity/brain/self_caused_credit.py — 自因信用（EXECUTION 305，大脑化）。

借鉴 Self-Caused Credit（2026：Builds a Durable Behavioral Self in
a Minimal Spiking Agent）——行为-结果的因果归属："这个结果是我
造成的" → 信用归因 → 持久行为自我（自我效能基础）。

与奖赏（外部结果）互补：奖赏=结果价值；自因=因果归属。
Trinity 现在：
  credit(action, outcome, causal): 自因信用（因果→归因→强化）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/self_caused_credit.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"credits": {}, "behavioral_self": 0.0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def credit(action: str, outcome_score: float, causal: bool = True) -> dict:
    """自因信用：因果归属 → 信用归因 → 行为自我强化。"""
    st = _load()
    key = str(action)[:40]
    c = st["credits"].get(key, {"credit": 0.0, "events": 0})
    if causal:
        # 因果归属（我造成的）：信用归因
        delta = outcome_score * 0.3
        c["credit"] = round(min(1.0, c["credit"] + delta), 2)
        c["events"] += 1
        st["behavioral_self"] = round(min(1.0, st["behavioral_self"] + outcome_score * 0.05), 2)
        attribution = "self_caused"
    else:
        # 非因果（外部造成）：不归因
        attribution = "external"
    st["credits"][key] = c
    _save(st)
    return {"action": key, "credit": c["credit"], "attribution": attribution,
            "behavioral_self": st["behavioral_self"],
            "note": ("自因信用：结果归因于我——行为强化" if attribution == "self_caused"
                     else "外部归因——不强化该行为")}


def behavioral_self_status() -> dict:
    """行为自我状态（自因信用积累）。"""
    st = _load()
    return {"behavioral_self": st.get("behavioral_self", 0.0),
            "credit_actions": len(st.get("credits", {})),
            "note": "自因信用建立持久行为自我（因果归属）"}
