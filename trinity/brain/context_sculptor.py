# -*- coding: utf-8 -*-
"""trinity/brain/context_sculptor.py — 主动上下文管理（EXECUTION 241，大脑化）。

借鉴 Sculptor（ICLR 2026：Active Context Management）——认知代理
主动塑造上下文（不是被动塞入）：按相关×价值×新颖选择，修剪冗余。

与 token_budget（预算上限）互补：预算=限制；塑形=选择。
Trinity 现在：
  sculpt(results, query, budget): 上下文塑形（选择+修剪）
  context_report(): 上下文构成分析
"""
import os
import sys
import json


def sculpt(results: list, query: str = "", budget: int = 5,
           min_score: float = 0.3) -> dict:
    """上下文塑形：按 相关×价值 评分选择，修剪冗余。"""
    scored = []
    for r in results:
        content = str(r.get("content") or "")
        # 相关（查询词命中）
        rel = 0.5
        if query:
            qw = set(str(query)[:20])
            rel = 0.7 if any(w in content[:100] for w in query.split()[:3]) else 0.4
        # 价值（importance + 新颖——避免重复）
        importance = float(r.get("importance") or 0.5)
        score = rel * 0.5 + importance * 0.5
        scored.append({"memory_id": r.get("memory_id"), "content": content[:60],
                       "score": round(score, 2), "importance": importance,
                       "relevance": rel})
    scored.sort(key=lambda x: x["score"], reverse=True)
    selected = [s for s in scored if s["score"] >= min_score][:budget]
    trimmed = [s for s in scored if s not in selected]
    return {"selected": selected, "trimmed_count": len(trimmed),
            "budget": budget,
            "context_size": sum(len(s["content"]) for s in selected)}


def context_report(results: list) -> dict:
    """上下文构成分析。"""
    if not results:
        return {"note": "空上下文"}
    import re
    cats = {}
    total_len = 0
    for r in results:
        c = str(r.get("content") or "")
        total_len += len(c)
        cat = str(r.get("category") or "unknown")
        cats[cat] = cats.get(cat, 0) + 1
    return {"items": len(results), "total_chars": total_len,
            "composition": cats,
            "avg_len": round(total_len / max(len(results), 1))}
