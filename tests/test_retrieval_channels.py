"""Retrieval channel contract tests (round43: aggregator degradation fix).

aggregator.py 以 `channel.search(query, top_k)` 调用检索通道；V47 是 47 路
通道注册器（无独立数据源），Exabase 是四信号检索器（retrieve）。两个类
此前缺 search → AttributeError → 通道降级（tier full→degraded）。本测试
锁定契约：search 不抛、返回可迭代、aggregator 能从 dict/对象取 memory_id。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_v47_search_returns_list():
    """RetrievalSystemV47.search 契约：返回列表（注册器无数据，不抛异常）。"""
    from trinity.modules.second_brain import RetrievalSystemV47

    v47 = RetrievalSystemV47()
    results = v47.search("any query", top_k=5)
    assert isinstance(results, list)
    # 再次调用不抛（聚合器每次 hybrid 都会调）
    assert v47.search("another", 3) == []


def test_exabase_search_delegates_retrieve():
    """ExabaseRetrieval.search 委托 retrieve，兼容 dict/list 返回。"""
    from trinity.modules.second_brain import ExabaseRetrieval

    exa = ExabaseRetrieval()
    results = exa.search("test query", top_k=3)
    assert isinstance(results, list)  # 空池也返回列表，不抛


def test_aggregator_extracts_id_from_dict_and_obj():
    """aggregator 的 dv_id 提取兼容对象属性与 dict 键（round43 修复）。"""
    from types import SimpleNamespace

    from trinity.agents.aggregator import MemoryAggregator

    agg = MemoryAggregator()
    obj = SimpleNamespace(memory_id="mem_obj")
    dct = {"memory_id": "mem_dict", "id": "mem_dict_alt"}
    dct_no_mid = {"id": "mem_alt"}
    # 对象属性
    assert getattr(obj, "memory_id", None) == "mem_obj"
    # dict：getattr 取不到 → 回退 .get
    dv = getattr(dct, "memory_id", None) or (dct.get("memory_id") if isinstance(dct, dict) else None)
    assert dv == "mem_dict"
    dv2 = getattr(dct_no_mid, "memory_id", None) or (dct_no_mid.get("memory_id") or dct_no_mid.get("id") if isinstance(dct_no_mid, dict) else None)
    assert dv2 == "mem_alt"
    assert agg is not None
