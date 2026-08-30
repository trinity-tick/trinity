#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trinity REST API Server — 召回可解释端点 (white-box debug).
============================================================

GET /memory/search/explain?q=...&top_k=5&strategy=rrf

复用现有 engine 的 hybrid 检索 (_routers_search.py 同款调用方式：
    mem.search_hybrid(query=..., top_k=..., strategy=...)
)，对每条命中做白盒分数分解:

  - keyword_score  来自 BM25 关键词通道 (hybrid_retriever 的 ``bm25_score``)。
  - vector_score   来自 向量/FTS 语义通道 (``vector_score``)。
  - rerank_factor  来自图谱 1-hop 扩展 / 聚合池 / 过程性 skill 的重排补充
                   通道 (``graph_score`` / ``aggregator_score`` /
                   ``procedural_score``)。图谱通道本质是"实体重排 + 邻居扩充"，
                   故归入重排因子。
  - final_score    融合后的终分 (``hybrid_score``)。
  - channels_hit   实际命中的通道列表。

对读不到的分量标注 ``null``，并在 ``decompose_scores`` 内注明该通道为
``merged``（融合阶段并入，无法单独还原）。所有分解逻辑在纯函数
``decompose_scores`` 中，便于单元测试（不连真实服务）。
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, HTTPException

from ._deps import _live_memory as get_memory

logger = logging.getLogger(__name__)

router = APIRouter()

# 白盒分解的可用通道（对齐 hybrid_retriever.HybridRetriever 实际字段）。
KEYWORD_KEY = "bm25_score"      # BM25 关键词通道
VECTOR_KEY = "vector_score"     # 向量 / FTS 语义通道
RERANK_KEYS = [                 # 重排 / 扩充通道
    "graph_score",
    "aggregator_score",
    "procedural_score",
]

MERGE_MARKER = "merged"


def _to_float(value: Any) -> Optional[float]:
    """把分数字段安全转 float；不可解析时返回 None（标注 merged/缺失）。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def decompose_scores(
    hit: Dict[str, Any],
    channels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """白盒分解单条命中分数（纯函数，无副作用）。

    Parameters
    ----------
    hit : dict
        hybrid_retriever 融合后的单条结果，可能含
        ``memory_id`` / ``hybrid_score`` / ``vector_score`` /
        ``bm25_score`` / ``graph_score`` / ``aggregator_score`` /
        ``procedural_score``。
    channels : list[str], optional
        要探测的通道名（keyword / vector / graph / aggregator /
        procedural）。默认全部。

    Returns
    -------
    dict with keys:
        keyword_score  : BM25 关键词通道分数；缺失/不可解析 → None(merged)
        vector_score   : 向量语义通道分数；缺失/不可解析 → None(merged)
        rerank_factor  : 重排因子 dict 或 None——
                         {graph, aggregator, procedural} 各通道的实值；
                         无任何重排通道分量时 → None(merged)。
        final_score    : 融合终分 (hybrid_score)；缺失 → None。
        channels_hit   : 实际"命中"（分数存在于 hit 且 > 0）的通道名列表。
        merged_channels: 因融合被并入、无法单独还原的通道名列表
                         （对应分量为 null 的通道）。
    """
    channels = channels or ["keyword", "vector", "graph", "aggregator", "procedural"]
    hit = hit or {}
    enabled = {c for c in channels}

    keyword_val = _to_float(hit.get(KEYWORD_KEY)) if "keyword" in enabled else None
    vector_val = _to_float(hit.get(VECTOR_KEY)) if "vector" in enabled else None

    # 重排因子：图谱 / 聚合池 / 过程性 skill 通道的实值（缺失分量保持 null）。
    rerank: Dict[str, Optional[float]] = {}
    for name, key in (
        ("graph", "graph_score"),
        ("aggregator", "aggregator_score"),
        ("procedural", "procedural_score"),
    ):
        if name not in enabled:
            rerank[name] = None
            continue
        val = _to_float(hit.get(key))
        rerank[name] = None if val is None else val

    has_any_rerank = any(v is not None for v in rerank.values())
    rerank_factor = rerank if has_any_rerank else None

    final_score = _to_float(hit.get("hybrid_score"))

    # 命中通道：仅统计"显式存在且 > 0"的分量（score 缺失/为 0 不算命中）。
    hits: List[str] = []
    if keyword_val is not None and keyword_val > 0:
        hits.append("keyword")
    if vector_val is not None and vector_val > 0:
        hits.append("vector")
    for name, val in rerank.items():
        if val is not None and val > 0:
            hits.append(name)

    merged_channels: List[str] = []
    if keyword_val is None:
        merged_channels.append("keyword")
    if vector_val is None:
        merged_channels.append("vector")
    merged_channels += [name for name, val in rerank.items() if val is None]

    return {
        "keyword_score": keyword_val,
        "vector_score": vector_val,
        "rerank_factor": rerank_factor,
        "final_score": final_score,
        "channels_hit": hits,
        "merged_channels": merged_channels,
    }


def clamp_top_k(top_k: int, default: int = 5, max_top_k: int = 20) -> int:
    """top_k 上限截断纯函数：<1 → 1；>max_top_k → max_top_k；None → default。"""
    if top_k is None:
        return default
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        return default
    if k < 1:
        return 1
    if k > max_top_k:
        return max_top_k
    return k


def validate_query(q: Optional[str]) -> Optional[str]:
    """query 校验纯函数：None/空白 → 返回错误信息；合法 → 返回 None。"""
    if q is None:
        return "query is required"
    if not isinstance(q, str):
        return "query must be a string"
    if not q.strip():
        return "query must not be empty"
    return None


@router.get("/memory/search/explain")
async def search_explain(
    q: Optional[str] = Query(None, description="搜索查询字符串"),
    top_k: int = Query(5, description="返回结果数量（默认 5，经 clamp_top_k 钳制在 1..20）"),
    strategy: str = Query("rrf", description="融合策略: fusion / rrf / cascade"),
    situation: Optional[str] = Query(None, description="情境文本（编码特异性：情境相关记忆优先，EXECUTION 122）"),
):
    """召回可解释端点（白盒调试）。

    调现有 engine 混合检索，对每条命中给出分数分解与命中通道。
    空 query → 400；无结果 → 空列表；检索异常 → 500 + 日志。
    """
    err = validate_query(q)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)

    k = clamp_top_k(top_k, default=5, max_top_k=20)
    mem = get_memory()

    try:
        data = mem.search_hybrid(
            query=q,
            top_k=k,
            strategy=strategy,
            situation=situation,
        )
    except Exception as exc:  # pragma: no cover - 异常路径
        logger.error("search/explain retrieval failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Search failed: {exc}")

    results = data.get("results", data if isinstance(data, list) else [])
    adapter = getattr(mem, "_adapter", None)

    out: List[Dict[str, Any]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        preview = r.get("content_preview") or r.get("content") or ""
        if not preview:
            mid = r.get("memory_id")
            if mid and adapter is not None:
                try:
                    detail = adapter.get_memory(mid)
                    if detail and detail.get("content"):
                        preview = detail["content"]
                except Exception:
                    pass
        dec = decompose_scores(r)
        channels_hit = list(dict.fromkeys(dec.pop("channels_hit")))
        dec["channels_hit"] = channels_hit
        out.append({
            "memory_id": r.get("memory_id"),
            "content_preview": (preview[:200] if preview else ""),
            "decompose_scores": dec,
            "channels_hit": channels_hit,
        })

    return out
