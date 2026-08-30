#!/usr/bin/env python3
"""_routers_recall.py — 重建式回忆端点（2026-09，EXECUTION 105）

大脑：回忆是【重建】而非取档。对标 R3Mem（可逆压缩回忆）与
GEM-RAG（图特征记忆）思想：检索 top-k 记忆片段 → LLM 重建为连贯的
"回忆"（时间锚定、整合、信心、模糊部分标注）。

失败降级：LLM 不可用时返回片段聚合摘要（confidence=0.3），不 500。
"""

import time
from collections import OrderedDict
from typing import Optional

from fastapi import APIRouter, Body

from ._deps import _live_memory as get_memory
from trinity.brain.value_encoder import recall_reconstruct

router = APIRouter()

# 2026-09（EXECUTION 105.10）回忆语义缓存：LRU 64 条 + TTL 600s——
# 重复/相似回忆不重复付 LLM 成本（记忆未变时回忆稳定）。
_RECALL_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_RECALL_CACHE_TTL = 600.0
_RECALL_CACHE_MAX = 64


def _cache_get(key: str):
    item = _RECALL_CACHE.get(key)
    if item is None:
        return None
    if time.time() - item["ts"] > _RECALL_CACHE_TTL:
        _RECALL_CACHE.pop(key, None)
        return None
    _RECALL_CACHE.move_to_end(key)
    return item["value"]


def _cache_put(key: str, value: dict) -> None:
    _RECALL_CACHE[key] = {"ts": time.time(), "value": value}
    _RECALL_CACHE.move_to_end(key)
    while len(_RECALL_CACHE) > _RECALL_CACHE_MAX:
        _RECALL_CACHE.popitem(last=False)


@router.post("/memory/recall")
def memory_recall(  # sync def：FastAPI 线程池调度，避免 LLM 同步调用阻塞事件循环
    query: str = Body(...),
    top_k: int = Body(8, ge=1, le=20),
    mode: str = Body("reconstruct"),
    include_sources: bool = Body(True),
):
    """重建式回忆：检索 + LLM 重建为连贯叙述。

    mode: reconstruct（默认，LLM 重建）/ condense（同路径，仅换措辞意图）。
    """
    t0 = time.time()
    mem = get_memory()
    data = mem.search_hybrid(query=query, top_k=top_k, strategy="rrf")
    results = data.get("results", []) if isinstance(data, dict) else data
    sources = []
    for r in results:
        mid = r.get("memory_id")
        c = r.get("content_preview") or r.get("content") or ""
        created = r.get("created_at")
        sources.append({
            "memory_id": mid,
            "content": str(c)[:300],
            "created_at": str(created)[:10] if created else "",
        })
    if not sources:
        return {
            "query": query, "recall": "（没有检索到相关记忆）",
            "confidence": 0.0, "mode": mode, "sources": [],
            "latency_s": round(time.time() - t0, 2),
        }
    # 2026-09（EXECUTION 105.14）：可逆压缩集成——recall mode="decompress"
    # 时，若来源记忆带压缩线索（compression_hints），用线索引导还原
    # （R3Mem 式解压回忆），而非仅原文片段重建。
    decompress_mode = mode == "decompress"
    if decompress_mode:
        for s in sources:
            try:
                import psycopg2 as _pg
                _conn = _pg.connect(host="127.0.0.1", port=5432,
                                    dbname="trinity", user="trinity",
                                    password="trinity")
                _cur = _conn.cursor()
                _cur.execute(
                    "SELECT metadata->>'compression_summary', "
                    "metadata->'compression_hints' FROM memories WHERE memory_id=%s",
                    (s.get("memory_id"),))
                row = _cur.fetchone()
                _conn.close()
                if row and row[0]:
                    s["_hints"] = {
                        "summary": row[0],
                        "hints": row[1] if isinstance(row[1], list) else [],
                    }
            except Exception:
                pass
    # 回忆缓存命中则跳过 LLM（key=query|top_k|mode + 来源 id 指纹）
    _sig = "|".join(str(s.get("memory_id", "")) for s in sources[:8])
    _key = query + "|" + str(top_k) + "|" + mode + "|" + _sig
    cached = _cache_get(_key)
    if cached is not None:
        cached["cached"] = True
        cached["latency_s"] = round(time.time() - t0, 2)
        return cached
    # LLM 重建（失败降级为聚合摘要）；decompress 模式带压缩线索引导
    if decompress_mode:
        from trinity.brain.compression import decompress as _decomp
        _hinted = [s for s in sources if s.get("_hints")]
        if _hinted:
            raw = _decomp({"summary": _hinted[0]["_hints"]["summary"],
                           "hints": _hinted[0]["_hints"]["hints"]})
            if raw:
                recall = raw.strip()
                confidence = 0.7
        else:
            raw = recall_reconstruct(query, sources, top_k=top_k)
    else:
        raw = recall_reconstruct(query, sources, top_k=top_k)
    if raw:
        recall = raw.strip()
        confidence = 0.7
    else:
        lines = []
        for s in sources[:5]:
            lines.append("- (" + (s["created_at"] or "?") + ") " + s["content"][:120])
        recall = "\n".join(lines)
        confidence = 0.3
    payload = {
        "query": query,
        "recall": recall,
        "confidence": confidence,
        "mode": mode,
        "sources": sources if include_sources else [s["memory_id"] for s in sources],
        "cached": False,
        "latency_s": round(time.time() - t0, 2),
    }
    _cache_put(_key, payload)
    return payload
