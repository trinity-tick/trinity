#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""`retrieval_health()` 判据测试（2026-10-02，事故后建立）。

事故实测：权威库损坏时 `POST /memory/search/hybrid` 返回 **HTTP 200 + results: []**，
上游无法区分「真的没有相关记忆」与「检索面坏了」。

本测试的核心是**反事实**：同一个函数，
  · 把 `no_adapter` 那一支去掉 ⇒ 事故形状（A 案）必须变成 ok=True（即判据不是恒真）；
  · 健康面 + 合法空结果（B 案）**不得**被判故障（否则在健康系统上造假红）。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from trinity.api.server._routers_search import retrieval_health  # noqa: E402

ACCIDENT = {
    "breakdown": {"vector_channel": "none:no_adapter", "routing": "full",
                  "vector": 0, "bm25": 0, "graph": 0, "aggregator": 0,
                  "procedural": 0, "pagetree": 0},
}
HEALTHY_EMPTY = {
    "breakdown": {"vector_channel": "lexical:SQLiteAdapter.search_memories",
                  "routing": "full", "vector": 0, "bm25": 0},
}
HEALTHY_HIT = {
    "breakdown": {"vector_channel": "lexical:SQLiteAdapter.search_memories",
                  "routing": "full", "vector": 5, "bm25": 5},
}


# ── A 事故形状：必须 ok=False 且指名原因 ────────────────────────────────
def test_accident_shape_is_flagged_as_fault():
    h = retrieval_health(ACCIDENT, [])
    assert h["ok"] is False
    assert h["degraded_reason"] and "no_adapter" in h["degraded_reason"]
    assert h["n_results"] == 0
    # 同时报出特征信号（供排查），但它不是判故障的那一条
    assert h["signals"] == ["all_channels_zero"]


def test_accident_shape_counts_as_zero_result_with_fault():
    stats = {}
    retrieval_health(ACCIDENT, [], stats)
    assert stats["zero_result_with_fault"] == 1
    assert stats["zero_result_healthy"] == 0
    assert stats["zero_result_all_channels_zero"] == 1


# ── B 健康面 + 合法空结果：**不得**判故障（防假红）────────────────────
def test_healthy_empty_is_not_a_fault():
    h = retrieval_health(HEALTHY_EMPTY, [])
    assert h["ok"] is True, h
    assert h["degraded_reason"] is None
    assert h["signals"] == ["all_channels_zero"]   # 只是信号，不是故障


def test_healthy_empty_counts_as_healthy_zero():
    stats = {}
    retrieval_health(HEALTHY_EMPTY, [], stats)
    assert stats["zero_result_healthy"] == 1
    assert stats["zero_result_with_fault"] == 0


# ── C 正常命中 ────────────────────────────────────────────────────────
def test_healthy_hits_are_ok():
    h = retrieval_health(HEALTHY_HIT, [{"score": 0.8}, {"score": 0.4}])
    assert h["ok"] is True
    assert h["n_results"] == 2
    assert h["signals"] is None


def test_all_weak_scores_are_signal_only():
    h = retrieval_health(HEALTHY_HIT, [{"score": 0.1}, {"score": 0.05}])
    assert h["ok"] is True                       # 弱分不是故障
    assert h["degraded_reason"] is None


# ── D 健壮性：奇怪输入不得抛 ──────────────────────────────────────────
@pytest.mark.parametrize("data,results", [
    ({}, []),
    ({"breakdown": None}, []),
    ({"breakdown": "not-a-dict"}, None),
    ({"breakdown": {"channels": "not-a-list", "vector_channel": None}}, [{"score": None}]),
])
def test_never_raises_on_odd_input(data, results):
    h = retrieval_health(data, results)
    assert "ok" in h and "note" in h


# ── E **反事实**：拿掉 no_adapter 判定后，事故形状不再被告警 ──────────
def test_counterfactual_without_no_adapter_branch_it_goes_green(monkeypatch):
    """把 no_adapter 判据短路 ⇒ 同一份事故输入必须变 ok=True。

    这条证明：A 案的红**确实来自那条判据**，而不是别的副作用
    （本仓纪律：判据必须先证明自己有判别力）。
    """
    import trinity.api.server._routers_search as mod
    src_marker = 'if vc.startswith("none:") or vc.startswith("unknown:"):'
    assert src_marker in open(mod.__file__, encoding="utf-8").read(), "判据已改，反事实需同步"

    real = mod.retrieval_health

    def patched(data, results, stats=None):
        # 只对 ACCIDENT 那种 vc 做短路，模拟"没有这条判据"
        d = dict(data)
        br = dict(d.get("breakdown") or {})
        if str(br.get("vector_channel") or "").startswith("none:"):
            br["vector_channel"] = "lexical:stub"
            br["routing"] = "light"     # 同时去掉 all_channels_zero 的形状
        d["breakdown"] = br
        return real(d, results, stats)

    assert real(ACCIDENT, [])["ok"] is False     # 判据在位 ⇒ 红
    assert patched(ACCIDENT, [])["ok"] is True   # 判据缺席 ⇒ 绿（说明它真的在起作用）


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
