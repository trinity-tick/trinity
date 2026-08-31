# -*- coding: utf-8 -*-
"""trinity/brain/strategic_surprise.py — 策略惊喜（EXECUTION 325）。

借鉴 SuS（2026：Strategy-aware Surprise for Intrinsic Exploration）——
超越状态新颖性的探索：惊喜 × 策略价值（不仅"新"还要"有用"——
战略惊喜）。

与 surprise 编码（内容新颖）互补：新颖=状态；本模块=策略价值。
Trinity 现在：
  strategic_value(state, strategy): 策略惊喜（探索价值评估）
"""
import os
import sys
import json


def strategic_value(state: str, strategy: str, novelty: float = 0.5) -> dict:
    """策略惊喜：状态新颖 × 策略相关性 → 探索价值。"""
    # 策略相关性（惊喜状态与当前策略的关联）
    relevance = 0.5
    if strategy and state:
        # 共享词
        words_s = set()
        for i in range(len(state) - 1):
            if "\u4e00" <= state[i] <= "\u9fff" and "\u4e00" <= state[i+1] <= "\u9fff":
                words_s.add(state[i:i+2])
        words_t = set()
        for i in range(len(strategy) - 1):
            if "\u4e00" <= strategy[i] <= "\u9fff" and "\u4e00" <= strategy[i+1] <= "\u9fff":
                words_t.add(strategy[i:i+2])
        if words_s and words_t:
            relevance = len(words_s & words_t) / max(len(words_t), 1)
    # 探索价值 = 新颖 × 相关性
    value = novelty * 0.5 + relevance * 0.5
    return {"state": str(state)[:30], "strategy": str(strategy)[:30],
            "novelty": round(novelty, 2), "relevance": round(relevance, 2),
            "explore_value": round(value, 2),
            "worth_exploring": value >= 0.55,
            "note": f"策略惊喜：{'值得探索' if value >= 0.55 else '价值不足'}（新颖 {round(novelty,2)}×策略 {round(relevance,2)}）"}


def strategic_explore(states: list, strategy: str) -> dict:
    """策略探索：按策略价值排序探索目标。"""
    scored = []
    for s in states[:10]:
        r = strategic_value(str(s.get("state", "")), strategy, float(s.get("novelty", 0.5)))
        scored.append({"state": r["state"], "value": r["explore_value"]})
    scored.sort(key=lambda x: -x["value"])
    return {"ranked": scored[:5], "top": scored[0] if scored else None,
            "note": "策略感知探索：超越状态新颖性（SuS）"}
