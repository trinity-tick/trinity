# -*- coding: utf-8 -*-
"""trinity/brain/gist_extraction.py — 要点蒸馏（EXECUTION 244，大脑化）。

借鉴 Verbatim to Gist（2026：Distilling Pyramidal Memory via Semantic
Information Bottleneck）——从记忆细节提炼要点（逐字→要点→核心），
语义记忆的形成（细节会忘，要点长存）。

Trinity 现在：
  extract_gist(memories): 多记忆 → 要点（高频概念+共同主题）
  pyramid(): 金字塔层级（细节→要点→核心）
"""
import os
import sys
import json


def extract_gist(memories: list, top_n: int = 5) -> dict:
    """从记忆提炼要点：高频词 + 共同主题。"""
    from collections import Counter
    words = Counter()
    topics = []
    for m in memories[:20]:
        t = str(m.get("content") or "")
        topics.append(t[:60])
        # 2 字词窗口
        for i in range(len(t) - 1):
            if "\u4e00" <= t[i] <= "\u9fff" and "\u4e00" <= t[i+1] <= "\u9fff":
                words[t[i:i+2]] += 1
    # 要点 = 高频概念（>=2 次）过滤停用词
    stop = {"系统", "状态", "我的", "我们", "进行", "可以", "需要", "一个", "这个", "相关"}
    gist = [w for w, c in words.most_common(15) if c >= 2 and w not in stop][:top_n]
    return {"gist_concepts": gist, "source_memories": len(memories),
            "coverage": len(gist)}


def pyramid(memories: list) -> dict:
    """金字塔蒸馏：细节 → 要点 → 核心。"""
    details = [str(m.get("content") or "")[:50] for m in memories[:5]]
    gist = extract_gist(memories)
    core = gist["gist_concepts"][:2]  # 核心 = 最强要点
    return {"level_detail": details,
            "level_gist": gist["gist_concepts"],
            "level_core": core,
            "pyramid": f"细节({len(details)}条) → 要点({len(gist['gist_concepts'])}个) → 核心({len(core)}个)"}
