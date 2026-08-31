# -*- coding: utf-8 -*-
"""trinity/brain/joint_exploration.py — 记忆-探索联合（EXECUTION 332）。

借鉴 Joint Agent Memory and Exploration Learning（2026：Novelty
Signals）——新颖信号联合驱动记忆与探索（同一信号同时：
写入记忆 + 驱动探索——联合优化）。

Trinity 现在：
  joint_learn(signal, novelty): 联合学习（记忆写入+探索驱动）
"""
import os
import sys
import json


def joint_learn(signal: str, novelty: float = 0.5) -> dict:
    """联合学习：新颖信号 → 记忆 + 探索 双驱动。"""
    # 1) 记忆通道（新颖高 → 写入强化）
    memory_action = "write_strong" if novelty >= 0.7 else (
        "write" if novelty >= 0.4 else "skip")
    # 2) 探索通道（新颖高 → 探索驱动）
    explore_drive = novelty * 0.8
    # 3) 联合效应
    joint = novelty >= 0.5
    return {"signal": str(signal)[:40], "novelty": round(novelty, 2),
            "memory": memory_action, "explore_drive": round(explore_drive, 2),
            "joint": joint,
            "note": f"联合学习：新颖 {round(novelty,2)} → 记忆{memory_action} + 探索 {round(explore_drive,2)}"}


def joint_report() -> dict:
    """联合学习状态。"""
    return {"note": "记忆-探索联合：新颖信号双驱动（Joint Learning 2026）"}
