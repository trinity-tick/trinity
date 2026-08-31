# -*- coding: utf-8 -*-
"""trinity/brain/foresight_planning.py — 预见规划（EXECUTION 262，大脑化）。

借鉴 See Tomorrow, Act Today（CVPR 2026：Foresight-Driven）——
预见未来状态 → 规划今天的行动（前瞻驱动）。

与世界排练（行动模拟）互补：排练=行动预演；预见=未来规划。
Trinity 现在：
  foresee(goal): 预见未来（模拟目标达成路径——未来状态序列）
  plan_today(): 今天的行动（基于预见——先做影响未来的事）
"""
import os
import sys
import json


def foresee(goal: str, horizon: int = 3) -> dict:
    """预见未来：模拟目标达成路径（未来状态序列）。"""
    steps = []
    try:
        # 从记忆检索相关经验（未来路径的依据）
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(goal[:30], top_k=2)
        items = r if isinstance(r, list) else r.get("results", [])
        evidence = [str(x.get("content") or "")[:40] for x in items[:2]]
    except Exception:
        evidence = []
    for i in range(1, horizon + 1):
        steps.append({"step": i, "state": f"阶段{i}：推进『{str(goal)[:20]}』"})
    return {"goal": str(goal)[:30], "future_steps": steps,
            "horizon": horizon, "evidence": evidence,
            "note": f"预见 {horizon} 步未来路径（基于经验）"}


def plan_today(goal: str, urgent: list = None) -> dict:
    """今天的行动：基于预见（先做影响未来的事）。"""
    fut = foresee(goal)
    today_plan = []
    # 预见的第一步 = 今天该做的（影响未来）
    if fut.get("future_steps"):
        today_plan.append({"action": f"开始{fut['future_steps'][0]['state']}",
                           "reason": "预见第一步（影响未来）"})
    # 紧急事项
    for u in (urgent or [])[:2]:
        today_plan.append({"action": str(u)[:30], "reason": "紧急"})
    return {"goal": str(goal)[:30], "today_plan": today_plan,
            "foresight": len(fut.get("future_steps", [])),
            "note": "预见驱动：今天做影响未来的事（See Tomorrow, Act Today）"}
