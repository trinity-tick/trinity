"""P1-4: 引擎图谱通道 PPR 增强 (COMPARISON_VS_2026_SOTA_R8).

Verifies:
  - ppr_from_graph: 种子得分最高、邻居扩散、悬空节点质量守恒、include_seeds
  - HybridRetriever._get_graph_results 走 PPR 分支（TRINITY_GRAPH_PPR 门控）
  - 聚合池 _AggregatorKGraphAdapter.ppr_search 复用共享实现
"""

import os

import pytest

from trinity.kgraph.ppr_core import ppr_from_graph
from trinity.retrieval.hybrid_retriever import HybridRetriever


def _simple_graph():
    # a -- b -- c ;  a -- d
    return {
        "a": {"b": "semantic", "d": "semantic"},
        "b": {"a": "semantic", "c": "semantic"},
        "c": {"b": "semantic"},
        "d": {"a": "semantic"},
    }


def test_ppr_seed_ranks_first():
    hits = ppr_from_graph(_simple_graph(), ["a"], top_k=10)
    assert hits[0]["id"] == "a"
    assert hits[0]["score"] > 0


def test_ppr_neighbor_diffusion():
    hits = ppr_from_graph(_simple_graph(), ["a"], top_k=10)
    ranked = {h["id"]: h["score"] for h in hits}
    # b 和 d 是 a 的直接邻居，得分应高于二跳邻居 c
    assert ranked["b"] > ranked["c"]
    assert ranked["d"] > ranked["c"]


def test_ppr_include_seeds_false():
    hits = ppr_from_graph(_simple_graph(), ["a"], top_k=10, include_seeds=False)
    ids = {h["id"] for h in hits}
    assert "a" not in ids
    assert ids


def test_ppr_dangling_node_quality():
    """悬空节点（无出边）跳回个性化分布，质量守恒（分数和为 1）。"""
    graph = {"a": {"b": "semantic"}, "b": {}}
    hits = ppr_from_graph(graph, ["a"], top_k=10, max_iter=100)
    total = sum(h["score"] for h in hits)
    assert abs(total - 1.0) < 1e-3


def test_ppr_empty_seeds():
    assert ppr_from_graph(_simple_graph(), [], top_k=10) == []
    assert ppr_from_graph(_simple_graph(), ["nonexistent"], top_k=10) == []


class _FakeGraph:
    def search_by_entity(self, query, top_k):
        return [{"memory_id": "m1", "content": "base", "score": 0.5}]


class _FakePpr:
    def __call__(self, query, top_k):
        return [{"id": "m2", "memory_id": "m2", "score": 0.9, "content": ""}]


def test_hybrid_graph_channel_includes_ppr(monkeypatch):
    monkeypatch.setenv("TRINITY_GRAPH_PPR", "on")
    hr = HybridRetriever(
        bm25_index=None,
        graph_retriever=_FakeGraph(),
        search_fn=lambda q, top_k: [],
        ppr_fn=_FakePpr(),
    )
    results = hr._get_graph_results("query", top_k=10)
    mids = {r.get("memory_id") for r in results}
    assert "m1" in mids       # 基础实体检索
    assert "m2" in mids       # PPR 增强命中
    assert any(r.get("source") == "graph_ppr" for r in results)


def test_hybrid_graph_channel_ppr_off(monkeypatch):
    monkeypatch.setenv("TRINITY_GRAPH_PPR", "off")
    hr = HybridRetriever(
        bm25_index=None,
        graph_retriever=_FakeGraph(),
        search_fn=lambda q, top_k: [],
        ppr_fn=_FakePpr(),
    )
    results = hr._get_graph_results("query", top_k=10)
    mids = {r.get("memory_id") for r in results}
    assert "m1" in mids
    assert "m2" not in mids    # PPR 关闭时不注入


def test_hybrid_graph_channel_ppr_failure_falls_back(monkeypatch):
    """PPR 抛异常 → 静默降级为基础实体检索（不破坏主通道）。"""
    monkeypatch.setenv("TRINITY_GRAPH_PPR", "on")

    def _boom(query, top_k):
        raise RuntimeError("ppr exploded")

    hr = HybridRetriever(
        bm25_index=None,
        graph_retriever=_FakeGraph(),
        search_fn=lambda q, top_k: [],
        ppr_fn=_boom,
    )
    results = hr._get_graph_results("query", top_k=10)
    assert {r.get("memory_id") for r in results} == {"m1"}


def test_aggregator_adapter_reuses_shared_ppr():
    from trinity.agents.aggregator._kgraph_adapter import _AggregatorKGraphAdapter

    class _FakeAgg:
        _relations_graph = _simple_graph()

    adapter = _AggregatorKGraphAdapter(_FakeAgg())
    hits = adapter.ppr_search(["a"], top_k=10)
    assert hits[0]["id"] == "a"
    ranked = {h["id"]: h["score"] for h in hits}
    assert ranked["b"] > ranked["c"]
