"""Trinity — R5 储备接入单元测试（2026-08-15）。

覆盖：
- Serendipity 探索通道（MemoryAggregator 初始化 + Wander 采样）
- SAGE 自进化图（Trinity.sage_ingest/query/evolve）
- DCPM 双过程（belief 修订链 + 夜间整合）
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from trinity.agents.aggregator import MemoryAggregator
from trinity.core.client import Trinity


def test_serendipity_channel_active() -> None:
    agg = MemoryAggregator(persist_path=None)
    assert agg._serendipity is not None
    assert agg._serendipity_bridge is not None
    agg.shutdown()


def test_serendipity_wander_samples() -> None:
    agg = MemoryAggregator(persist_path=None)
    class H:
        def __init__(self, rel):
            self.relevance = rel
            self.mode = None
            self.serendipity_score = 0.0
    hits = [H(0.9), H(0.1), H(0.2), H(0.05)]
    wandered = agg._serendipity.wander(hits)
    assert 0 < len(wandered) <= 3  # 温度采样探索（sample_count=3）
    agg.shutdown()


def test_serendipity_in_hybrid() -> None:
    """hybrid 查询引入探索通道（低相关记忆可能被采样）。"""
    agg = MemoryAggregator(persist_path=None)
    for i in range(4):
        agg.ingest(f"数据库配置 {i}", "eng", {"category": "db", "importance": 0.8})
    for i in range(4):
        agg.ingest(f"旅行分享 {i}", "main", {"category": "life", "importance": 0.2})
    try:
        agg._rebuild_index()
    except Exception:
        pass
    results = agg.query({}, limit=10, mode="hybrid", query_text="PostgreSQL 配置")
    assert isinstance(results, list)
    agg.shutdown()


def test_sage_lifecycle() -> None:
    t = Trinity(store_path=os.path.join(tempfile.mkdtemp(), "s.db"))
    assert t.sage is not None
    r1 = t.sage_ingest("Alice 负责 Trinity 记忆模块，使用 PostgreSQL")
    assert r1.get("sage") is True
    assert r1.get("entities_written", 0) >= 1
    q = t.sage_query("Trinity 模块")
    assert q.get("sage") is True
    ev = t.sage_evolve()
    assert ev.get("sage") is True


def test_dcpm_lifecycle() -> None:
    t = Trinity(store_path=os.path.join(tempfile.mkdtemp(), "d.db"))
    assert t.dcpm is not None
    r1 = t.dcpm_record_belief("trinity", "uses", "postgresql")
    assert r1.get("dcpm") is True
    assert r1.get("chain_len", 0) >= 1
    r2 = t.dcpm_record_belief("trinity", "uses", "sqlite",
                              superseded_by=r1.get("belief_id"))
    assert r2.get("chain_len", 0) >= 2  # 修订链
    cons = t.dcpm_consolidate()
    assert cons.get("dcpm") is True
    assert cons.get("schemas", 0) >= 1
