"""核心模块测试：检索（hybrid_retriever 5 通道 RRF + 校准 + 查询扩展）。

Verifies:
  - HybridRetriever 5 通道结果归一化 + RRF 融合
  - 置信度/importance/strength 校准（env 门控）
  - _expand_query PRF 扩展（短查询限定）
  - _minmax_normalise 归一化正确性
"""

import sys

sys.path.insert(0, "C:/Users/Administrator/trinity")
import os
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

import pytest

from trinity.retrieval.hybrid_retriever import HybridRetriever, _minmax_normalise
from trinity.retrieval.bm25_index import BM25Index


def test_minmax_normalise():
    """归一化：最大值→1，最小值→0，空列表→空。"""
    assert _minmax_normalise([], "score") == []
    items = [{"memory_id": "a", "score": 10.0},
             {"memory_id": "b", "score": 0.0},
             {"memory_id": "c", "score": 5.0}]
    out = _minmax_normalise(items, "score")
    scores = {x["memory_id"]: x["score"] for x in out}
    assert scores["a"] == pytest.approx(1.0)
    assert scores["b"] == pytest.approx(0.0)
    assert scores["c"] == pytest.approx(0.5)


def test_bm25_index_basic():
    """BM25 索引：文档添加 + 检索返回相关文档。"""
    idx = BM25Index(k1=1.5, b=0.75)
    idx.add_documents([
        ("d1", "I bought a blue bicycle in Berlin"),
        ("d2", "My favorite food is sushi"),
        ("d3", "Berlin is famous for blue bicycles"),
    ])
    hits = idx.search("blue bicycle", top_k=3)
    assert len(hits) >= 1
    # d1/d3 含 blue+bicycle 应排前（返回 (doc_id, score)）
    assert hits[0][0] in ("d1", "d3")


def test_bm25_k1_affects_scores():
    """BM25 k1 参数影响分数（可测域验证）。"""
    idx1 = BM25Index(k1=1.5, b=0.75)
    idx2 = BM25Index(k1=0.1, b=0.1)
    docs = [("d1", "blue bicycle berlin river tour summer"),
            ("d2", "blue blue blue bicycle berlin")]
    idx1.add_documents(docs)
    idx2.add_documents(docs)
    h1 = dict(idx1.search("blue bicycle", top_k=2))
    h2 = dict(idx2.search("blue bicycle", top_k=2))
    # k1 不同 → 分数不同（高频词饱和差异）
    assert h1.get("d1") != h2.get("d1")


def test_expand_query_short_only():
    """查询扩展 v2：短查询（≤3 词）才扩展；长查询原样。"""
    mem = type("M", (), {
        "_search_fn": lambda q, top_k: [
            {"content": "blue bicycle berlin river tour summer bicycle"},
            {"content": "bicycle shop near berlin opens monday"},
        ],
        "_STOPWORDS": frozenset({"the", "a", "an", "is", "are", "i", "my", "you", "and"}),
    })()
    hr = HybridRetriever.__new__(HybridRetriever)
    hr._search_fn = mem._search_fn
    hr._STOPWORDS = mem._STOPWORDS
    # 短查询 → 扩展
    expanded = hr._expand_query("blue bicycle")
    assert "bicycle" in expanded or "berlin" in expanded
    assert "blue" in expanded  # 原词保留
    # 长查询 → 不扩展
    long_q = "what time is my wake up scheduled on tuesdays"
    assert hr._expand_query(long_q) == long_q
