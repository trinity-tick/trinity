# -*- coding: utf-8 -*-
"""trinity/brain/attention_control.py — 注意力控制（EXECUTION 207，大脑化）。

借鉴 Emergent Cognitive Architecture（2026：动态注意控制）——
大脑同时面对多个刺激时，按"显著性×价值×目标相关性"竞争分配
处理资源（注意瓶颈）。

Trinity 现在：
  attend(candidates): 候选刺激（感知信号/任务/查询）→ 竞争评分 →
                      top 优先处理，其余延迟（注意瓶颈）
  抑制：低分信号进入"注意抑制"（延迟/降权——习惯化互补）
"""
import os
import sys
import json
import time


def attend(candidates: list, goal_focus: str = "", top_n: int = 2) -> dict:
    """注意力竞争：按显著性×价值×目标相关性评分排序。"""
    scored = []
    for c in candidates[:20]:
        try:
            salience = float(c.get("salience") or 0.5)
            value = float(c.get("value") or 0.5)
            goal = 0.5
            if goal_focus:
                g = str(c.get("signal") or c.get("name") or "")
                goal = 1.0 if goal_focus in g else 0.3
            score = salience * value * goal
            scored.append({"item": c.get("signal") or c.get("name") or "?",
                           "salience": salience, "value": value,
                           "goal_match": goal, "score": round(score, 3)})
        except Exception:
            continue
    scored.sort(key=lambda x: x["score"], reverse=True)
    attended = scored[:top_n]
    suppressed = scored[top_n:]
    return {"attended": attended, "suppressed": suppressed,
            "attention_bottleneck": len(scored) - len(attended)}


def focus_shift(current_focus: str, new_signal: str, priority: float) -> dict:
    """注意转移：高优先新刺激 → 注意重定向。"""
    shift = priority >= 0.8 and new_signal != current_focus
    return {"shifted": shift, "from": current_focus, "to": new_signal if shift else current_focus,
            "priority": priority}


def attention_report() -> dict:
    """注意力状态报告（最近决策）。"""
    try:
        f = os.path.expanduser("~/.trinity/attention_state.json")
        if os.path.exists(f):
            return json.load(open(f, encoding="utf-8"))
        return {"decisions": 0}
    except Exception:
        return {"decisions": 0}
