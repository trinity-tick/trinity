# -*- coding: utf-8 -*-
"""trinity/brain/reflective_context.py — 反思上下文（EXECUTION 349）。

借鉴 ARC（2026：Active and Reflection-driven Context Management）——
主动+反思驱动的上下文管理：反思 → 上下文调整决策（长时程
信息寻求）。

与上下文塑形（主动选择）互补：塑形=选择；本模块=反思驱动。
Trinity 现在：
  manage_context(reflection): 反思驱动上下文（调整决策）
"""
import os
import sys
import json


def manage_context(reflection: str) -> dict:
    """反思驱动上下文：反思发现 → 上下文管理调整。"""
    r = str(reflection)
    actions = []
    # 反思发现 → 上下文调整
    if any(w in r for w in ("信息不足", "缺少", "缺失")):
        actions.append({"action": "expand_context", "note": "信息不足——扩展上下文"})
    if any(w in r for w in ("冗余", "重复", "多余")):
        actions.append({"action": "prune_context", "note": "冗余——修剪上下文"})
    if any(w in r for w in ("过时", "陈旧", "旧")):
        actions.append({"action": "refresh_context", "note": "过时——刷新上下文"})
    if any(w in r for w in ("混乱", "无组织", "不连贯")):
        actions.append({"action": "reorganize", "note": "混乱——重组上下文"})
    if not actions:
        actions.append({"action": "maintain", "note": "上下文健康——保持"})
    return {"reflection": r[:40], "actions": actions,
            "driven": len(actions) >= 1,
            "note": f"反思驱动上下文：{'、'.join(a['action'] for a in actions)}（ARC）"}


def context_report() -> dict:
    """上下文管理状态。"""
    return {"note": "ARC：主动反思驱动上下文管理（长时程信息寻求）"}
