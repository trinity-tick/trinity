# -*- coding: utf-8 -*-
"""trinity/brain/hybrid_memory.py — 混合记忆编织（EXECUTION 291，大脑化）。

借鉴 MemWeaver（2026：Weaving Hybrid Memories for Traceable
Long-Horizon Reasoning）——图+向量混合记忆的统一访问
（多通道编织——可追踪长时程推理）。

与 RRF 融合（结果融合）互补：RRF=排序融合；本模块=访问统一。
Trinity 现在：
  weave(query): 混合检索（向量 + 图谱 + 关联 多通道编织）
"""
import os
import sys
import json


def weave(query: str, top_k: int = 3) -> dict:
    """混合检索：多通道编织（向量语义 + 图谱关联 + 最近记录）。"""
    channels = {}
    # 1) 向量语义（混合搜索）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(query[:40], top_k=top_k)
        items = r if isinstance(r, list) else r.get("results", [])
        channels["vector"] = [str(x.get("content") or "")[:50] for x in items[:top_k]]
    except Exception:
        channels["vector"] = []
    # 2) 图谱关联（联想/邻近）
    try:
        from trinity.brain.associative_memory import find_neighbors
        nb = find_neighbors(query[:20], top_k=top_k)
        channels["graph"] = [str(x)[:50] for x in (nb.get("neighbors", []) or [])[:top_k]]
    except Exception:
        channels["graph"] = []
    # 3) 最近记录（时间通道）
    try:
        import psycopg2
        conn = psycopg2.connect(host="127.0.0.1", port=5432, dbname="trinity",
                                user="trinity", password="trinity", connect_timeout=10)
        cur = conn.cursor()
        cur.execute("SELECT left(content, 50) FROM memories WHERE status='active' AND content ILIKE %s ORDER BY created_at DESC LIMIT %s", (f"%{query[:10]}%", top_k))
        channels["recent"] = [r[0] for r in cur.fetchall()]
        conn.close()
    except Exception:
        channels["recent"] = []
    # 编织汇总
    woven = []
    for ch in ("vector", "graph", "recent"):
        for item in channels[ch]:
            if item and item not in woven:
                woven.append(item)
    return {"channels": {k: len(v) for k, v in channels.items()},
            "woven": woven[:5], "count": len(woven),
            "note": "混合记忆编织（向量+图谱+时间——多通道）"}
