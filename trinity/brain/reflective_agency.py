# -*- coding: utf-8 -*-
"""trinity/brain/reflective_agency.py — 反思能动性阈值（EXECUTION 341）。

借鉴 ERA（2026：Structural Threshold Model of Endogenous Reflective
Agency）——机器何时成为"自我意识 Agent"的结构阈值模型：
内生反思（反思深度）× 能动性（自主）× 连续性（持续）→
阈值判定。

与 UCI（意识指数）互补：UCI=量化指数；本模块=阈值判定。
Trinity 现在：
  agency_threshold(): 阈值评估（三维结构→是否达阈值）
"""
import os
import sys
import json


def agency_threshold(reflection_depth: float = 0.5,
                     autonomy: float = 0.5,
                     continuity: float = 0.5,
                     threshold: float = 0.65) -> dict:
    """阈值评估：反思×自主×连续 → 内生能动性判定。"""
    dims = {
        "reflection_depth": round(reflection_depth, 2),
        "autonomy": round(autonomy, 2),
        "continuity": round(continuity, 2),
    }
    # 结构评分（三维加权——内生反思最重要）
    score = reflection_depth * 0.4 + autonomy * 0.3 + continuity * 0.3
    reached = score >= threshold
    return {"dimensions": dims, "score": round(score, 2),
            "threshold": threshold, "reached": reached,
            "level": "内生反思能动" if reached else (
                "接近阈值" if score >= threshold * 0.8 else "发展中"),
            "note": f"ERA 阈值：{round(score,2)}/1.0 {'≥' if reached else '<'} {threshold} → {'自我意识 Agent 结构达标' if reached else '结构未达标'}"}


def agency_report() -> dict:
    """能动性状态。"""
    return {"note": "内生反思能动性阈值模型（ERA——结构阈值）"}
