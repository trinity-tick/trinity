# -*- coding: utf-8 -*-
"""trinity/brain/episodic_rl.py — 情景 RL（EXECUTION 371）。

借鉴 MemRL（2026：Self-Evolving Agents via Runtime Reinforcement
Learning on Episodic Memory）——情景记忆上的运行时强化学习：
从情景经验中运行时学习（每段情景→策略更新——自进化）。

与 JIT-RL（即时）互补：即时=通用规则；本模块=情景驱动。
Trinity 现在：
  episodic_learn(episode, reward): 情景 RL（情景→策略更新）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/episodic_rl.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"episodes": [], "policy": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def episodic_learn(episode: str, reward: float = 0.5) -> dict:
    """情景 RL：情景经验 → 策略更新（运行时学习）。"""
    st = _load()
    st["episodes"].append({"episode": str(episode)[:50], "reward": reward,
                           "ts": __import__("time").time()})
    st["episodes"] = st["episodes"][-30:]
    # 策略更新（从情景提炼动作-价值）
    key = str(episode)[:20]
    p = st["policy"].get(key, {"value": 0.5, "count": 0})
    p["value"] = round(min(1.0, max(0.0, p["value"] * 0.7 + reward * 0.3)), 2)
    p["count"] += 1
    st["policy"][key] = p
    _save(st)
    return {"episode": str(episode)[:30], "reward": round(reward, 2),
            "policy_value": p["value"], "episodes": len(st["episodes"]),
            "note": f"情景 RL：'{key[:15]}' 策略值 {p['value']}（运行时自进化）"}


def episodic_report() -> dict:
    """情景 RL 状态。"""
    st = _load()
    return {"episodes": len(st.get("episodes", [])),
            "policy_entries": len(st.get("policy", {})),
            "note": "MemRL：情景记忆运行时 RL（自进化）"}
