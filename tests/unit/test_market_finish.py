# -*- coding: utf-8 -*-
"""2026-08-22 收尾：市场冷启动建议落地验证。

覆盖：
  - ReputationEngine 最小信任种子（建议②）：默认 0 行为不变；构造参数/env
    TRINITY_REPUTATION_SEED 启用；种子随 activity_bonus 衰减；[0,1] 钳制。
  - OrderBook.best_ask 最优卖价原语（建议③基石）：空簿 None、最低价命中、
    下架排除。
"""
import os

import pytest

os.environ.setdefault("TRINITY_TESTING", "1")

from trinity.market.memory_asset import MemoryAsset  # noqa: E402
from trinity.market.orderbook import OrderBook  # noqa: E402
from trinity.market.reputation import ReputationEngine, ReputationEntry  # noqa: E402


# ── 声誉种子（建议②）──────────────────────────────────────────────

def _zero_history_engine(seed=None):
    return ReputationEngine(seed=seed) if seed is not None else ReputationEngine()


def test_reputation_seed_default_zero_preserves_baseline():
    eng = _zero_history_engine()
    assert eng.seed == 0.0
    score = eng.calculate_reputation("new_seller").score
    assert score == 0.0  # 零历史 + 无种子 = 0（与改造前一致）


def test_reputation_seed_constructor_optin():
    eng = ReputationEngine(seed=0.3)
    assert eng.seed == 0.3
    score = eng.calculate_reputation("new_seller").score
    assert score == pytest.approx(0.3, abs=1e-4)  # 零历史活动系数 1.0 → 种子即分


def test_reputation_seed_env_optin(monkeypatch):
    monkeypatch.setenv("TRINITY_REPUTATION_SEED", "0.2")
    eng = ReputationEngine()
    assert eng.seed == pytest.approx(0.2)
    score = eng.calculate_reputation("new_seller").score
    assert score == pytest.approx(0.2, abs=1e-4)
    monkeypatch.delenv("TRINITY_REPUTATION_SEED", raising=False)
    assert ReputationEngine().seed == 0.0  # 环境还原后默认 0


def test_reputation_seed_clamped():
    assert ReputationEngine(seed=-1).seed == 0.0
    assert ReputationEngine(seed=2).seed == 1.0


def test_reputation_seed_decays_with_inactivity():
    from datetime import datetime, timedelta, timezone
    eng = ReputationEngine(seed=0.5)
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    # 中性事件：不计入 endorse/report/audit，仅提供"最后活跃时间"
    eng._ledger["old_agent"] = [
        ReputationEntry(
            event_id="e1", agent_id="old_agent", event_type="unknown_event",
            from_agent="x", reason="", timestamp=old_ts,
        )
    ]
    score = eng.calculate_reputation("old_agent").score
    # 20 天 → activity_bonus ≈ 2^(-20/30) ≈ 0.63 → 种子被衰减：0 < score < 0.5
    assert 0.0 < score < 0.5


def test_reputation_seed_does_not_inflate_bad_agents():
    # 种子是下限加成而非上限；有差评时仍显著低于纯种子分
    eng = ReputationEngine(seed=0.5)
    eng._ledger["bad"] = [
        ReputationEntry(
            event_id="e1", agent_id="bad", event_type="report",
            from_agent="x", reason="", timestamp="2026-08-01T00:00:00+00:00",
        )
    ]
    eng._trade_stats["bad"] = {"success": 0, "fail": 3}
    score = eng.calculate_reputation("bad").score
    assert score < 0.3


# ── 最优卖价（建议③基石）──────────────────────────────────────────

def _asset(mid, owner="seller_a"):
    return MemoryAsset(memory_id=mid, owner_agent=owner, content_hash=f"h-{mid}")


def test_best_ask_empty_book_returns_none():
    book = OrderBook()
    assert book.best_ask() is None


def test_best_ask_returns_lowest_price():
    book = OrderBook()
    book.list_asset(_asset("m1"), price=5.0)
    book.list_asset(_asset("m2"), price=1.5)
    book.list_asset(_asset("m3"), price=3.0)
    best = book.best_ask()
    assert best is not None
    assert best["asset_id"] == "m2"
    assert best["price"] == 1.5


def test_best_ask_excludes_delisted():
    book = OrderBook()
    book.list_asset(_asset("m1"), price=1.0)
    book.list_asset(_asset("m2"), price=9.0)
    book.delist_asset("m1")
    best = book.best_ask()
    assert best["asset_id"] == "m2"
