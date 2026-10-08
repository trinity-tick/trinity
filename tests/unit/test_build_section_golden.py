# -*- coding: utf-8 -*-
"""从文档结构生成题集（换生成输入的复现）判据测试。

钉住的失败形态：
1. **解析不出就返回空**，不许猜（由调用方分档计数，§13.2）。
2. `basename_of` 必须同时吃 Windows 反斜杠与正斜杠。
3. 组装出的题集必须是**字典 + provenance**（`doc_retrieval_eval.py:434` 读 `golden["items"]`；
   裸列表会 `TypeError: list indices must be integers` —— 上一轮已踩过一次）。
4. 缓存必须**可续跑**：`load_cache` 能读回已完成的文档。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "build_section_golden.py")
    spec = importlib.util.spec_from_file_location("build_section_golden", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_section_golden"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.mark.parametrize("src,expect", [
    (r"C:\a\b\X.md", "X.md"), ("C:/a/b/Y.md", "Y.md"), ("/t/Z.md", "Z.md"),
    (None, ""), ("", ""),
])
def test_basename_of(src, expect):
    assert mod.basename_of(src) == expect


def test_parse_json_array_capped_by_k():
    raw = '["怎么配置默认智能体", "如何排查持久层未启动", "怎样做库存对账", "多余的第四条"]'
    got = mod.parse_questions(raw, 3)
    assert got == ["怎么配置默认智能体", "如何排查持久层未启动", "怎样做库存对账"]


def test_parse_numbered_list():
    raw = "1. 怎么配置默认智能体\n2. 如何排查持久层未启动"
    assert mod.parse_questions(raw, 5) == ["怎么配置默认智能体", "如何排查持久层未启动"]


def test_parse_dedups_and_filters():
    raw = '["太短", "怎么配置默认智能体", "怎么配置默认智能体", "%s"]' % ("很" * 250)
    assert mod.parse_questions(raw, 5) == ["怎么配置默认智能体"]


def test_parse_returns_empty_on_garbage():
    assert mod.parse_questions("", 3) == []
    assert mod.parse_questions("```json\n{broken", 3) in ([], ["```json"])


def test_assemble_writes_dict_with_items(tmp_path, monkeypatch):
    """**关键回归**：必须是 dict + items —— 裸列表会让评测脚本 TypeError。"""
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))
    items = [{"id": "sec0000", "type": "section-task", "query": "q", "target": "A.md"}]
    out = mod.assemble("trinity-docs", items)
    d = json.load(open(out, encoding="utf-8"))
    assert isinstance(d, dict) and d["items"] == items
    assert "provenance" in d
    assert "NOT_fully_independent" in d["provenance"], \
        "必须显式声明它**不是**完全独立的复现"


def test_cache_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "CACHE", os.path.join(str(tmp_path), "c.jsonl"))
    mod.append_cache({"doc": "A.md", "questions": ["q1"]})
    mod.append_cache({"doc": "B.md", "questions": []})
    done = mod.load_cache()
    assert set(done) == {"A.md", "B.md"}
    assert done["A.md"]["questions"] == ["q1"]


def test_cache_skips_corrupt_lines(tmp_path, monkeypatch):
    p = os.path.join(str(tmp_path), "c.jsonl")
    monkeypatch.setattr(mod, "CACHE", p)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write('{"doc":"A.md","questions":["q"]}\n')
        fh.write("{not json}\n")
    assert set(mod.load_cache()) == {"A.md"}
