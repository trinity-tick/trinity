# -*- coding: utf-8 -*-
"""Trinity — /memory/search/explain 召回可解释端点的单元测试。

覆盖（不连真实服务）：
- decompose_scores 对构造 hit 的正确拆分（含缺失分量 → null/merged 标注）
- top_k 上限截断纯函数 clamp_top_k
- query 校验纯函数 validate_query
- 端点空结果路径与参数校验（TestClient + monkeypatch get_memory）
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from trinity.api.server._routers_explain import (
    clamp_top_k,
    decompose_scores,
    router,
    validate_query,
)

# 构造一个只挂载 explain router 的最小 app，避免拉起整个 Trinity server。
_app = FastAPI()
_app.include_router(router)


def _make_hit(**overrides) -> dict:
    hit = {
        "memory_id": "m_1",
        "hybrid_score": 0.9,
        "vector_score": 0.95,
        "bm25_score": 0.3,
        "graph_score": 0.5,
        "aggregator_score": 0.7,
        "procedural_score": 0.2,
        "content": "hello world",
    }
    hit.update(overrides)
    return hit


# ── decompose_scores ──────────────────────────────────────────────────

def test_decompose_full_hit_separates_components() -> None:
    """全通道命中：keyword/vector/重排(图谱+聚合+过程)/final 正确分离。"""
    dec = decompose_scores(_make_hit())
    assert dec["keyword_score"] == 0.3
    assert dec["vector_score"] == 0.95
    assert dec["final_score"] == 0.9
    rf = dec["rerank_factor"]
    assert rf == {"graph": 0.5, "aggregator": 0.7, "procedural": 0.2}
    assert set(dec["channels_hit"]) == {"keyword", "vector", "graph", "aggregator", "procedural"}
    assert dec["merged_channels"] == []


def test_decompose_is_pure_no_side_effect() -> None:
    """decompose_scores 不改写传入 hit，是纯函数。"""
    hit = _make_hit()
    before = dict(hit)
    dec = decompose_scores(hit)
    assert hit == before
    assert dec["final_score"] == 0.9


def test_decompose_missing_channels_marked_merged() -> None:
    """缺失分量 → 对应分数为 None，并入 merged_channels 标记。"""
    # RRF 风格命中：只带 keyword + vector + graph，聚合/过程通道缺失。
    hit = _make_hit(
        hybrid_score=0.12,
        vector_score=0.8,
        bm25_score=0.6,
        graph_score=0.4,
    )
    hit.pop("aggregator_score", None)
    hit.pop("procedural_score", None)
    dec = decompose_scores(hit)
    assert dec["keyword_score"] == 0.6
    assert dec["vector_score"] == 0.8
    # graph 有值；aggregator/procedural 缺失 → 记入 merged_channels
    assert dec["rerank_factor"]["graph"] == 0.4
    assert dec["rerank_factor"]["aggregator"] is None
    assert dec["rerank_factor"]["procedural"] is None
    assert dec["final_score"] == 0.12
    assert set(dec["channels_hit"]) == {"keyword", "vector", "graph"}
    assert "keyword" not in dec["merged_channels"]
    assert "vector" not in dec["merged_channels"]
    assert "graph" not in dec["merged_channels"]
    assert set(dec["merged_channels"]) == {"aggregator", "procedural"}


def test_decompose_when_rerank_absent_returns_none() -> None:
    """无任何重排分量 → rerank_factor 为 None（整体 merged）。"""
    hit = _make_hit(hybrid_score=0.5, bm25_score=None, vector_score=0.4)
    for key in ("graph_score", "aggregator_score", "procedural_score"):
        hit.pop(key, None)
    dec = decompose_scores(hit)
    assert dec["vector_score"] == 0.4
    assert dec["keyword_score"] is None
    assert dec["rerank_factor"] is None
    assert set(dec["merged_channels"]) == {"keyword", "graph", "aggregator", "procedural"}


def test_decompose_empty_hit_tolerated() -> None:
    """空 hit（{}）不抛异常，全部返回 None/空。"""
    dec = decompose_scores({})
    assert dec["keyword_score"] is None
    assert dec["vector_score"] is None
    assert dec["rerank_factor"] is None
    assert dec["final_score"] is None
    assert dec["channels_hit"] == []


# ── 参数校验纯函数 ─────────────────────────────────────────────────────

def test_clamp_top_k_bounds() -> None:
    """top_k 上限截断：越界夹紧，None 用默认。"""
    assert clamp_top_k(None) == 5
    assert clamp_top_k(999) == 20
    assert clamp_top_k(0) == 1
    assert clamp_top_k(-3) == 1
    assert clamp_top_k(7) == 7
    assert clamp_top_k(20) == 20
    assert clamp_top_k("junk") == 5


def test_validate_query_empty_and_valid() -> None:
    """query 校验：空/None → 错误信息，合法 → None。"""
    assert validate_query(None) is not None
    assert validate_query("   ") is not None
    assert validate_query("") is not None
    assert validate_query("python memory") is None


# ── 端点路径（monkeypatch get_memory）────────────────────────────────

def _patch_memory(monkeypatch, search_result, raise_on_search=False):
    class _FakeMem:
        def __init__(self):
            self._adapter = None

        def search_hybrid(self, **kwargs):
            if raise_on_search:
                raise RuntimeError("boom")
            return search_result

    monkeypatch.setattr(
        "trinity.api.server.get_memory", lambda: _FakeMem()
    )
    return _FakeMem


def test_endpoint_empty_results_returns_empty_list(monkeypatch) -> None:
    """无结果路径：search_hybrid 返回空 results → 返回 []。"""
    _patch_memory(monkeypatch, {"results": [], "strategy": "rrf", "query": "x", "breakdown": {}})
    with TestClient(_app) as client:
        resp = client.get("/memory/search/explain", params={"q": "x"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_endpoint_valid_query_returns_decomposition(monkeypatch) -> None:
    """正常路径：返回 memory_id / content_preview / decompose_scores / channels_hit。"""
    hit = _make_hit()
    _patch_memory(monkeypatch, {"results": [hit], "strategy": "rrf", "query": "x", "breakdown": {}})
    with TestClient(_app) as client:
        resp = client.get("/memory/search/explain", params={"q": "x"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    item = body[0]
    assert item["memory_id"] == "m_1"
    assert item["content_preview"].startswith("hello world")
    dec = item["decompose_scores"]
    assert dec["keyword_score"] == 0.3
    assert dec["vector_score"] == 0.95
    assert dec["final_score"] == 0.9
    assert set(dec["channels_hit"]) == {"keyword", "vector", "graph", "aggregator", "procedural"}
    assert item["channels_hit"] == dec["channels_hit"]


def test_endpoint_empty_query_returns_400(monkeypatch) -> None:
    """空 query → 400。"""
    _patch_memory(monkeypatch, {"results": []})
    with TestClient(_app) as client:
        resp = client.get("/memory/search/explain", params={"q": "   "})
    assert resp.status_code == 400


def test_endpoint_search_exception_returns_500(monkeypatch) -> None:
    """检索异常 → 500。"""
    _patch_memory(monkeypatch, {}, raise_on_search=True)
    with TestClient(_app) as client:
        resp = client.get("/memory/search/explain", params={"q": "x"})
    assert resp.status_code == 500


def test_endpoint_top_k_truncated(monkeypatch) -> None:
    """top_k 超出上限被钳制并原样回传（不报错）。"""
    hits = [_make_hit(memory_id=f"m_{i}", hybrid_score=(20 - i) / 20.0, vector_score=0.9, bm25_score=0.5, graph_score=0.4, aggregator_score=0.3, procedural_score=0.2) for i in range(30)]
    _patch_memory(monkeypatch, {"results": hits, "strategy": "rrf", "query": "x", "breakdown": {}})

    call_kwargs = {}

    class _FakeMem2:
        _adapter = None

        def search_hybrid(self, **kwargs):
            call_kwargs.update(kwargs)
            return {"results": hits[: kwargs.get("top_k", 5)], "strategy": "rrf", "qry": "x", "breakdown": {}}

    monkeypatch.setattr("trinity.api.server.get_memory", lambda: _FakeMem2())
    with TestClient(_app) as client:
        resp = client.get("/memory/search/explain", params={"q": "x", "top_k": 999999})
    assert resp.status_code == 200
    # 传 999999 被钳到 20 后传给 search_hybrid
    assert call_kwargs["top_k"] == 20
    assert len(resp.json()) == 20
