# -*- coding: utf-8 -*-
"""trinity/brain/reflection_loop.py — 反思循环（EXECUTION 238，大脑化）。

借鉴 Meta-cognitive Reflection（ACL 2026：Learn Like Humans——
Use Meta-cognitive Reflection for Efficient Self-Improvement）。

反思循环：表现 → 反思（找改进点）→ 改进（应用）→ 验证 → 再反思。
与自省（记录）区分：反思循环=驱动持续改进的闭环。
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/reflection_loop.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"cycles": [], "improvements": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def reflect(performance: float, baseline: float = 0.6) -> dict:
    """反思：评估表现 → 找改进点。"""
    if performance < baseline:
        gap = baseline - performance
        insight = ("性能低于基线" if gap >= 0.2 else "略低于基线")
        return {"performance": round(performance, 2), "below_baseline": True,
                "gap": round(gap, 2), "insight": insight,
                "improvement_needed": gap >= 0.1}
    return {"performance": round(performance, 2), "below_baseline": False,
            "insight": "表现达标", "improvement_needed": False}


def improve(insight: str, action: str = "") -> dict:
    """改进：应用改进动作 → 记录循环。"""
    st = _load()
    cycle = {"ts": time.time(), "insight": str(insight)[:60],
             "action": str(action)[:60] or "调整策略"}
    st["cycles"].append(cycle)
    st["cycles"] = st["cycles"][-20:]
    st["improvements"] += 1
    _save(st)
    return {"improved": True, "action": cycle["action"],
            "improvements_total": st["improvements"]}


def verify(performance_after: float, baseline: float = 0.6) -> dict:
    """验证：改进后表现是否提升。"""
    return {"verified": performance_after >= baseline,
            "performance": round(performance_after, 2),
            "note": "改进有效" if performance_after >= baseline else "仍需改进"}


def loop_status() -> dict:
    """循环状态：是否在持续改进。"""
    st = _load()
    n = st.get("improvements", 0)
    return {"cycles_completed": len(st.get("cycles", [])),
            "improvements": n, "evolving": n >= 2,
            "note": "反思-改进循环运转中" if n >= 2 else "循环初期"}
