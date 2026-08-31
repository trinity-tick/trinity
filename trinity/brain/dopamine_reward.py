# -*- coding: utf-8 -*-
"""trinity/brain/dopamine_reward.py — 多巴胺奖赏（EXECUTION 214，大脑化）。

借鉴 Dopamine-Modulated Plasticity（2026 IEEE）——奖赏预测误差
驱动学习。Trinity 现在：
  reward(result): 行动结果 → 奖赏信号（成功 +1.0 / 失败 -0.5）
  dopamine_level(): 奖赏累计水平（影响行为倾向——高奖赏→更积极）
  bias_by_dopamine(): 奖赏水平 → 检索/行动倾向（乐观/谨慎）

与条件反射（182）互补：条件反射=成功率统计；多巴胺=奖赏-情绪信号。
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/dopamine_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"level": 0.5, "events": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def reward(action: str, done: bool, strength: float = 1.0) -> dict:
    """奖赏信号：成功 → +1.0×strength；失败 → -0.5×strength（EMA 平滑）。"""
    st = _load()
    signal = (1.0 if done else -0.5) * strength
    level = st.get("level", 0.5)
    new_level = max(0.0, min(1.0, level * 0.8 + (signal * 0.2 + 0.5) * 0.2))
    st["level"] = round(new_level, 3)
    st["events"].append({"action": action, "done": done,
                         "signal": round(signal, 2), "level": round(new_level, 3)})
    st["events"] = st["events"][-50:]
    _save(st)
    return {"signal": round(signal, 2), "level": round(new_level, 3),
            "delta": round(new_level - level, 3)}


def dopamine_level() -> float:
    """当前多巴胺水平（0 悲观 - 1 乐观，0.5 中性）。"""
    return _load().get("level", 0.5)


def bias_by_dopamine() -> dict:
    """奖赏水平 → 行为倾向。"""
    lvl = dopamine_level()
    if lvl >= 0.65:
        return {"tendency": "optimistic", "exploration_boost": 0.15,
                "level": lvl}
    if lvl <= 0.35:
        return {"tendency": "pessimistic", "exploration_boost": -0.15,
                "level": lvl}
    return {"tendency": "neutral", "exploration_boost": 0.0, "level": lvl}
