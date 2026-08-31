# -*- coding: utf-8 -*-
"""trinity/brain/evidence_plasticity.py — 证据门控塑性（EXECUTION 294）。

借鉴 Evidence-Gated Plasticity（2026：Context-Aware for Multi-Goal
Learning）——上下文感知 + 证据门控的可塑性（有证据支持才可塑，
无证据抑制学习——防无据改变）。

与自适应塑性（频率调节）互补：频率=快慢；本模块=证据门。
Trinity 现在：
  evidence_gate(context, evidence): 证据门控（可塑/抑制/审慎）
"""
import os
import sys
import json


def evidence_gate(context: str, evidence_sources: int = 0,
                  consistency: float = 0.5) -> dict:
    """证据门控：上下文 × 证据 → 可塑性决策。"""
    # 强证据（>=2 源 + 高一致）→ 可塑
    if evidence_sources >= 2 and consistency >= 0.7:
        return {"gate": "plastic", "evidence": evidence_sources,
                "consistency": round(consistency, 2),
                "plasticity": 0.9, "note": "强证据——允许可塑（学习）"}
    # 中等证据（1 源）→ 审慎
    if evidence_sources >= 1 and consistency >= 0.5:
        return {"gate": "cautious", "evidence": evidence_sources,
                "consistency": round(consistency, 2),
                "plasticity": 0.5, "note": "中等证据——审慎可塑"}
    # 无证据 → 抑制
    return {"gate": "inhibited", "evidence": evidence_sources,
            "consistency": round(consistency, 2),
            "plasticity": 0.1,
            "note": "无证据——抑制可塑（防无据学习）"}


def goal_plasticity(goal: str, evidence_by_goal: dict) -> dict:
    """多目标塑性：各目标按证据门控。"""
    results = {}
    for g, ev in (evidence_by_goal or {}).items():
        gate = evidence_gate(goal, ev.get("sources", 0), ev.get("consistency", 0.5))
        results[g] = gate["gate"]
    return {"goal": str(goal)[:30], "gates": results,
            "note": "多目标证据门控（各目标独立可塑性）"}
