# -*- coding: utf-8 -*-
"""trinity/brain/jit_reinforcement.py — 即时强化学习（EXECUTION 318）。

借鉴 Just-In-Time RL（2026：Continual Learning Without Gradient
Updates）——无梯度的持续学习：即时调整策略权重（不需要
反向传播——状态-动作-奖赏即时更新）。

与奖赏（多巴胺）互补：多巴胺=信号；本模块=即时学习规则。
Trinity 现在：
  jit_learn(state, action, reward): 即时学习（无梯度策略调整）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/jit_rl.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"policy": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def jit_learn(state: str, action: str, reward: float) -> dict:
    """即时学习：无梯度策略调整（即时更新权重）。"""
    st = _load()
    key = f"{state[:15]}|{action[:15]}"
    entry = st["policy"].get(key, {"weight": 0.5, "updates": 0})
    # 即时更新（无梯度——直接加权调整）
    lr = 0.3  # 即时学习率
    entry["weight"] = round(min(1.0, max(0.0, entry["weight"] + (reward - 0.5) * lr)), 3)
    entry["updates"] += 1
    st["policy"][key] = entry
    st["policy"] = dict(list(st["policy"].items())[-100:])
    _save(st)
    return {"state": str(state)[:15], "action": str(action)[:15],
            "reward": reward, "weight": entry["weight"],
            "updates": entry["updates"],
            "note": f"即时学习：权重 {'上调' if reward >= 0.6 else '下调'}（无梯度）"}


def policy_status(state: str = "") -> dict:
    """策略状态：当前权重分布。"""
    st = _load()
    policy = st.get("policy", {})
    if state:
        relevant = {k: v for k, v in policy.items() if k.startswith(state[:15])}
    else:
        relevant = policy
    best = max(relevant.items(), key=lambda x: x[1]["weight"]) if relevant else None
    return {"pairs": len(relevant), "updates": sum(v["updates"] for v in relevant.values()),
            "best": {"pair": best[0], "weight": best[1]["weight"]} if best else None,
            "note": "即时强化学习：无梯度持续调整（JIT-RL）"}
