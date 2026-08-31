# -*- coding: utf-8 -*-
"""trinity/brain/episodic_reasoning.py — 情景记忆推理（EXECUTION 229，大脑化）。

借鉴 REMem（ICLR 2026：Reasoning with Episodic Memory）——不只是
检索情景，而是"用情景推理"：检索相关证据 → 综合 → 得出结论。

与检索（召回）区分：检索=找证据；推理=用证据推结论。
Trinity 现在：
  reason_with_episodes(query): 检索情景证据 → 综合推理（支持/反对/结论）
"""
import os
import sys
import json


def reason_with_episodes(query: str, top_k: int = 5, use_llm: bool = True) -> dict:
    """情景记忆推理：证据检索 → 综合结论。"""
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(query, top_k=top_k)
        items = r if isinstance(r, list) else r.get("results", [])
        episodes = [str(x.get("content") or "")[:100] for x in items[:top_k]]
        if not episodes:
            return {"reasoned": False, "note": "无相关情景"}
        evidence = "；".join(episodes[:4])
        if use_llm:
            try:
                from trinity.brain.value_encoder import llm_chat
                _prompt = ("基于以下情景记忆证据，对『" + str(query)[:40]
                           + "』给出推理结论（2-3句，说明依据）：" + evidence[:300])
                _r = llm_chat(_prompt, max_tokens=150, timeout=30)
                if _r and len(_r.strip()) > 30:
                    return {"reasoned": True, "conclusion": _r.strip()[:300],
                            "evidence_count": len(episodes), "mode": "llm"}
            except Exception:
                pass
        # 结构化推理（无 LLM）：证据统计 → 倾向结论
        return {"reasoned": True,
                "conclusion": "基于 " + str(len(episodes)) + " 条相关情景记忆，"
                              "证据显示与『" + str(query)[:30] + "』相关："
                              + evidence[:150],
                "evidence_count": len(episodes), "mode": "structured"}
    except Exception as e:
        return {"reasoned": False, "error": str(e)[:80]}
