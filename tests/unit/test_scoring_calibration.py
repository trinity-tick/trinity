"""Trinity — 评分校准层 + PPR 升级测试（2026-08-17, 建议2/4/5/6 落地）。

覆盖：
- TRINITY_CONFIDENCE_SCORER：四维置信度校准（旧记忆比新记忆降权更多）
- TRINITY_IMPORTANCE_BOOST：importance 动态微调（高 importance 上移）
- TRINITY_RERANK：Cross-Encoder 重排（fake 验证顺序变更 + 默认关闭不变）
- ppr_search：幂迭代 PPR（种子居首 / 1 跳 > 2 跳 / 收敛 / 空图不崩）
"""

from __future__ import annotations

import os
import time

import pytest

from trinity.agents.aggregator import MemoryAggregator


@pytest.fixture()
def agg() -> MemoryAggregator:
    a = MemoryAggregator(persist_path=None)
    yield a
    a.shutdown()


def _hybrid(agg: MemoryAggregator, q: str, limit: int = 10):
    return agg.query({}, limit=limit, mode="hybrid", query_text=q)


# ── 建议2: 置信度校准 ─────────────────────────────────────────────

def test_confidence_calibration_discounts_stale(agg: MemoryAggregator, monkeypatch) -> None:
    """旧记忆比新记忆在置信度校准下降权更多（时效窗口）。"""
    fresh = agg.ingest("最新数据库方案 PostgreSQL 17 发布", "eng", {"category": "general"})
    stale = agg.ingest("旧数据库方案 MySQL 5.7 配置", "eng", {"category": "general"})
    now = time.time()
    fresh.created_at = now - 3600          # 1 小时前（fresh）
    stale.created_at = now - 400 * 86400   # 400 天前（stale，超 general 365d 窗口）
    for dv in (fresh, stale):
        dv.priority = 0.8

    # 隔离 RL 隐式反馈（2026-08-17 起查询自动打 IMPLICIT_USE，会影响两次查询间的 Q 值）
    monkeypatch.setenv("TRINITY_RL_SCORER", "off")
    monkeypatch.delenv("TRINITY_CONFIDENCE_SCORER", raising=False)
    off = {r.memory_id: r.priority for r in _hybrid(agg, "数据库 方案 配置")}
    monkeypatch.setenv("TRINITY_CONFIDENCE_SCORER", "on")
    on = {r.memory_id: r.priority for r in _hybrid(agg, "数据库 方案 配置")}

    ratio_fresh = on.get(fresh.memory_id, 0.0) / max(off.get(fresh.memory_id, 1e-9), 1e-9)
    ratio_stale = on.get(stale.memory_id, 0.0) / max(off.get(stale.memory_id, 1e-9), 1e-9)
    assert ratio_fresh > ratio_stale  # 新鲜记忆保留更多分数


# ── 建议5: importance 动态微调 ────────────────────────────────────

def test_importance_boost_shifts_priority(agg: MemoryAggregator, monkeypatch) -> None:
    """高 importance 记忆上移、低 importance 记忆下移（有界 ±0.1）。"""
    hi = agg.ingest("高价值核心决策：系统整体迁移到 PostgreSQL，全量数据同步完成，后续所有服务统一走新库。该决策影响全部下游组件与数据链路。", "eng", {"category": "decision"})
    lo = agg.ingest("临时备注", "eng", {"category": "trace"})
    hi.access_count = 10  # importance_score 访问频率因子拉高
    for dv in (hi, lo):
        dv.priority = 0.5

    # 隔离 RL 隐式反馈（2026-08-17 起查询自动打 IMPLICIT_USE）
    monkeypatch.setenv("TRINITY_RL_SCORER", "off")
    monkeypatch.delenv("TRINITY_IMPORTANCE_BOOST", raising=False)
    off = {r.memory_id: r.priority for r in _hybrid(agg, "系统 决策 数据库")}
    monkeypatch.setenv("TRINITY_IMPORTANCE_BOOST", "on")
    on = {r.memory_id: r.priority for r in _hybrid(agg, "系统 决策 数据库")}

    d_hi = on.get(hi.memory_id, 0.0) - off.get(hi.memory_id, 0.0)
    d_lo = on.get(lo.memory_id, 0.0) - off.get(lo.memory_id, 0.0)
    assert d_hi > d_lo
    assert abs(d_hi) <= 0.11 and abs(d_lo) <= 0.11  # 有界


# ── 建议6: Cross-Encoder 重排 ─────────────────────────────────────

def test_rerank_env_on_changes_order(agg: MemoryAggregator, monkeypatch) -> None:
    """TRINITY_RERANK=on 时按重排器顺序重排。

    fake 重排器用"输入顺序无关"的固定排序（按内容长度降序），断言结果
    完全确定——不依赖 RRF 基序在两次调用间稳定，也不受 RL bonus ±0.15
    扰动（重排后 priority 相邻差 ≥0.166 > 0.15，满载环境也不翻转）。
    """
    a = agg.ingest("A", "eng", {"category": "x"})       # len 1
    b = agg.ingest("BB", "eng", {"category": "x"})      # len 2
    c = agg.ingest("CCC", "eng", {"category": "x"})     # len 3

    class _FakeReranker:
        def __init__(self, **kw):
            pass

        def rerank(self, query, candidates, top_k=10, **kw):
            # 固定排序：按 text 长度降序（与输入顺序无关）
            return sorted(candidates, key=lambda x: -len(x.get("text", "")))[:top_k]

    import trinity.vector_index.reranker as rk_mod
    monkeypatch.setattr(rk_mod, "CrossEncoderReranker", _FakeReranker)
    monkeypatch.setenv("TRINITY_RERANK", "on")
    on_ids = [r.memory_id for r in _hybrid(agg, "A BB CCC")]
    # 期望：内容最长者居首（CCC → BB → A）
    assert on_ids == [c.memory_id, b.memory_id, a.memory_id]


