# -*- coding: utf-8 -*-
"""trinity/brain/sovereign_layers.py — 主权分层（EXECUTION 352）。

借鉴 OmegA（2026：Layered Architecture for Sovereign Cognitive
Agents）——分层主权架构：认知分层 + 自主权（各层自治——
感知/记忆/认知/决策/行动各有主权）。

与 7 层记忆（神经视图）互补：7 层=记忆组织；本模块=主权分层。
Trinity 现在：
  sovereign_layer(): 主权分层（各层状态+自治权）
"""
import os
import sys
import json


LAYERS = [
    ("perception", "感知层", ["perception", "attention_control"]),
    ("memory", "记忆层", ["memory_manager", "associative_memory"]),
    ("cognition", "认知层", ["cognition_pipeline", "executive_function"]),
    ("emotion", "情绪层", ["affect", "emotion_regulation"]),
    ("decision", "决策层", ["fast_slow_decision", "multi_perspective"]),
    ("action", "行动层", ["action_loop", "proactive_initiative"]),
]


def sovereign_layer() -> dict:
    """主权分层：各认知层状态 + 自治权。"""
    layers = {}
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        for key, name, mods in LAYERS:
            # 层活跃度（机制文件存在=层就绪）
            active = sum(1 for m in mods if os.path.exists(os.path.join(os.path.dirname(__file__), m + ".py")))
            # 自治权（机制就绪+独立功能=有主权）
            sovereign = active >= 1
            layers[key] = {"name": name, "active": active, "sovereign": sovereign}
        conn.close()
    except Exception:
        pass
    sovereign_count = sum(1 for v in layers.values() if v.get("sovereign"))
    return {"layers": layers, "sovereign": f"{sovereign_count}/{len(LAYERS)}",
            "note": f"主权分层：{sovereign_count}/6 层自治（OmegA——主权认知）"}


def sovereign_report() -> dict:
    """主权体系状态。"""
    return {"note": "OmegA：分层主权认知架构（各层自治）"}
