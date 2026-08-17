"""Trinity — R6 RL 记忆决策单元测试（2026-08-15）。

覆盖 MemoryAggregator._rl_scorer：
- RL scorer 初始化
- Q 值学习（正反馈升、负反馈降，纯 Q 值验证）
- rl_feedback 接口（返回 q_value）
- hybrid 排序微调不崩溃（inf 处理）
"""

from __future__ import annotations

import pytest

from trinity.agents.aggregator import MemoryAggregator


@pytest.fixture()
def agg() -> MemoryAggregator:
    a = MemoryAggregator(persist_path=None)
    yield a
    a.shutdown()


def test_rl_scorer_initialized(agg: MemoryAggregator) -> None:
    assert agg._rl_scorer is not None


def test_rl_q_learning_up(agg: MemoryAggregator) -> None:
    dv = agg.ingest("数据库 PostgreSQL 配置", "eng", {"category": "db", "importance": 0.8})
    rl = agg._rl_scorer
    rl.register_memory(dv.memory_id, semantic_score=0.8)
    q0 = rl._states[dv.memory_id].q_value
    for _ in range(5):
        agg.rl_feedback(dv.memory_id, positive=True)
    q_up = rl._states[dv.memory_id].q_value
    assert q_up > q0  # 正反馈提升 Q


def test_rl_q_learning_down(agg: MemoryAggregator) -> None:
    dv = agg.ingest("记忆 A", "eng", {"category": "x", "importance": 0.5})
    rl = agg._rl_scorer
    rl.register_memory(dv.memory_id, semantic_score=0.5)
    for _ in range(3):
        agg.rl_feedback(dv.memory_id, positive=True)
    q_high = rl._states[dv.memory_id].q_value
    agg.rl_feedback(dv.memory_id, positive=False)
    q_low = rl._states[dv.memory_id].q_value
    assert q_low < q_high  # 负反馈降低 Q


def test_rl_feedback_returns_q(agg: MemoryAggregator) -> None:
    dv = agg.ingest("记忆 B", "eng", {"category": "x"})
    rl = agg._rl_scorer
    rl.register_memory(dv.memory_id, semantic_score=0.6)
    r = agg.rl_feedback(dv.memory_id, positive=True)
    assert r["rl"] is True
    assert isinstance(r["q_value"], float)


def test_rl_rerank_does_not_crash(agg: MemoryAggregator) -> None:
    """hybrid 查询含 RL 排序微调不崩溃（含未尝试记忆 inf 处理）。"""
    for i in range(4):
        agg.ingest(f"数据库配置 {i}", "eng", {"category": "db", "importance": 0.8})
    try:
        agg._rebuild_index()
    except Exception:
        pass
    results = agg.query({}, limit=10, mode="hybrid", query_text="数据库")
    assert isinstance(results, list)
