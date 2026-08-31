# -*- coding: utf-8 -*-
"""trinity/brain/compression_spectrum.py — 经验压缩谱（EXECUTION 293）。

借鉴 Experience Compression Spectrum（2026：Unifying Memory, Skills,
and Rules）——记忆→技能→规则的渐进压缩谱（高频使用经验逐步
压缩：原始记忆 → 技能 → 规则）。

Trinity 现在：
  compress_experience(memory, usage): 按使用频率压缩（原始→技能→规则）
"""
import os
import sys
import json


def compress_experience(memory: str, usage: int = 0) -> dict:
    """经验压缩：使用频率驱动压缩档位。"""
    # 档位：0-2 次=记忆（原始）；3-9 次=技能（提炼动作）；
    # 10+ 次=规则（泛化法则）
    if usage >= 10:
        level = "rule"
        compressed = f"规则：{str(memory)[:40]}（高频泛化）"
        note = "已压缩为规则（高频经验→法则）"
    elif usage >= 3:
        level = "skill"
        compressed = f"技能：{str(memory)[:40]}（动作提炼）"
        note = "已压缩为技能（常用经验→动作）"
    else:
        level = "memory"
        compressed = str(memory)[:60]
        note = "保持原始记忆（低频——不压缩）"
    return {"memory": str(memory)[:40], "usage": usage, "level": level,
            "compressed": compressed[:70], "note": note,
            "spectrum": "记忆 → 技能 → 规则（渐进压缩）"}


def spectrum_overview() -> dict:
    """压缩谱概览（各档位分布）。"""
    return {"levels": ["memory", "skill", "rule"],
            "note": "经验压缩谱：记忆/技能/规则统一（Experience Compression）"}
