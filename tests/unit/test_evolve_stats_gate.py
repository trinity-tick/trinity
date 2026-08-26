"""R8 P0: judge 校准 + A/B 配对统计决策门 (RESEARCH_ROUND8_SUMMARY).

Verifies:
  - _paired_stats: McNemar p + bootstrap CI 正确计算
  - 采纳条件（delta>0 且 CI 下界>0）在显著/不显著场景判对
  - judge_calibration._kappa: 一致性/不一致性场景
  - judge3 默认温度 0 + rubric 防长度压分
"""

import sys

sys.path.insert(0, "C:/Users/Administrator/trinity")
import os
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

import pytest

from scripts.evolve_ab import _paired_stats
from scripts.judge_calibration import _kappa


def test_paired_stats_no_improvement():
    """无差异：delta≈0，CI 不排除 0，p 大 → 不采纳。"""
    ids = [f"q{i}" for i in range(40)]
    base = set(ids[:30])          # 30/40 对
    exp = set(ids[:30])           # 同样 30/40
    s = _paired_stats(base, exp, ids)
    assert abs(s["delta"]) < 1e-9
    assert s["ci_low"] <= 0 <= s["ci_high"]
    assert s["mcnemar_p"] > 0.05


def test_paired_stats_clear_improvement():
    """显著改进：候选多对 8 题 → delta>0 且 CI 下界>0（40 题样本）。"""
    ids = [f"q{i}" for i in range(40)]
    base = set(ids[10:30])        # 20 对
    exp = set(ids[10:38])         # 28 对（+8）
    s = _paired_stats(base, exp, ids)
    assert s["delta"] == pytest.approx(0.2, abs=0.01)
    assert s["ci_low"] > 0        # bootstrap 下界为正
    assert s["mcnemar_p"] < 0.05


def test_paired_stats_small_noise_not_accepted():
    """小样本小差异：+1 题（2.5%）→ CI 下界可能 ≤0 → 不可靠。"""
    ids = [f"q{i}" for i in range(40)]
    base = set(ids[:20])
    exp = set(ids[:21])           # 仅 +1
    s = _paired_stats(base, exp, ids)
    # 至少不产生假阳性采纳（CI 下界 >0 概率低；delta 本身 < 阈值）
    assert s["delta"] < 0.05


def test_kappa_agreement():
    human = ["YES", "YES", "NO", "NO", "YES"] * 6
    judge = [True, True, False, False, True] * 6
    r = _kappa(human, judge)
    assert r["kappa"] == 1.0
    assert r["n"] == 30


def test_kappa_disagreement():
    human = ["YES", "YES", "NO", "NO", "YES"] * 6
    judge = [True, False, False, False, True] * 6  # 一处反转
    r = _kappa(human, judge)
    assert r["kappa"] < 1.0
    assert r["agreement"] < 1.0


def test_kappa_unsure_excluded():
    human = ["YES", "UNSURE", "NO", "UNSURE"]
    judge = [True, True, False, True]
    r = _kappa(human, judge)
    assert r["n"] == 2  # UNSURE 排除


def test_judge3_default_temp_zero():
    """judge3 默认温度 0（确定性判分）——直接读源码确认。"""
    src = open("C:/Users/Administrator/trinity/benchmark/judge3.py", encoding="utf-8").read()
    assert "default=0.0" in src or "default=0" in src


def test_judge3_rubric_no_length_bias():
    """rubric 含防长度压分条款。"""
    src = open("C:/Users/Administrator/trinity/benchmark/judge3.py", encoding="utf-8").read()
    assert "Do NOT penalize short answers" in src
    assert "Do NOT prefer longer responses" in src
