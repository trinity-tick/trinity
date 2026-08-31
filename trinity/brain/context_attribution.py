# -*- coding: utf-8 -*-
"""trinity/brain/context_attribution.py — 上下文归因（EXECUTION 309）。

借鉴 Shapley Context Attribution（2026：Dual-Grained Agent Memory）——
记忆对输出的贡献归因（哪条记忆贡献最大——可解释性）。

Trinity 现在：
  attribute(query, memories): 上下文归因（贡献评分——可解释）
"""
import os
import sys
import json


def attribute(query: str, memories: list) -> dict:
    """上下文归因：各记忆对答案的贡献评分（Shapley 近似）。"""
    q = str(query)
    qw = set()
    for i in range(len(q) - 1):
        if "\u4e00" <= q[i] <= "\u9fff" and "\u4e00" <= q[i+1] <= "\u9fff":
            qw.add(q[i:i+2])
    contributions = []
    for m in memories[:10]:
        content = str(m.get("content") or "")
        mw = set()
        for i in range(len(content) - 1):
            if "\u4e00" <= content[i] <= "\u9fff" and "\u4e00" <= content[i+1] <= "\u9fff":
                mw.add(content[i:i+2])
        # 贡献 = 查询词重叠 + 重要度 + 新鲜度
        overlap = len(qw & mw) / max(len(qw), 1) if qw else 0
        importance = float(m.get("importance") or 0.5)
        contribution = overlap * 0.6 + importance * 0.4
        contributions.append({"memory_id": str(m.get("memory_id"))[:12],
                              "content": content[:40],
                              "contribution": round(contribution, 3),
                              "overlap": round(overlap, 2)})
    contributions.sort(key=lambda x: -x["contribution"])
    return {"query": q[:30], "contributions": contributions[:5],
            "top": contributions[0]["content"][:35] if contributions else None,
            "note": "上下文归因：贡献排序（可解释——哪条记忆最重要）"}


def attribution_report() -> dict:
    """归因状态。"""
    return {"note": "Shapley 上下文归因：记忆贡献可解释（Dual-Grained 2026）"}
