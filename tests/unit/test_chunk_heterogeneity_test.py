# -*- coding: utf-8 -*-
"""chunk 数异质性检验 判据测试。

## 钉住的失败形态

1. **缺 chunk 信息的文档必须整篇丢弃**，不许猜、不许当 0（当 0 会把它塞进 low 组，
   而 low 组恰恰是本假设的关键对照）。
2. **切分必须按文档、不是按题目**：同一文档的题目不许跨组（否则组间不独立）。
3. `median` 的偶数长度行为要正确。
4. `bootstrap_delta_diff` **按文档整簇重采样**：文档内题目不独立，
   按题目重采样会低估方差 ⇒ 假显著。
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "chunk_heterogeneity_test.py")
    spec = importlib.util.spec_from_file_location("chunk_heterogeneity_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["chunk_heterogeneity_test"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def test_median_odd_and_even():
    assert mod.median([1, 3, 5]) == 3
    assert mod.median([1, 2, 3, 4]) == 2.5
    assert mod.median([]) == 0.0


def _rs(pairs):
    return [{"target": t, "A2": {"pages": ["x"]}, "B2": {"pages": ["x"]}} for t in pairs]


def test_split_by_chunks_keeps_documents_intact():
    """同一文档的题目必须全在同一组（按文档切，不按题切）。

    用 {1, 2, 50, 60} ⇒ 中位数 26 ⇒ low={a,c}、high={b,d}。
    （初版用 {1,2,50} 时中位数=2，`c.md`(2) 恰好**等于**中位数 ⇒ 按语义归 high，
    我的断言写错了 —— 实现是对的，见下一条边界测试。）
    """
    results = _rs(["a.md"] * 5 + ["b.md"] * 3 + ["c.md"] * 4 + ["d.md"] * 2)
    counts = {"a.md": 1, "b.md": 50, "c.md": 2, "d.md": 60}
    g = mod.split_by_chunks(results, counts, "median")
    low_docs = {r["target"] for r in g["low"]}
    high_docs = {r["target"] for r in g["high"]}
    assert low_docs & high_docs == set(), "文档不得跨组"
    assert low_docs == {"a.md", "c.md"}
    assert high_docs == {"b.md", "d.md"}


def test_median_boundary_goes_to_high():
    """语义钉死：**等于**中位数的文档归 high（low 是"严格低于中位数"）。"""
    results = _rs(["a.md", "b.md", "c.md"])
    counts = {"a.md": 1, "b.md": 2, "c.md": 50}      # 中位数 = 2
    g = mod.split_by_chunks(results, counts, "median")
    assert {r["target"] for r in g["low"]} == {"a.md"}
    assert {r["target"] for r in g["high"]} == {"b.md", "c.md"}, \
        "等于中位数的 b.md 应归 high"


def test_missing_chunk_info_is_discarded_not_guessed():
    """缺 chunk 信息 ⇒ 整篇丢弃并计数；**不得当 0**（0 会被塞进 low 组，污染对照）。"""
    results = _rs(["known_low.md", "known_high.md", "unknown.md"])
    counts = {"known_low.md": 1, "known_high.md": 99}
    g = mod.split_by_chunks(results, counts, "median")
    assert g["missing_docs"] == 1
    all_targets = {r["target"] for r in g["low"] + g["high"]}
    assert "unknown.md" not in all_targets, "无信息的文档必须被丢弃"


def test_split_empty_counts_is_safe():
    g = mod.split_by_chunks(_rs(["a.md"]), {}, "median")
    assert g["low"] == [] and g["high"] == [] and g["missing_docs"] == 1


def test_tertile_mode_drops_middle():
    counts = {"a.md": 1, "b.md": 2, "c.md": 3, "d.md": 4, "e.md": 5, "f.md": 6}
    results = _rs(list(counts))
    g = mod.split_by_chunks(results, counts, "tertile")
    mid = {"c.md", "d.md"}
    got_low = {r["target"] for r in g["low"]}
    got_high = {r["target"] for r in g["high"]}
    assert not (got_low & mid), "中间 1/3 不该进 low"
    assert not (got_high & mid), "中间 1/3 不该进 high"


def test_bootstrap_rejects_empty_groups():
    pac = mod._load_pac()
    r = mod.bootstrap_delta_diff(pac, [], [], {}, "A2", "B2", 10, n_boot=10)
    assert "error" in r
