# -*- coding: utf-8 -*-
"""trinity/brain/entropic_memory.py — 熵记忆（EXECUTION 342）。

借鉴 Entropic Memory（ICLR 2026：Thermodynamics-Inspired
Consolidation）——热力学启发巩固：记忆的熵管理（混乱记忆
→ 有序化巩固——终身学习）。

与睡眠巩固（阶段）互补：睡眠=阶段；本模块=熵视角。
Trinity 现在：
  entropic_consolidate(): 熵巩固（混乱度→有序化）
"""
import os
import sys
import json
import math


def _entropy(categories: dict) -> float:
    """熵：类别分布混乱度（0 有序 1 混乱）。"""
    total = sum(categories.values())
    if total == 0:
        return 0.0
    h = 0.0
    for c in categories.values():
        p = c / total
        if p > 0:
            h -= p * math.log2(p)
    # 归一化（log2(类别数)）
    max_h = math.log2(max(len(categories), 2))
    return h / max_h if max_h > 0 else 0.0


def entropic_consolidate() -> dict:
    """熵巩固：记忆混乱度评估 → 有序化建议。"""
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        # 类别分布
        cur.execute("SELECT category, count(*) FROM memories WHERE status='active' GROUP BY category")
        dist = {r[0]: r[1] for r in cur.fetchall()}
        # 混乱度（低重要无分类记忆 = 熵源）
        cur.execute("SELECT count(*) FROM memories WHERE status='active' AND category='uncategorized'")
        uncategorized = cur.fetchone()[0]
        conn.close()
        entropy = _entropy(dist)
        # 有序化建议（热力学：降熵）
        if entropy > 0.6 or uncategorized > 50:
            action = "consolidate"
            note = f"熵 {round(entropy,2)} 高——需要有序化（分类/合并）"
        elif entropy > 0.4:
            action = "tidy"
            note = f"熵 {round(entropy,2)} 中——轻度整理"
        else:
            action = "stable"
            note = f"熵 {round(entropy,2)} 低——记忆有序"
        return {"entropy": round(entropy, 2), "categories": len(dist),
                "uncategorized": uncategorized, "action": action, "note": note}
    except Exception as e:
        return {"error": str(e)[:80]}


def entropic_report() -> dict:
    """熵体系状态。"""
    return {"note": "熵记忆：热力学巩固（混乱→有序——终身学习）"}
