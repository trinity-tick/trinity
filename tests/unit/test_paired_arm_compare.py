# -*- coding: utf-8 -*-
"""两臂配对比较（McNemar 精确检验）判据测试。

钉住的失败形态：
1. **不一致对为 0 ⇒ INCONCLUSIVE**，**不得**当作"无差异"（"一致"最常见的来源就是"两边都没有"）。
2. **p >= 0.05 ⇒ NOT_SIGNIFICANT**，不得据此改默认。
3. **McNemar 必须只看不一致对**：`both_hit` / `neither` 增加时 p 不得变化
   （若把一致对也算进去，样本量会虚增、p 会假性变小 —— 这是配对检验最经典的错法）。
4. `hit()` 必须按**文档级去重后的前 k** 判定（与评测脚本同口径）。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "paired_arm_compare.py")
    spec = importlib.util.spec_from_file_location("paired_arm_compare", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["paired_arm_compare"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ---- mcnemar_exact -----------------------------------------------------------

def test_mcnemar_symmetric_and_bounded():
    assert mod.mcnemar_exact(5, 5) == 1.0, "对称时不可能显著"
    assert 0.0 <= mod.mcnemar_exact(1, 9) <= 1.0
    assert mod.mcnemar_exact(0, 0) == 1.0


def test_mcnemar_more_discordant_means_smaller_p():
    assert mod.mcnemar_exact(2, 20) < mod.mcnemar_exact(8, 20), "不平衡度越大 p 越小"


def test_mcnemar_known_value():
    """b=9,c=1（n=10）双侧精确 p = 2 * (C(10,0)+C(10,1))/2^10 = 2*11/1024。"""
    assert mod.mcnemar_exact(9, 1) == pytest.approx(2 * 11 / 1024, abs=1e-9)


# ---- wilson / hit ------------------------------------------------------------

def test_wilson_ci():
    lo, hi = mod.wilson_ci(153, 250)
    assert 0.0 < lo < 0.612 < hi < 1.0
    assert mod.wilson_ci(0, 0) == (0.0, 0.0)


def test_hit_dedups_before_truncating():
    """去重**先于**截断：重复项不占位，目标位置按去重后的序算。

    构造：前 10 位是不同文档，目标在第 11、12 位（重复）⇒
    去重后目标落在第 **11** 位 ⇒ k=10 不命中、k=11 命中。
    （初版测试写错了：用 `["x"]*9` 会让去重后只剩 2 条，目标其实在第 2 位。）
    """
    pages = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "t", "t"]
    assert mod.hit(pages, "t", 10) == 0, "去重后目标在第 11 位，k=10 不该命中"
    assert mod.hit(pages, "t", 11) == 1
    # 反向：重复项不该把目标往后挤
    dup = ["x", "x", "x", "t"]
    assert mod.hit(dup, "t", 2) == 1, "去重后 t 在第 2 位，k=2 必须命中"


# ---- judge -------------------------------------------------------------------

def _cmp(a_only, b_only, both=0, neither=0, arm_a="A2", arm_b="B2"):
    n = both + a_only + b_only + neither
    return {"arm_a": arm_a, "arm_b": arm_b, "k": 10, "n_paired": n,
            "both_hit": both, "a_only": a_only, "b_only": b_only, "neither": neither,
            "a_R@k": 0.0, "b_R@k": 0.0, "delta_b_minus_a": 0.0}


def test_judge_inconclusive_when_no_discordant():
    v = mod.judge(_cmp(0, 0, both=100))
    assert v["verdict"] == "INCONCLUSIVE"
    assert "不得当作无差异" in v["why"]


def test_judge_not_significant_on_thin_evidence():
    v = mod.judge(_cmp(3, 7))
    assert v["verdict"] == "NOT_SIGNIFICANT", v
    assert "不得据此改默认" in v["why"]


def test_judge_significant_when_clearly_lopsided():
    v = mod.judge(_cmp(2, 40))
    assert v["verdict"] == "SIGNIFICANT" and v["mcnemar_p"] < 0.05


def test_consistent_pairs_do_not_change_p():
    """**关键**：加 `both_hit` / `neither` 不得改变 p（否则 p 会假性变小）。"""
    p1 = mod.judge(_cmp(2, 20))["mcnemar_p"]
    p2 = mod.judge(_cmp(2, 20, both=100, neither=100))["mcnemar_p"]
    assert p1 == p2, "一致对把样本量算进去 = 配对检验的经典错法"


def test_compare_end_to_end_and_direction():
    results = [
        {"target": "a.md", "A2": {"pages": ["a.md"]}, "B2": {"pages": ["z.md"]}},   # A2 only
        {"target": "b.md", "A2": {"pages": ["z.md"]}, "B2": {"pages": ["b.md"]}},   # B2 only
        {"target": "c.md", "A2": {"pages": ["c.md"]}, "B2": {"pages": ["c.md"]}},   # both
    ]
    c = mod.compare(results, "A2", "B2", 10)
    assert c["n_paired"] == 3 and c["both_hit"] == 1
    assert c["a_only"] == 1 and c["b_only"] == 1
    # 逐题是二值、汇总比例会四舍五入到 4 位 ⇒ 用绝对容差（初版用默认 rel=1e-6 会假红）
    assert c["a_R@k"] == pytest.approx(2 / 3, abs=1e-3)
    assert c["delta_b_minus_a"] == pytest.approx(0.0, abs=1e-3)


def test_compare_skips_rows_missing_arms():
    results = [{"target": "a.md", "A2": {"pages": ["a.md"]}}]
    c = mod.compare(results, "A2", "B2", 10)
    assert c["n_paired"] == 0, "缺臂的行不得计入配对"
