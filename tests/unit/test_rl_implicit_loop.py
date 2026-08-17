"""Trinity — RL 隐式反馈闭环测试（2026-08-17）。

覆盖：
- hybrid 查询命中 → top 记忆自动获得 IMPLICIT_USE（Q 值提升，检索→使用→反馈闭环）
- 每记忆每进程只奖励一次（防通胀）
- 空结果/无 RL scorer/env 关闭 均优雅
"""

from __future__ import annotations

import os

import pytest

from trinity.agents.aggregator import MemoryAggregator


@pytest.fixture()
def agg() -> MemoryAggregator:
    a = MemoryAggregator(persist_path=None)
    yield a
    a.shutdown()


def _hybrid(agg: MemoryAggregator, q: str, limit: int = 10):
    return agg.query({}, limit=limit, mode="hybrid", query_text=q)


def test_implicit_use_rewards_top_hits(agg: MemoryAggregator, monkeypatch) -> None:
    """hybrid 查询后 top 记忆 Q 值上升（自动闭环）。"""
    dv = agg.ingest("数据库 PostgreSQL 连接配置", "eng", {"category": "db", "importance": 0.8})
    monkeypatch.delenv("TRINITY_RL_SCORER", raising=False)
    results = _hybrid(agg, "数据库 PostgreSQL")
    assert any(r.memory_id == dv.memory_id for r in results[:3])
    state = agg._rl_scorer._states.get(dv.memory_id)
    assert state is not None and state.try_count >= 1
    assert state.q_value > 0.5  # IMPLICIT_USE 0.05 → Q ~0.55


def test_implicit_use_throttled_once(agg: MemoryAggregator, monkeypatch) -> None:
    """同一记忆重复查询只奖励一次（防通胀）。"""
    dv = agg.ingest("配置项 X", "eng", {"category": "db"})
    monkeypatch.delenv("TRINITY_RL_SCORER", raising=False)
    _hybrid(agg, "配置项 X")
    n1 = agg._rl_scorer._states[dv.memory_id].try_count
    r = agg.rl_implicit_use([dv.memory_id], limit=3)
    assert r == 0  # 已奖励过，去重
    assert agg._rl_scorer._states[dv.memory_id].try_count == n1


def test_implicit_use_empty_no_crash(agg: MemoryAggregator) -> None:
    assert agg.rl_implicit_use([], limit=3) == 0
    assert agg.rl_implicit_use(None, limit=3) == 0


def test_implicit_use_no_scorer_returns_zero(agg: MemoryAggregator, monkeypatch) -> None:
    monkeypatch.setattr(agg, "_rl_scorer", None)
    assert agg.rl_implicit_use(["mem_x"], limit=3) == 0


def test_implicit_use_env_off_no_feed(agg: MemoryAggregator, monkeypatch) -> None:
    """TRINITY_RL_SCORER=off 时查询不打隐式反馈。"""
    dv = agg.ingest("环境关闭测试记忆", "eng", {"category": "x"})
    monkeypatch.setenv("TRINITY_RL_SCORER", "off")
    _hybrid(agg, "环境关闭测试")
    assert dv.memory_id not in agg._rl_scorer._states  # 未注册未奖励
