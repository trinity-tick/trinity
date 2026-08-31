# -*- coding: utf-8 -*-
"""trinity/brain/cognitive_flexibility.py — 认知灵活性（EXECUTION 217，大脑化）。

借鉴 lex-cognitive-flexibility（执行功能扩展）——任务/策略切换：
大脑在前额叶协调下灵活切换策略（僵化=病理，灵活=适应）。

Trinity 现在：
  should_switch(current_strategy, performance): 性能下降/环境变化 →
    策略切换决策（何时改变）
  flexibility_score(): 切换历史评估（僵化/平衡/过于善变）

与注意力（选择）互补：注意力=选什么；灵活性=何时换。
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/flexibility_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"switches": [], "sticky": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def should_switch(current: str, performance: float, baseline: float = 0.6,
                  environment_changed: bool = False) -> dict:
    """切换决策：性能低于基线或环境变化 → 建议切换。"""
    st = _load()
    sticky = st.get("sticky", {})
    # 持续使用（粘性——避免过度切换）
    use_count = sticky.get(current, 0) + 1
    st["sticky"][current] = use_count
    _save(st)

    degrade = performance < baseline
    too_fresh = use_count < 3  # 至少用 3 次再评估（避免浮躁）
    switch = (degrade or environment_changed) and not too_fresh
    return {
        "switch": switch,
        "current": current,
        "performance": round(performance, 2),
        "below_baseline": degrade,
        "environment_changed": environment_changed,
        "used_times": use_count,
        "reason": ("性能下降" if degrade else "") + ("环境变化" if environment_changed else ""),
    }


def record_switch(from_strategy: str, to_strategy: str) -> dict:
    """记录策略切换（灵活性历史）。"""
    st = _load()
    st["switches"].append({"from": from_strategy, "to": to_strategy,
                           "ts": time.time()})
    st["switches"] = st["switches"][-30:]
    # 重置粘性
    st["sticky"] = {}
    _save(st)
    return {"switched": f"{from_strategy} → {to_strategy}",
            "total_switches": len(st["switches"])}


def flexibility_score() -> dict:
    """灵活性评估：切换次数与模式。"""
    st = _load()
    n = len(st.get("switches", []))
    # 最近 7 天切换数（近似：历史窗口）
    recent = [s for s in st.get("switches", []) if time.time() - s.get("ts", 0) < 7 * 86400]
    if n == 0:
        return {"score": 0, "verdict": "僵化（从未切换）"}
    if len(recent) > 15:
        return {"score": min(100, len(recent) * 5), "verdict": "过于善变（切换频繁）"}
    return {"score": min(100, 30 + len(recent) * 10),
            "verdict": "灵活（按需切换）", "recent_switches": len(recent)}