def test_rerank_env_off_unchanged(agg: MemoryAggregator, monkeypatch) -> None:
    """默认关闭：不加载模型、顺序不变（回归保护）。"""
    for i in range(3):
        agg.ingest(f"回归记忆 {i} 内容", "eng", {"category": "x"})
    monkeypatch.delenv("TRINITY_RERANK", raising=False)
    ids1 = [r.memory_id for r in _hybrid(agg, "回归 记忆")]
    monkeypatch.setenv("TRINITY_RERANK", "off")
    ids2 = [r.memory_id for r in _hybrid(agg, "回归 记忆")]
    assert ids1 == ids2


# ── 建议4: PPR 幂迭代 ─────────────────────────────────────────────

def test_ppr_seed_first_and_decay(agg: MemoryAggregator) -> None:
    """种子居首；1 跳 > 2 跳（距离衰减）；分数收敛。"""
    a = agg.ingest("A", "eng", {"category": "x"})
    b = agg.ingest("B", "eng", {"category": "x"})
    c = agg.ingest("C", "eng", {"category": "x"})
    agg._relations_graph.setdefault(a.memory_id, {})[b.memory_id] = "links"
    agg._relations_graph.setdefault(b.memory_id, {})[c.memory_id] = "links"

    res = agg._graph_channel.ppr_search([a.memory_id], top_k=10)
    ids = [g["id"] for g in res]
    scores = {g["id"]: g["score"] for g in res}
    assert ids[0] == a.memory_id
    assert ids.index(b.memory_id) < ids.index(c.memory_id)
    assert scores[b.memory_id] > scores[c.memory_id]
    assert abs(sum(scores.values()) - 1.0) < 0.15  # 分布收敛于 1


def test_ppr_empty_graph_no_crash(agg: MemoryAggregator) -> None:
    res = agg._graph_channel.ppr_search(["nonexistent_seed"], top_k=5)
    assert isinstance(res, list)


def test_ppr_deterministic(agg: MemoryAggregator) -> None:
    a = agg.ingest("X1", "eng", {"category": "x"})
    b = agg.ingest("X2", "eng", {"category": "x"})
    agg._relations_graph.setdefault(a.memory_id, {})[b.memory_id] = "links"
    r1 = agg._graph_channel.ppr_search([a.memory_id], top_k=5)
    r2 = agg._graph_channel.ppr_search([a.memory_id], top_k=5)
    assert r1 == r2


# ── 引擎路径校准（HybridRetriever）────────────────────────────────

def _mk_hybrid_retriever():
    from trinity.retrieval.hybrid_retriever import HybridRetriever
    return HybridRetriever(
        bm25_index=None,
        graph_retriever=None,
        search_fn=lambda q, top_k: [],
    )


def test_engine_confidence_calibration_discounts_stale(monkeypatch) -> None:
    """引擎路径：旧记忆比新记忆降权更多，重排后新记忆居首。"""
    import time as _t
    hr = _mk_hybrid_retriever()
    now = _t.time()
    fused = [
        {"memory_id": "old", "content": "旧配置", "hybrid_score": 0.8,
         "created_at": now - 400 * 86400, "category": "general", "access_count": 0},
        {"memory_id": "new", "content": "新配置", "hybrid_score": 0.8,
         "created_at": now - 3600, "category": "general", "access_count": 0},
    ]
    monkeypatch.setenv("TRINITY_CONFIDENCE_SCORER", "on")
    out = hr._apply_engine_calibration(list(fused), "数据库 配置")
    assert out[0]["memory_id"] == "new"  # 新鲜记忆居首
    assert out[0]["hybrid_score"] > out[1]["hybrid_score"]


def test_engine_importance_boost(monkeypatch) -> None:
    """引擎路径：importance 高记忆上移（±0.1 有界）。"""
    hr = _mk_hybrid_retriever()
    fused = [
        {"memory_id": "hi", "content": "x", "hybrid_score": 0.5, "importance": 0.9},
        {"memory_id": "lo", "content": "y", "hybrid_score": 0.5, "importance": 0.1},
    ]
    monkeypatch.setenv("TRINITY_IMPORTANCE_BOOST", "on")
    out = hr._apply_engine_calibration(list(fused), "q")
    assert out[0]["memory_id"] == "hi"
    d_hi = out[0]["hybrid_score"] - 0.5
    assert 0.07 <= d_hi <= 0.11  # (0.9-0.5)*0.2 = 0.08


def test_engine_calibration_off_unchanged(monkeypatch) -> None:
    """默认关闭：分数与顺序不变（回归保护）。"""
    hr = _mk_hybrid_retriever()
    fused = [
        {"memory_id": "a", "content": "x", "hybrid_score": 0.3},
        {"memory_id": "b", "content": "y", "hybrid_score": 0.9},
    ]
    monkeypatch.delenv("TRINITY_CONFIDENCE_SCORER", raising=False)
    monkeypatch.delenv("TRINITY_IMPORTANCE_BOOST", raising=False)
    out = hr._apply_engine_calibration(list(fused), "q")
    # 关闭时保持输入顺序与分数不变（early-return，不排序不修改）
    assert out[0]["memory_id"] == "a"
    assert out[1]["memory_id"] == "b"
    assert abs(out[0]["hybrid_score"] - 0.3) < 1e-9
    assert abs(out[1]["hybrid_score"] - 0.9) < 1e-9
