# -*- coding: utf-8 -*-
"""双强度因子（Bjork 双强度模型）检索排名单测。

覆盖 trinity.retrieval.hybrid_retriever.HybridRetriever._apply_engine_calibration
新增的 TRINITY_STRENGTH_BOOST 行为（第 49 轮，2026-08-20）：
  - 最近访问/高频使用的记忆排名微升（对应脑科学"测试效应/检索强化"）
  - 无访问数据的记忆保持中性（不改变基线）
  - env 关闭时完全不变；坏数据不崩、分数有界
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import pytest

from trinity.retrieval.hybrid_retriever import HybridRetriever


@pytest.fixture
def retriever():
    return HybridRetriever(
        bm25_index=None,
        graph_retriever=None,
        search_fn=lambda query, top_k: [],
    )


BASE = [
    {"memory_id": "m_new_hot", "hybrid_score": 0.70,
     "access_count": 12, "last_accessed_at": "2026-08-20T00:00:00+00:00"},
    {"memory_id": "m_old_cold", "hybrid_score": 0.70,
     "access_count": 0, "last_accessed_at": "2026-01-01T00:00:00+00:00"},
    {"memory_id": "m_no_data", "hybrid_score": 0.70},
]


def _run(retriever, fused):
    return retriever._apply_engine_calibration([dict(x) for x in fused], "test query")


class TestStrengthBoost:
    def test_recent_and_high_access_ranks_first(self, retriever, monkeypatch):
        monkeypatch.setenv("TRINITY_STRENGTH_BOOST", "on")
        out = _run(retriever, BASE)
        assert out[0]["memory_id"] == "m_new_hot"
        assert all(0.0 <= f["hybrid_score"] <= 1.0 for f in out)

    def test_no_access_data_stays_neutral(self, retriever, monkeypatch):
        monkeypatch.setenv("TRINITY_STRENGTH_BOOST", "on")
        out = _run(retriever, BASE)
        mnd = next(f for f in out if f["memory_id"] == "m_no_data")
        assert mnd["hybrid_score"] == pytest.approx(0.70, abs=1e-9)

    def test_old_and_unused_demoted(self, retriever, monkeypatch):
        monkeypatch.setenv("TRINITY_STRENGTH_BOOST", "on")
        out = _run(retriever, BASE)
        moc = next(f for f in out if f["memory_id"] == "m_old_cold")
        assert moc["hybrid_score"] < 0.70

    def test_disabled_leaves_scores_unchanged(self, retriever, monkeypatch):
        monkeypatch.setenv("TRINITY_STRENGTH_BOOST", "off")
        out = _run(retriever, BASE)
        assert all(
            f["hybrid_score"] == pytest.approx(0.70, abs=1e-9) for f in out
        )

    def test_default_is_off_preserves_baseline(self, retriever, monkeypatch):
        # 2026-08-20 决策：默认 off（opt-in），基线纯净；不设环境变量时不得改变分数
        monkeypatch.delenv("TRINITY_STRENGTH_BOOST", raising=False)
        out = _run(retriever, BASE)
        assert all(
            f["hybrid_score"] == pytest.approx(0.70, abs=1e-9) for f in out
        )

    def test_robust_bad_data(self, retriever, monkeypatch):
        monkeypatch.setenv("TRINITY_STRENGTH_BOOST", "on")
        out = _run(retriever, [
            {"memory_id": "m_bad", "hybrid_score": 0.6,
             "access_count": -5, "last_accessed_at": "garbage"},
            {"memory_id": "m_ok", "hybrid_score": 0.6,
             "access_count": 3, "last_accessed_at": None},
            {"memory_id": "m_z", "hybrid_score": 0.6},
        ])
        assert len(out) == 3
        assert all(0.0 <= f["hybrid_score"] <= 1.0 for f in out)
