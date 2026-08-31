# -*- coding: utf-8 -*-
"""trinity/brain/rl_graph_evolution.py — RL 图进化（EXECUTION 355）。

借鉴 HAGE（2026：RL-Driven Weighted Graph Evolution——Agentic
Memory）——RL 驱动的记忆图进化：奖赏信号 → 图边权重调整
（奖赏高的路径强化——图随 RL 进化）。

与 SAGE（自动进化）互补：SAGE=结构；本模块=RL 驱动。
Trinity 现在：
  evolve_graph(reward_signal, edges): RL 图进化（权重更新）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/rl_graph_evolution.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"edges": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def evolve_graph(reward_signal: float, edges: list) -> dict:
    """RL 图进化：奖赏 → 图边权重调整。"""
    st = _load()
    lr = 0.2  # RL 学习率
    updated = []
    for e in edges[:10]:
        key = e.get("key", f"{e.get('from','?')}|{e.get('to','?')}")
        weight = float(e.get("weight", 0.5))
        # RL 更新（奖赏信号→权重调整）
        new_weight = weight + (reward_signal - 0.5) * lr
        new_weight = min(1.0, max(0.0, new_weight))
        st["edges"][key] = new_weight
        updated.append({"edge": key, "weight": round(new_weight, 2),
                        "delta": round(new_weight - weight, 2)})
    st["edges"] = dict(list(st["edges"].items())[-100:])
    _save(st)
    return {"reward": round(reward_signal, 2), "updated": updated,
            "note": f"RL 图进化：奖赏 {round(reward_signal,2)} → {len(updated)} 边权重调整（HAGE）"}


def graph_report() -> dict:
    """图进化状态。"""
    st = _load()
    edges = st.get("edges", {})
    return {"edges": len(edges),
            "avg_weight": round(sum(edges.values()) / max(len(edges), 1), 2),
            "note": "RL 加权图进化：奖赏驱动边权重（HAGE——Agentic Memory）"}
