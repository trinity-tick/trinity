# -*- coding: utf-8 -*-
"""trinity/brain/observability_retention.py — 可观测性安全保留（EXECUTION 333）。

借鉴 Observability-Safe Memory Retention（2026：Constrained
Optimization）——记忆保留的约束优化：保留记忆但不能破坏
可观测性（关键信息必须可回溯——约束）。

与保留-影响分离（DREAM）互补：分离=作用；本模块=可观测性约束。
Trinity 现在：
  safe_retain(memory, observability): 安全保留（约束评估）
"""
import os
import sys
import json


def safe_retain(memory: str, observability: float = 0.7,
                critical: bool = False) -> dict:
    """安全保留：可观测性约束下的保留决策。"""
    # 约束：关键信息必须保可观测（critical → 必须保留完整）
    if critical:
        return {"retained": True, "constraint": "critical",
                "observability": round(observability, 2),
                "note": "关键信息——完整保留（可观测性必须）"}
    # 一般信息：可观测性阈值约束
    if observability < 0.3:
        return {"retained": False, "constraint": "observability",
                "observability": round(observability, 2),
                "note": "可观测性不足——不保留（防信息黑洞）"}
    # 可观测性足够 → 保留（按可观测性分级）
    if observability >= 0.7:
        level = "full_retention"
    elif observability >= 0.4:
        level = "partial_retention"
    else:
        level = "minimal_retention"
    return {"retained": True, "constraint": "observability_ok",
            "observability": round(observability, 2), "level": level,
            "note": f"可观测性 {round(observability,2)} → {level}（约束满足）"}


def retention_report() -> dict:
    """保留体系状态。"""
    return {"note": "可观测性安全保留：约束优化（Observability-Safe 2026）"}
