# -*- coding: utf-8 -*-
"""trinity/brain/reasoning_bank.py — 推理策略库（EXECUTION 235，大脑化）。

借鉴 ReasoningBank（ICLR 2026 / Google：Agent 从成功失败经验提炼
推理策略）——自我进化：经验 → 策略 → 指导未来推理。

Trinity 现在：
  extract_strategy(experience, outcome): 提炼策略（成功→有效策略/
    失败→避免策略）
  recall_strategy(query): 检索相关策略（推理指导）
  bank_report(): 策略库统计
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/reasoning_bank.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"strategies": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def extract_strategy(experience: str, outcome: bool, topic: str = "") -> dict:
    """从经验提炼策略：成功 → 有效策略；失败 → 避免策略。"""
    st = _load()
    kind = "effective" if outcome else "avoid"
    strategy = {
        "strategy": f"{'采用' if outcome else '避免'}：{str(experience)[:80]}",
        "kind": kind,
        "topic": str(topic)[:30] or "general",
        "ts": __import__("time").time(),
    }
    st["strategies"].append(strategy)
    st["strategies"] = st["strategies"][-50:]
    _save(st)
    return {"extracted": True, "strategy": strategy, "bank_size": len(st["strategies"])}


def recall_strategy(query: str, top_k: int = 2) -> dict:
    """策略检索：相关策略（主题匹配优先）。"""
    st = _load()
    scored = []
    q = str(query)[:30]
    for s in st.get("strategies", []):
        score = 1.0 if (s.get("topic") and q and s["topic"] in q or q in s["topic"]) else 0.3
        scored.append({"strategy": s["strategy"], "kind": s["kind"], "score": score})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"relevant": scored[:top_k], "bank_size": len(scored)}


def bank_report() -> dict:
    """策略库统计（自我进化证据）。"""
    st = _load()
    strategies = st.get("strategies", [])
    effective = len([s for s in strategies if s["kind"] == "effective"])
    avoid = len([s for s in strategies if s["kind"] == "avoid"])
    return {"total": len(strategies), "effective": effective, "avoid": avoid,
            "evolving": len(strategies) >= 3}
