# -*- coding: utf-8 -*-
"""trinity/brain/evomind_governance.py — 受治理自主（EXECUTION 367）。

借鉴 EvoMind（2026：Governed Cognitive Architecture——Persistent,
Verifiable, Experience-Driven Autonomy）——受治理认知架构：
经验驱动的自主 + 治理约束（可验证——自主不失控）。

与治理（变更边界）互补：治理=修改边界；本模块=自主-治理统一。
Trinity 现在：
  governed_autonomy(action, experience): 受治理自主（评估）
"""
import os
import sys
import json


def governed_autonomy(action: str, experience: float = 0.5) -> dict:
    """受治理自主：经验驱动 + 治理约束评估。"""
    # 1) 经验驱动（经验支撑度）
    experienced = experience >= 0.6
    # 2) 治理检查（核心/风险）
    action_lower = str(action).lower()
    core_protected = any(c in action_lower for c in ("identity", "axioms", "core", "删除全部", "清空",
                                       "身份", "公理", "核心", "审计", "治理"))
    risky = any(w in action_lower for w in ("删除", "清空", "覆盖", "强制"))
    # 3) 自主决策（经验×治理）
    if core_protected:
        verdict = "rejected"
        note = "治理拒绝（核心保护）"
    elif experienced and not risky:
        verdict = "autonomous"
        note = "经验充分 + 低风险——完全自主"
    elif experienced and risky:
        verdict = "supervised"
        note = "经验充分但高风险——监督执行"
    elif not experienced:
        verdict = "deferred"
        note = "经验不足——推迟（先学习）"
    return {"action": str(action)[:30], "experience": round(experience, 2),
            "verdict": verdict, "note": note,
            "governed": True,
            "verifiable": verdict in ("autonomous", "supervised")}
