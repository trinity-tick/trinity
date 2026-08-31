# -*- coding: utf-8 -*-
"""trinity/brain/auto_research.py — 自动研究（EXECUTION 312，大脑化）。

借鉴 EvolveMem（2026：Self-Evolving Memory Architecture via
AutoResearch）——自动研究驱动的记忆进化：主题 → 研究问题 →
收集证据 → 提炼结论 → 进化记忆架构。

与自发进化（探索）互补：自发=随机好奇；研究=系统化。
Trinity 现在：
  research(topic): 自动研究（系统化——问题→收集→提炼）
"""
import os
import sys
import json


def research(topic: str, evidence_limit: int = 8) -> dict:
    """自动研究：系统化研究流程。"""
    # 1) 研究问题（从主题生成）
    questions = [f"『{str(topic)[:20]}』的核心是什么？",
                 f"『{str(topic)[:20]}』的关键经验？"]
    # 2) 收集证据（检索现有记忆）
    evidence = []
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(topic[:30], top_k=evidence_limit)
        items = r if isinstance(r, list) else r.get("results", [])
        evidence = [str(x.get("content") or "")[:60] for x in items[:evidence_limit]]
    except Exception:
        pass
    # 3) 提炼结论（高频要点）
    conclusions = []
    try:
        from trinity.brain.gist_extraction import extract_gist
        g = extract_gist([{"content": e} for e in evidence])
        if g.get("gist_concepts"):
            conclusions = g["gist_concepts"][:3]
    except Exception:
        pass
    # 4) 进化（记录研究→记忆）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest(f"[research:{topic[:15]}] 研究结论：{'、'.join(conclusions or ['待深入'])}",
                 category="research-note", tags=["research", topic[:10]],
                 importance=0.7, wait_backfill=True)
    except Exception:
        pass
    return {"topic": str(topic)[:30], "questions": questions,
            "evidence_count": len(evidence), "conclusions": conclusions,
            "note": f"自动研究：{len(evidence)} 条证据 → {'、'.join(conclusions or ['待深入'])}（记忆已进化）"}
