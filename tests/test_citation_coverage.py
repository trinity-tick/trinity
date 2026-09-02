# -*- coding: utf-8 -*-
"""Fable 5.1 对照审计 P1-③：citation-coverage 归因指标（2026-09-02，闭环 CH-1）。

LLM-free 部分单测：normalize/split_facts/extract_markers/classify_fact/accumulate。
"""
import pytest

from benchmark.citation_coverage import (accumulate, classify_fact,
                                         extract_markers, normalize, split_facts)


def test_normalize():
    assert normalize("  Business  Administration! ") == "businessadministration"
    assert normalize("我 2019 年毕业。") == "我2019年毕业"


def test_split_facts_en_sentences():
    f = split_facts("I studied CS. I graduated in 2019.")
    assert any("CS" in x for x in f) and any("2019" in x for x in f)


def test_split_facts_short_answer_single():
    f = split_facts("Business Administration")
    assert f == ["Business Administration"]


def test_split_facts_zh():
    f = split_facts("他本科读的计算机。后来转行做了物流。")
    assert len(f) >= 2


def test_extract_markers():
    s = "I like hiking [2] and coding [7]."
    m = extract_markers(s)
    assert m == [(s.find("[2]"), 2), (s.find("[7]"), 7)]


CTX = [
    {"content": "My degree is Business Administration.", "memory_id": "mem_a"},
    {"content": "用户偏好深色模式，且习惯周末爬山。", "memory_id": "mem_b"},
]


def test_classify_cited_with_supporting_marker():
    cl = classify_fact("Business Administration", "I graduated with Business Administration [1].", CTX)
    assert cl["covered"] and cl["cited"]
    assert cl["cited_evidence_id"] == "mem_a"


def test_classify_wrong_index_not_cited():
    cl = classify_fact("Business Administration", "I graduated with Business Administration [2].", CTX)
    assert cl["covered"] and not cl["cited"]  # [2] 的证据不含该事实


def test_classify_no_marker_covered_uncited():
    cl = classify_fact("Business Administration", "I graduated with Business Administration.", CTX)
    assert cl["covered"] and not cl["cited"]


def test_classify_not_covered():
    cl = classify_fact("I own a red car", "Business Administration.", CTX)
    assert not cl["covered"] and not cl["cited"]


def test_classify_zh_marker():
    cl = classify_fact("习惯周末爬山", "用户偏好深色模式且习惯周末爬山 [2]。", CTX)
    assert cl["covered"] and cl["cited"] and cl["cited_evidence_id"] == "mem_b"


def test_accumulate_metrics_math():
    qs = [
        {"category": "ku", "gold": "Business Administration",
         "answer": "Business Administration [1].", "contexts": CTX},
        {"category": "ku", "gold": "没有提到的知识",
         "answer": "", "contexts": CTX},
        {"category": "tr", "gold": "用户偏好深色模式。",
         "answer": "用户偏好深色模式 [2]。", "contexts": CTX},
    ]
    m = accumulate(qs)
    ku = m["per_category"]["ku"]
    # ku: 2 事实（1 覆盖+引用, 1 全miss）
    assert ku["facts"] == 2
    assert ku["answer_coverage"] == 0.5 and ku["citation_coverage"] == 0.5
    assert ku["citation_rate"] == 1.0
    tr = m["per_category"]["tr"]
    assert tr["citation_rate"] == 1.0 and tr["citation_coverage"] == 1.0
    tot = m["totals"]
    assert tot["facts"] == 3
    assert tot["citation_coverage"] == round(2 / 3, 4)
    assert tot["answer_coverage"] == round(2 / 3, 4)


def test_accumulate_empty_safe():
    m = accumulate([])
    assert m["totals"]["citation_coverage"] == 0.0
