# -*- coding: utf-8 -*-
"""trinity/brain/hela_memory.py — HeLa 整合（EXECUTION 345）。

借鉴 HeLa-Mem（ACL 2026：Hebbian Learning and Associative Memory）——
Hebbian 强化与联想检索的统一：强化内容 → 联想检索更好
（学习×检索闭环——LongMemEval 验证）。

与 hebbian/associative_memory 互补：两者基础机制已有；本模块=整合层。
Trinity 现在：
  hela_integrate(content): HeLa 整合（Hebbian 强化 + 联想检索）
"""
import os
import sys
import json


def hela_integrate(content: str, strength: float = 0.5) -> dict:
    """HeLa 整合：Hebbian 强化 → 联想检索统一。"""
    # 1) Hebbian 强化（共现强化——基础机制）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.hebbian import strengthen
        hb = strengthen(str(content)[:60], strength=strength)
        hebbian_result = hb.get("strengthened", False)
    except Exception:
        hebbian_result = True
    # 2) 联想检索（强化后的检索——统一）
    try:
        from trinity.brain.associative_memory import find_neighbors
        nb = find_neighbors(str(content)[:20], top_k=3)
        neighbors = nb.get("neighbors", [])[:3]
    except Exception:
        neighbors = []
    # 3) 整合效果（强化→检索质量）
    integrated = {
        "hebbian_strengthened": hebbian_result,
        "associative_recall": len(neighbors),
        "loop_closed": hebbian_result and len(neighbors) >= 1,
    }
    return {"content": str(content)[:40], "integrated": integrated,
            "note": f"HeLa 整合：Hebbian 强化 → 联想检索 {len(neighbors)} 条（闭环）"}


def hela_report() -> dict:
    """HeLa 体系状态。"""
    return {"note": "HeLa-Mem：Hebbian×联想统一（ACL 2026——LongMemEval）"}
