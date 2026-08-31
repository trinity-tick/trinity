# -*- coding: utf-8 -*-
"""trinity/brain/introspective_reward.py — 内省奖赏（EXECUTION 284，大脑化）。

借鉴 Exploration Through Introspection（2026：Self-Aware Reward
Model）——内省驱动的奖赏：自我发现/领悟 → 内在奖赏信号
（探索不只是外部奖赏——内省本身有价值）。

与多巴胺（外部结果）互补：外部=行为结果；内省=自我发现。
Trinity 现在：
  introspect_reward(discovery, novelty): 内省奖赏（发现价值评估）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/introspective_reward.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"discoveries": [], "total_reward": 0.0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def introspect_reward(discovery: str, novelty: float = 0.5,
                      insight_depth: float = 0.5) -> dict:
    """内省奖赏：自我发现 → 奖赏信号（新颖×深度）。"""
    reward = novelty * 0.5 + insight_depth * 0.5
    st = _load()
    entry = {"discovery": str(discovery)[:60], "reward": round(reward, 2),
             "ts": __import__("time").time()}
    st["discoveries"].append(entry)
    st["discoveries"] = st["discoveries"][-30:]
    st["total_reward"] += reward
    _save(st)
    return {"discovery": entry["discovery"][:40], "reward": round(reward, 2),
            "intrinsic": True,
            "note": "内省奖赏（自我发现——内在驱动）"}


def introspection_report() -> dict:
    """内省奖赏状态。"""
    st = _load()
    discoveries = st.get("discoveries", [])
    return {"discoveries": len(discoveries),
            "total_intrinsic_reward": round(st.get("total_reward", 0), 2),
            "avg": round(st.get("total_reward", 0) / max(len(discoveries), 1), 2),
            "note": "内省探索：自我发现驱动（不依赖外部奖赏）"}
