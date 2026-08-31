# -*- coding: utf-8 -*-
"""trinity/brain/generative_memory.py — 生成式记忆（EXECUTION 266，大脑化）。

借鉴 MemGen（2026：Weaving Generative Latent Memory）——从现有
记忆主动合成新记忆（生成式表征——不只是缓存，而是生成新内容）。

与潜在记忆（缓存）互补：缓存=复用；生成=合成新。
Trinity 现在：
  synthesize(memories): 合成新记忆（组合相关记忆→新表征）
  generative_weave(topic): 编织（动态混合相关记忆→生成条目）
"""
import os
import sys
import json


def synthesize(memories: list, topic: str = "") -> dict:
    """合成新记忆：组合相关记忆 → 新表征（生成式）。"""
    if not memories:
        return {"synthesized": False, "note": "无源记忆"}
    # 提取要点（gist）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.gist_extraction import extract_gist
        g = extract_gist(memories)
        concepts = g.get("gist_concepts", [])
    except Exception:
        concepts = []
    # 合成：主题 + 要点组合 → 新表征
    sources = [str(m.get("content") or "")[:40] for m in memories[:3]]
    synthesized = {
        "content": f"[generated] 关于『{str(topic)[:25]}』的合成表征："
                   f"结合{'、'.join(concepts[:3] or sources[:2])}",
        "sources": len(memories),
        "concepts": concepts[:3],
    }
    return {"synthesized": True, **synthesized}


def generative_weave(topic: str, top_k: int = 3) -> dict:
    """生成式编织：检索相关记忆 → 合成生成条目（写入记忆）。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(topic[:30], top_k=top_k)
        items = r if isinstance(r, list) else r.get("results", [])
        if not items:
            return {"woven": False, "note": "无相关记忆"}
        syn = synthesize(items, topic)
        if not syn.get("synthesized"):
            return {"woven": False}
        # 写入生成记忆（generative 类别）
        m.ingest(syn["content"][:280], category="generative-memory",
                 tags=["generated", topic[:10]], importance=0.65,
                 wait_backfill=True)
        return {"woven": True, "content": syn["content"][:80],
                "sources": syn["sources"]}
    except Exception as e:
        return {"woven": False, "error": str(e)[:80]}
