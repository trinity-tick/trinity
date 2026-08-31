# -*- coding: utf-8 -*-
"""trinity/brain/event_logic_map.py — 事件逻辑图（EXECUTION 348）。

借鉴 Event-Centric Memory as a Logic Map（ACL 2026：Findings——
Searching and Reasoning）——事件作为逻辑地图：事件连接成
逻辑图（因果/时序→搜索与推理导航）。

与叙事记忆（连贯故事）互补：叙事=故事；本模块=逻辑图。
Trinity 现在：
  build_map(events): 事件逻辑图（连接→导航）
"""
import os
import sys
import json


def build_map(events: list) -> dict:
    """事件逻辑图：事件连接成逻辑导航图。"""
    # 事件节点
    nodes = []
    for i, e in enumerate(events[:10]):
        content = str(e.get("content") or f"事件{i}")
        nodes.append({"id": i, "label": content[:25],
                      "type": e.get("type", "event")})
    # 逻辑边（因果/时序——按序连接）
    edges = []
    for i in range(len(nodes) - 1):
        edges.append({"from": i, "to": i + 1, "relation": "sequence"})
    # 因果边（含因果词的）
    for i, n in enumerate(nodes):
        if any(w in n["label"] for w in ("导致", "因为", "所以", "触发")):
            if i > 0:
                edges.append({"from": i - 1, "to": i, "relation": "causal"})
    return {"nodes": len(nodes), "edges": len(edges),
            "relations": {"sequence": sum(1 for e in edges if e["relation"] == "sequence"),
                          "causal": sum(1 for e in edges if e["relation"] == "causal")},
            "navigable": len(edges) >= 2,
            "note": f"事件逻辑图：{len(nodes)} 节点 {len(edges)} 边（搜索/推理导航）"}


def map_report() -> dict:
    """逻辑图状态。"""
    return {"note": "事件逻辑地图：搜索与推理导航（Event-Centric 2026）"}
