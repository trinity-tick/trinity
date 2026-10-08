# -*- coding: utf-8 -*-
"""补齐（scoped top-up）的**收益/代价分解**判据测试（Step 2）。

## 为什么需要这组判据

题集每题只有一个 target（`eval/doc_golden_set*.json`）。在这个口径下：
- 补齐**只会**增加「target 出现在 top-k 里」的机会 ⇒ R@k 天然只升不降；
- 每补一条不是 target 的条目都是实打实的错，而 R@k **看不见**。

⇒ 旧口径（R@k + 条数）在数学上无法量到补齐的代价。实测（2026-10-05 冒烟，n=3）：
`on` 臂比 `off` 多返回 2.67 条、补入 8 条，而 **R@1/R@5/R@10 全部纹丝不动**
（0.6667 → 0.6667）—— 8 条补入全是纯稀释，而旧报告只会显示「补入 8 条」。

**判据本身要先能失败**（AGENTS.md §13.3/§13.5）：所以下面既有正例也有反例。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_mod():
    path = os.path.join(ROOT, "dsh-ops", "_v3_topup_ab.py")
    spec = importlib.util.spec_from_file_location("_v3_topup_ab", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_v3_topup_ab"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()


def _item(rank, fusion_count, topup_count, qid="q"):
    return {"id": qid, "rank": rank, "fusion_count": fusion_count,
            "topup_count": topup_count, "returned": fusion_count + topup_count,
            "qlen": 10, "target": "t.md"}


# ---- topup_decomposition：收益与代价必须分开 --------------------------------

def test_gained_when_target_only_in_topup_segment():
    """target 排在融合段之后 ⇒ 补齐真的救回了这一题（真收益）。"""
    d = mod.topup_decomposition([_item(rank=7, fusion_count=5, topup_count=5)], k=10)
    assert d["gained_queries"] == 1
    assert d["diluted_queries"] == 0
    assert d["diluted_entries"] == 0


def test_diluted_when_target_already_in_fusion():
    """target 已在融合段，补齐又追加 5 条无关项 ⇒ 零召回收益、5 条纯稀释。"""
    d = mod.topup_decomposition([_item(rank=2, fusion_count=5, topup_count=5)], k=10)
    assert d["gained_queries"] == 0
    assert d["diluted_queries"] == 1
    assert d["diluted_entries"] == 5


def test_no_dilution_when_topup_added_nothing():
    """补齐没动手（topup_count=0）⇒ 不算稀释，否则判据会把 off 臂也算成有代价。"""
    d = mod.topup_decomposition([_item(rank=2, fusion_count=5, topup_count=0)], k=10)
    assert d["diluted_queries"] == 0
    assert d["diluted_entries"] == 0


def test_missed_when_rank_beyond_k_or_absent():
    d = mod.topup_decomposition([_item(rank=None, fusion_count=5, topup_count=5),
                                 _item(rank=12, fusion_count=5, topup_count=5)], k=10)
    assert d["missed_queries"] == 2
    assert d["gained_queries"] == 0


def test_mixed_population_is_counted_separately():
    d = mod.topup_decomposition([
        _item(rank=1, fusion_count=4, topup_count=3),   # 融合命中 + 稀释 3
        _item(rank=6, fusion_count=4, topup_count=3),   # 补齐救回
        _item(rank=None, fusion_count=4, topup_count=3),  # 未命中
    ], k=10)
    assert (d["diluted_queries"], d["diluted_entries"]) == (1, 3)
    assert d["gained_queries"] == 1
    assert d["missed_queries"] == 1


# ---- 排序与稀释指标 ----------------------------------------------------------

def test_precision_drops_when_slots_are_padded():
    """同一命中率下，返回更多 ⇒ precision 更低。这就是稀释的度量。"""
    items = [_item(rank=1, fusion_count=3, topup_count=0) for _ in range(4)]
    p_before = mod.precision_at_k(items, 10)
    padded = [_item(rank=1, fusion_count=3, topup_count=7) for _ in range(4)]
    p_after = mod.precision_at_k(padded, 10)
    assert p_after == pytest.approx(p_before, abs=1e-9), (
        "precision@k 分母是 k 而不是返回条数 ⇒ 单目标下补位不改变它；"
        "本条钉住这个语义，避免有人误以为它会掉而据此下错结论")


def test_precision_at_k_definition():
    items = [_item(rank=1, fusion_count=1, topup_count=0),
             _item(rank=None, fusion_count=1, topup_count=0)]
    assert mod.precision_at_k(items, 5) == pytest.approx(1 / (2 * 5))


def test_ndcg_at_k_rewards_higher_rank():
    top = mod.ndcg_at_k([_item(rank=1, fusion_count=1, topup_count=0)], 5)
    low = mod.ndcg_at_k([_item(rank=4, fusion_count=1, topup_count=0)], 5)
    assert top == pytest.approx(1.0)
    assert low < top
    assert mod.ndcg_at_k([_item(rank=6, fusion_count=1, topup_count=0)], 5) == 0.0


# ---- 回归：本次冒烟实测的形态（补了 8 条、R@10 没动）------------------------

def test_smoke_shape_flags_pure_dilution():
    """n=3 冒烟的形态：3 题里 2 题命中且都在融合段、补齐各追加若干条。

    反事实（判据必须能失败）：若把 `diluted_queries` 错记成 `gained`，
    本测试的两条断言会同时红 —— 这是「收益/代价被混为一谈」的可执行证明。
    """
    on = [_item(rank=2, fusion_count=6, topup_count=3),
          _item(rank=1, fusion_count=6, topup_count=5),
          _item(rank=None, fusion_count=6, topup_count=0)]
    d = mod.topup_decomposition(on, k=10)
    assert d["gained_queries"] == 0, "本题集里没有一题是靠补齐救回的"
    assert d["diluted_entries"] == 8, "补入的 8 条全是无关项（与冒烟读数一致）"


def test_total_observed_reconciles_with_selfreport():
    """自洽性：观测到的补齐条目总量必须能与引擎自报的 added 对上。

    实测（2026-10-05 冒烟 n=3）引擎自报 added=8，而 `diluted_entries` 只有 4
    —— 差额落在「未命中题」上。若只报 diluted_entries，读者无法解释这 4 条差额，
    会误以为丢数据。本测试钉住「总量另报」这一条。
    反事实：删掉 total_topup_entries_observed 字段 ⇒ 本测试 KeyError 红。
    """
    on = [_item(rank=2, fusion_count=6, topup_count=2),   # 命中 → 稀释 2
          _item(rank=1, fusion_count=6, topup_count=2),   # 命中 → 稀释 2
          _item(rank=None, fusion_count=6, topup_count=4)]  # 未命中 → 4 条不计入稀释
    d = mod.topup_decomposition(on, k=10)
    assert d["diluted_entries"] == 4
    assert d["total_topup_entries_observed"] == 8, "总量必须含未命中题的补位"
    assert d["total_topup_entries_observed"] >= d["diluted_entries"]
