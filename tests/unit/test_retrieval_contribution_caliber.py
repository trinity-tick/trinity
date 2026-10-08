# -*- coding: utf-8 -*-
"""检索基准脚本的**口径护栏**（2026-10-06, t19）

t19 的教训有两条，本文件把它们变成常驻判据，防止下次再"静默量错东西"：

  T1 **`vector` 键在 SQLite + `use_ann=False` 下结构性走词法** —— 所以任何想测"真嵌入通道"的
     臂都必须**显式声明后端与 ANN**，否则它只是 `chan_keyword` 的副本。
     （触发事件：t3 曾有一个 `chan_vector_embed` 臂，实测与 `chan_keyword` 逐项相同 ⇒ 已删除。）

  T2 **`_vector_search` 的失败是静默降级**（每层都吞，表现为"词法的数"）——
     所以基准脚本必须**逐臂留档** `vector_preflight`（实现名 + 是否走嵌入 + 后端），
     否则"缺包 ⇒ 臂变形"无法从产物里看出来。

本文件只依赖 stdlib + pytest + trinity（不需要 sklearn/onnx/faiss）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

RC_PATH = os.path.join(ROOT, "scripts", "retrieval_contribution.py")


def _load_rc():
    spec = importlib.util.spec_from_file_location("rc_under_test", RC_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rc_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_module_compiles():
    """脚本必须能被 compile()（队长要求：每步先 compile()）。"""
    src = open(RC_PATH, encoding="utf-8").read()
    compile(src, RC_PATH, "exec")


def test_fake_vector_arm_is_gone():
    """T1 闸门：那个"看起来测嵌入、实际测词法"的臂**不得**再出现。"""
    rc = _load_rc()
    assert "chan_vector_embed" not in rc.ARMS, (
        "`chan_vector_embed` 又回来了 —— 它在 SQLite + use_ann=False 下与 `chan_keyword` "
        "逐项相同（详见 RETRIEVAL-CONTRIBUTION.md §11.3.3）。要测真嵌入必须显式开 ANN 或用 PG。")


def test_routing_ab_arms_are_same_call_path():
    """T1 同族：routing 的 light/full 对比必须**同一条链路只改 routing**。

    t3 报告曾把 `prod_search_hybrid` 与 `prod_search_hybrid_light` 当成 light vs full —— 错，
    两者都经 `search()`（不透传 routing ⇒ auto ⇒ 题集查询全部判 full）。真正的 A/B 是这两个。
    """
    rc = _load_rc()
    for name in ("prod_hybrid_direct_full", "prod_hybrid_direct_light"):
        assert name in rc.ARMS, "同链路 routing A/B 臂缺失：%s" % name
        spec = rc.ARMS[name]
        assert spec.get("call") == "search_hybrid_direct", (
            "%s 必须走 search_hybrid 直连（否则不是同链路对比）" % name)
    assert rc.ARMS["prod_hybrid_direct_full"].get("routing") == "full"
    assert rc.ARMS["prod_hybrid_direct_light"].get("routing") == "light"
    assert rc.ARMS["prod_hybrid_direct_full"].get("capture_scores") is True
    assert rc.ARMS["prod_hybrid_direct_light"].get("capture_scores") is True


def test_sqlite_auto_vector_channel_is_lexical(tmp_path):
    """T1 的事实基础：SQLite + `use_ann=False` 下，**任何** `TRINITY_VECTOR_CHANNEL` 取值
    （除 `pgvector`）都由 `_use_embedding_channel` 判为"走词法"。

    这是"为什么 `auto` 臂不是嵌入臂"的**可执行理由**（不是我的推断）。
    """
    from trinity.core.client._hybrid_index import _use_embedding_channel, vector_channel_impl
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path / "store"))

    old = os.environ.get("TRINITY_VECTOR_CHANNEL")
    try:
        os.environ.pop("TRINITY_VECTOR_CHANNEL", None)          # 默认 lexical
        assert _use_embedding_channel(mem._adapter, False) is False
        assert vector_channel_impl(mem._adapter, False).startswith("lexical:")

        os.environ["TRINITY_VECTOR_CHANNEL"] = "auto"           # 曾经的假臂配置
        assert _use_embedding_channel(mem._adapter, False) is False, (
            "非 PG + use_ann=False 时 `auto` 也走词法（见 _hybrid_index.py:91-94）"
            "⇒ 把 `auto` 臂当成嵌入臂是错的")
        assert vector_channel_impl(mem._adapter, False).startswith("lexical:")

        os.environ["TRINITY_VECTOR_CHANNEL"] = "pgvector"       # 只有它强制嵌入
        assert _use_embedding_channel(mem._adapter, False) is True
        assert vector_channel_impl(mem._adapter, False).startswith("embedding:")
    finally:
        if old is None:
            os.environ.pop("TRINITY_VECTOR_CHANNEL", None)
        else:
            os.environ["TRINITY_VECTOR_CHANNEL"] = old


def test_vector_preflight_reports_impl_and_embedding_flag(tmp_path):
    """T2 闸门：`_vector_preflight` 必须**披露**实现名与"是否走嵌入"。

    它把这些字段写进每臂产物 ⇒ 将来任何"臂静默变形"都能从 JSON 里看出来，
    不需要重跑（t3 的教训：产物里没这个字段，我只能在残留里补一句）。
    """
    rc = _load_rc()
    from trinity import Trinity
    store = str(tmp_path / "store")
    mem = Trinity(adapter="sqlite", store_path=store)
    mem.ingest("Orion platform built by Carol team.", persona_id="p", session_id="1",
               category="general", tags=["t"])
    pf = rc._vector_preflight(mem)
    assert "impl" in pf or "impl_error" in pf, "vector_preflight 未披露实现名"
    assert "uses_embedding" in pf, "vector_preflight 未披露 uses_embedding"
    assert pf.get("impl", "").startswith("lexical:"), (
        "SQLite 默认应披露 lexical:…（实际 %r）" % pf.get("impl"))
    assert pf.get("uses_embedding") is False


def test_tie_stats_flags_truncation_ties():
    """§3.3 的判据自测：截断处同分必须被 `tie_stats` 认出来（含并列组大小）。"""
    rc = _load_rc()
    # 第 5/6 名同分，且并列组含 4 条（下标 4..7）
    pq = [{"scores": [3.0, 2.0, 2.0, 1.5, 1.0, 1.0, 1.0, 1.0, 0.5]}]
    st = rc.tie_stats(pq, k=5)
    assert st["tie_at_truncation"] == 1
    assert st["tie_at_truncation_rate"] == 1.0
    assert st["tie_group_size_of_k"]["max"] == 4, st
    # 不同分 ⇒ 不算并列
    st2 = rc.tie_stats([{"scores": [9, 8, 7, 6, 5, 4, 3]}], k=5)
    assert st2["tie_at_truncation"] == 0


def test_scoring_uses_same_denominator_and_substring_safe():
    """口径自测：`score_query` 的 R@k/recall@k/MRR 用**同一分母**，且包含式判定不放大。"""
    rc = _load_rc()
    exp = {"alpha fact": 2, "beta fact": 1}
    contents = ["alpha fact is here", "nothing relevant", "beta fact too"]
    sc = rc.score_query(contents, exp)
    assert sc["r@1"] == 1 and sc["r@3"] == 1
    assert sc["recall@3"] == 1.0, "两条期望事实都被命中 ⇒ recall 应为 1.0"
    assert sc["mrr"] == 1.0
    # 一条行只命中一条期望事实时，不得把两条都算进去
    sc2 = rc.score_query(["alpha fact only"], exp)
    assert sc2["recall@1"] == 0.5, sc2
