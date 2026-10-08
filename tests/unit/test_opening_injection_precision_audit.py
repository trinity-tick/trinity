# -*- coding: utf-8 -*-
"""注入路径 nDCG/精确率闸门（Step 2 的另一半）判据测试。

## 为什么这组判据必须存在

2026-10-05 源码级实测：**注入路径与 top-up 不是同一条路** ——
`_opening → _search → engine.search(mode="hybrid")`（**自带 FTS 兜底**），
而 `_scoped_topup` 住在 `Trinity.search_hybrid()`（REST hybrid，无兜底）。
⇒ 为 top-up 加的 nDCG 判据**覆盖不到注入路径**。本文件的判据补这一半。

## 钉住的失败形态

1. **补位换命中率**：`R@k` 上升**且** `precision@k` 下降 ⇒ 必须 FAIL
   （与 Step 2 在 top-up 上抓到的同一形态）。
2. **基线缺失不得当作通过** ⇒ INCONCLUSIVE（rc=2），不是 PASS。
3. **precision 跌破基线 − 容差** ⇒ FAIL。
4. 反例（判据必须能失败）：与基线相同的候选必须 PASS，被"多塞无关条"的候选必须 FAIL。
"""
from __future__ import annotations

import importlib.util
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "opening_injection_precision_audit.py")
    spec = importlib.util.spec_from_file_location("opening_injection_precision_audit", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["opening_injection_precision_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ---- 纯函数 ------------------------------------------------------------------

def test_ndcg_at_rank_matches_formula():
    assert mod.ndcg_at_rank(1, 5) == pytest.approx(1.0)
    assert mod.ndcg_at_rank(2, 5) == pytest.approx(1 / math.log2(3))
    assert mod.ndcg_at_rank(5, 5) == pytest.approx(1 / math.log2(6))
    assert mod.ndcg_at_rank(6, 5) == 0.0, "超出 k 不得计分"
    assert mod.ndcg_at_rank(None, 5) == 0.0


def test_precision_at_k_dilution_semantics():
    assert mod.precision_at_k(["t"], "t", 5) == pytest.approx(0.2)
    assert mod.precision_at_k(["a", "b"], "t", 5) == 0.0
    assert mod.precision_at_k([], "t", 5) == 0.0
    assert mod.precision_at_k(["t"], "t", 0) == 0.0


def test_rank_of():
    assert mod.rank_of(["a", "t", "b"], "t") == 2
    assert mod.rank_of(["a", "b"], "t") is None
    assert mod.rank_of([], "t") is None


def test_summarize_aggregates():
    per = [{"rank": 1, "precision": 0.2, "ndcg": 1.0, "injected": 5},
           {"rank": None, "precision": 0.0, "ndcg": 0.0, "injected": 5}]
    s = mod.summarize(per, 5)
    assert s["n"] == 2 and s["R@5"] == 0.5
    assert s["precision@5"] == pytest.approx(0.1)
    assert s["nDCG@5"] == pytest.approx(0.5)
    assert s["avg_injected"] == 5.0


def test_summarize_empty_is_zeros_not_crash():
    s = mod.summarize([], 5)
    assert s["n"] == 0 and s["R@5"] == 0.0


def test_summarize_keys_are_parameterised_by_k():
    """键名必须随 k 变 —— 否则 judge 与 summarize 会「各自 0.0」地对不上。"""
    assert "R@7" in mod.summarize([], 7) and "precision@7" in mod.summarize([], 7)


# ---- 契约测试：**这条是用来抓假绿闸门的** ------------------------------------

def test_judge_reads_the_keys_summarize_emits():
    """集成契约：`judge` 必须读到 `summarize` **实际发出**的键。

    2026-10-05 实测代价（本工具自己的缺陷）：初版 `summarize` 发 `R@k`/`precision@k`，
    而 `judge` 读 `R@5`/`precision@5`，两边都用 `.get(..., 0.0)` 兜底 ⇒
    **闸门恒判 PASS**（"0.0000 >= 基线 0.0000"），一个永远绿的假闸门。
    单测 `judge`（手工构造正确键名）抓不到它 —— 只有把两个组件接起来才暴露。
    """
    base_per = [{"rank": 1, "precision": 0.2, "ndcg": 1.0, "injected": 5}]
    worse_per = [{"rank": None, "precision": 0.0, "ndcg": 0.0, "injected": 5}]
    v = mod.judge(mod.summarize(worse_per, 5), mod.summarize(base_per, 5), 0.0, 5)
    assert v["verdict"] == "FAIL", (
        "真实退化（命中从 1 变 0）必须判 FAIL —— 若这里 PASS，说明键名又对不上了")


def test_judge_is_inconclusive_not_pass_on_missing_keys():
    """缺指标键 ⇒ INCONCLUSIVE，**不得**因为 `.get(..., 0.0)` 兜底而假绿。"""
    v = mod.judge({"n": 1}, {"n": 1}, 0.0, 5)
    assert v["verdict"] == "INCONCLUSIVE"
    assert "契约" in v["why"] or "缺指标键" in v["why"]


def test_judge_inconclusive_when_candidate_lacks_keys():
    v = mod.judge({"R@5": 0.5}, {"R@5": 0.5, "precision@5": 0.1}, 0.0, 5)
    assert v["verdict"] == "INCONCLUSIVE", "候选缺 precision 键不得当作通过"


# ---- 闸门：三条规则 + 反例 ---------------------------------------------------

BASE = {"R@5": 0.60, "precision@5": 0.12, "nDCG@5": 0.55}


def test_gate_inconclusive_without_baseline():
    assert mod.judge({"R@5": 0.6, "precision@5": 0.12}, {}, 0.0)["verdict"] == "INCONCLUSIVE"


def test_gate_fails_on_padding_signature():
    """R@k 上升 + precision@k 下降 = 补位换命中率 ⇒ 必须 FAIL。"""
    v = mod.judge({"R@5": 0.70, "precision@5": 0.10}, BASE, 0.0)
    assert v["verdict"] == "FAIL" and "补位" in v["why"]


def test_gate_fails_when_precision_below_tolerance():
    v = mod.judge({"R@5": 0.60, "precision@5": 0.05}, BASE, 0.0)
    assert v["verdict"] == "FAIL"


def test_gate_passes_when_precision_held():
    """反例：precision 不降（R 也不涨）⇒ 必须 PASS，否则闸门没有判别力。"""
    assert mod.judge(dict(BASE), BASE, 0.0)["verdict"] == "PASS"
    assert mod.judge({"R@5": 0.70, "precision@5": 0.12}, BASE, 0.0)["verdict"] == "PASS", \
        "R 上升但 precision 未降 ⇒ 是真实改善，不得判红"


def test_gate_tolerance_is_honoured():
    c = {"R@5": 0.60, "precision@5": 0.11}
    assert mod.judge(c, BASE, 0.0)["verdict"] == "FAIL"
    assert mod.judge(c, BASE, 0.02)["verdict"] == "PASS", "容差必须真的生效"


# ---- 与 top-up 判据同源（同一把尺子）----------------------------------------

def test_same_yardstick_as_topup_criterion():
    """本工具的 precision@k 必须与 Step 2 的 `precision_at_k` 同语义。

    反事实：若有人把分母从 k 改成"返回条数"，dilution 语义就没了，本测试红。
    """
    sys.path.insert(0, os.path.join(ROOT, "dsh-ops"))
    spec = importlib.util.spec_from_file_location(
        "_v3_topup_ab", os.path.join(ROOT, "dsh-ops", "_v3_topup_ab.py"))
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)
    per = [{"rank": 1, "fusion_count": 5, "topup_count": 0},
           {"rank": None, "fusion_count": 5, "topup_count": 0}]
    assert tp.precision_at_k(per, 10) == pytest.approx(1 / (2 * 10))
    assert mod.precision_at_k(["t", "x", "y", "z", "w"], "t", 10) == pytest.approx(1 / 10)
