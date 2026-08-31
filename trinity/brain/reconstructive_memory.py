# -*- coding: utf-8 -*-
"""trinity/brain/reconstructive_memory.py — 记忆重构（EXECUTION 204，大脑化）。

重构记忆（reconstructive memory，Bartlett）：大脑回忆不是精确回放，
而是按当前情境"重构"记忆（每次回忆都略变）。Trinity 现在：
  - 检索后 → 按查询重构为连贯回忆摘要（LLM，降级结构化）
  - 重构写入（reconstructive 类别）——"回忆的综合"随时间演进

与精确检索互补：检索=取回；重构=再创（情境化综合）。
"""
import os
import sys


def reconstruct(query: str, results: list, use_llm: bool = True) -> dict:
    """检索结果 → 重构回忆（连贯摘要）。"""
    if not results:
        return {"reconstructed": False, "note": "no results"}
    # 结构化重构（基础）：组合要点
    items = []
    for r in results[:5]:
        c = str(r.get("content") or "")[:80]
        if c and c not in items:
            items.append(c)
    base = "；".join(items[:4])
    if use_llm:
        try:
            sys.path.insert(0, r"D:\\trinity-code")
            from trinity.brain.value_encoder import llm_chat
            _prompt = ("基于以下检索记忆，用2-3句话连贯地回忆关于『" + str(query)[:30]
                       + "』的内容（重构式回忆）：" + base[:300])
            _r = llm_chat(_prompt, max_tokens=120, timeout=30)
            if _r and len(_r.strip()) > 20:
                return {"reconstructed": True, "recall": _r.strip()[:250],
                        "mode": "llm", "sources": len(items)}
        except Exception:
            pass
    return {"reconstructed": True, "recall": "关于『" + str(query)[:30] + "』的回忆：" + base[:250],
            "mode": "structured", "sources": len(items)}


def reconstruct_to_memory(query: str, results: list) -> bool:
    """重构回忆写入（reconstructive 类别——随时间演进）。"""
    try:
        r = reconstruct(query, results)
        if not r.get("reconstructed"):
            return False
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        m.ingest("[reconstructive] " + r["recall"][:280], category="reconstructive",
                 tags=["recall", "reconstruct"], importance=0.7, wait_backfill=True)
        return True
    except Exception:
        return False
