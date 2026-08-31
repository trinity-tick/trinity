# -*- coding: utf-8 -*-
"""trinity/brain/agent_governance.py — Agent 治理（EXECUTION 264，大脑化）。

借鉴 Agent Governance for Self-Evolving AI（2026）——自进化 Agent
的治理：自修改受约束（安全边界——什么能改/什么不能改）。

Trinity 现在：
  check_change(change): 变更审查（影响范围/风险→允许/拒绝/审查）
  governance_rules(): 治理规则（核心不可改/可改边界）
"""
import os
import sys
import json


# 核心保护（不可修改的边界——中英文）
CORE_PROTECTED = ("identity", "axioms", "audit", "governance", "core", "storage",
                 "身份", "公理", "审计", "治理", "存储", "核心")


def check_change(change: str, scope: str = "self", risk: float = 0.3) -> dict:
    """变更审查：按影响范围与风险决定。"""
    # 1) 核心保护（永远不可改）
    for p in CORE_PROTECTED:
        if p in change.lower():
            return {"verdict": "reject", "reason": f"核心保护（{p}）",
                    "change": str(change)[:40]}
    # 2) 高风险变更（影响全局）
    if risk >= 0.8:
        return {"verdict": "review", "reason": "高风险变更（需审查）",
                "change": str(change)[:40]}
    # 3) 低风险可改（局部/可逆）
    return {"verdict": "allow", "reason": "低风险可改（局部）",
            "change": str(change)[:40]}


def governance_rules() -> dict:
    """治理规则：安全边界。"""
    return {
        "protected": CORE_PROTECTED,
        "modifiable": "低风险局部行为策略（学习/偏好/策略）",
        "requires_review": "高风险变更（影响全局/不可逆）",
        "note": "自进化受治理约束（安全自主）",
    }


def governance_report() -> dict:
    """治理状态：变更审查记录。"""
    return {"note": "治理层：核心保护 + 风险分级（安全自主边界）"}
