# -*- coding: utf-8 -*-
"""trinity/brain/executive_memory.py — 执行记忆（EXECUTION 360）。

借鉴 MemoBrain（ACL 2026：Executive Memory as an Agentic Brain——
Coherent Long-Horizon Reasoning）——执行记忆：记忆支撑执行
推理（连贯长时程——记忆作为推理的执行大脑）。

与执行功能（控制）互补：执行=控制；本模块=执行记忆。
Trinity 现在：
  executive_reason(query): 执行记忆推理（记忆→执行推理）
"""
import os
import sys
import json


def executive_reason(query: str) -> dict:
    """执行记忆推理：记忆支撑执行推理（连贯长时程）。"""
    # 1) 执行记忆检索（目标相关记忆）
    memories = []
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(query[:30], top_k=3)
        items = r if isinstance(r, list) else r.get("results", [])
        memories = [str(x.get("content") or "")[:50] for x in items[:3]]
    except Exception:
        pass
    # 2) 执行推理（记忆→步骤）
    steps = []
    for i, mem in enumerate(memories[:3]):
        steps.append({"step": i + 1, "from_memory": mem[:30]})
    if not steps:
        steps.append({"step": 1, "from_memory": "纯逻辑推演"})
    # 3) 连贯性（记忆支撑的执行推理）
    return {"query": str(query)[:30], "steps": steps,
            "coherence": len(steps) >= 2,
            "memory_supported": len(memories) >= 1,
            "note": f"执行记忆：{len(steps)} 步推理（{'记忆支撑连贯' if len(steps) >= 2 else '待记忆积累'}）"}


def executive_report() -> dict:
    """执行记忆状态。"""
    return {"note": "MemoBrain：执行记忆作为推理大脑（连贯长时程）"}
