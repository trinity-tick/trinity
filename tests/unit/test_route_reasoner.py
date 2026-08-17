"""Trinity — RouteReasoner 产品化测试（2026-08-17）。

覆盖：
- 策略路由 route_for（multi→turn / temporal→temporal / pref→pref / 其他→plain）
- build_prompt 纯函数：turn 粒度 / temporal（REL 注入+时间线排序）/ pref 两段式 / plain
- answer() 完整管线（fake search + fake LLM）：按题型路由、pref NONE 处理
- 无 API key → 优雅 error（不崩溃）
- Trinity.reason 回退路径（TRINITY_ROUTE_REASONER=on 但无 key → OpenDomainReasoner）
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from trinity.qa.route_reasoner import (  # noqa: E402
    GEN_SYS_PLAIN,
    RouteReasoner,
    build_prompt,
    parse_date,
    route_for,
)


# ── 策略路由 ───────────────────────────────────────────────────────

def test_route_for() -> None:
    assert route_for("multi-session-reasoning") == "turn"
    assert route_for("temporal-reasoning") == "temporal"
    assert route_for("single-session-preference") == "pref"
    assert route_for("knowledge-update") == "plain"
    assert route_for(None) == "plain"
    assert route_for("") == "plain"


def test_parse_date() -> None:
    d = parse_date("2024/03/15")
    assert d is not None and d.day == 15
    assert parse_date("garbage") is None
    assert parse_date(None) is None


# ── build_prompt 纯函数 ────────────────────────────────────────────

def test_build_prompt_plain() -> None:
    ev = [{"content": "[DATE: 2024/01/01] [user] Alice 喜欢川菜"}]
    p = build_prompt("Alice 喜欢什么？", ev, "plain")
    assert p["system"] == GEN_SYS_PLAIN
    assert "Alice 喜欢川菜" in p["user"]
    assert "===SESSION===" in p["user"]


def test_build_prompt_turn_top16() -> None:
    ev = [{"content": f"[DATE: 2024/01/0{i}] [user] turn {i}"} for i in range(20)]
    p = build_prompt("q", ev, "turn", top_turns=16)
    assert p["user"].count("===TURN===") == 1
    # top_turns 限制：只保留前 16 条
    assert p["user"].count("turn ") == 16


def test_build_prompt_temporal_rel_injected_and_sorted() -> None:
    ev = [
        {"content": "[DATE: 2024/03/10] [user] 会议决定 X"},
        {"content": "[DATE: 2024/03/01] [user] 初步讨论 X"},
    ]
    p = build_prompt("X 何时决定？", ev, "temporal", question_date="2024/03/15")
    # REL 注入：两条都有相对天数
    assert "[REL: 5 days before question date]" in p["user"]
    assert "[REL: 14 days before question date]" in p["user"]
    # 时间线排序：早的在前（03/01 在 03/10 之前）
    assert p["user"].index("2024/03/01") < p["user"].index("2024/03/10")


def test_build_prompt_pref_stage1() -> None:
    ev = [{"content": "user: 我更喜欢深色模式"}]
    p = build_prompt("推荐主题？", ev, "pref")
    assert "preferences" in p["system"].lower() or "偏好" in p["system"]
    assert "user: 我更喜欢深色模式" in p["user"]


# ── answer() 管线（fake search + fake LLM）────────────────────────

class _FakeSearch:
    def __init__(self, results):
        self._results = results

    def __call__(self, query, top_k=5, agent_id=None, persona_id=None):
        return {"results": self._results[:top_k]}


def _fake_rr(search, chats=None):
    rr = RouteReasoner(search_fn=search, api_key="fake-key")
    calls = []

    def _chat(system, user, max_tokens=350, timeout=120):
        calls.append((system, user))
        return (chats.pop(0) if chats else "ANSWER")
    rr._chat = _chat
    rr._calls = calls
    return rr


def test_answer_routes_turn(monkeypatch) -> None:
    ev = [{"content": "[DATE: 2024/01/01] [user] 步骤一"}, {"content": "[DATE: 2024/01/02] [user] 步骤二"}]
    rr = _fake_rr(_FakeSearch(ev))
    out = rr.answer("怎么做？", qtype="multi-session-reasoning")
    assert out["strategy"] == "turn"
    assert out["answer"] == "ANSWER"
    assert out["n_evidence"] == 2


def test_answer_pref_two_stage(monkeypatch) -> None:
    ev = [{"content": "user: 我喜欢极简风格"}]
    rr = _fake_rr(_FakeSearch(ev), chats=["- 极简风格", "推荐极简主题"])
    out = rr.answer("推荐什么主题？", qtype="single-session-preference")
    assert out["strategy"] == "pref"
    assert len(rr._calls) == 2  # stage1 + stage2
    assert out["answer"] == "推荐极简主题"


def test_answer_pref_none_unknown(monkeypatch) -> None:
    ev = [{"content": "user: 今天天气不错"}]
    rr = _fake_rr(_FakeSearch(ev), chats=["NONE"])
    out = rr.answer("推荐什么？", qtype="single-session-preference")
    assert out["answer"] == "UNKNOWN"
    assert len(rr._calls) == 1  # NONE 短路，不调 stage2


def test_answer_no_api_key(monkeypatch) -> None:
    monkeypatch.setattr(RouteReasoner, "_load_key", staticmethod(lambda: None))
    rr = RouteReasoner(search_fn=_FakeSearch([{"content": "x"}]))
    out = rr.answer("q")
    assert out["error"] == "no api key"
    assert out["strategy"] == "plain"


def test_answer_empty_evidence_unknown() -> None:
    rr = _fake_rr(_FakeSearch([]))
    out = rr.answer("q", qtype="temporal-reasoning")
    assert out["answer"] == "UNKNOWN"
    assert out["strategy"] == "temporal"


# ── Trinity.reason 回退（无 key → OpenDomainReasoner 不崩溃）────────

def test_trinity_reason_fallback_without_key(monkeypatch, tmp_path) -> None:
    """TRINITY_ROUTE_REASONER=on 但无凭证 → 回退 OpenDomainReasoner（不崩溃）。"""
    from trinity.core.client import Trinity
    from trinity.qa.route_reasoner import RouteReasoner as RR

    monkeypatch.setattr(RR, "_load_key", staticmethod(lambda: None))  # 模拟无凭证
    monkeypatch.setenv("TRINITY_ROUTE_REASONER", "on")

    class _FakeReasoner:
        def answer(self, query, retriever=None, top_k=5):
            return {"response": "fallback:" + query}

        def answer_multi_hop(self, query, retriever=None, top_k=5):
            return {"response": "fallback-mh:" + query}

    monkeypatch.setattr(
        "trinity.modules.open_domain.reasoner.OpenDomainReasoner",
        lambda: _FakeReasoner(),
    )
    t = Trinity(adapter="sqlite", store_path=str(tmp_path / "r.db"))
    try:
        t._engine = object()  # 走本地 reasoner 分支（非 bridge）
        r = t.reason("capital of France?", top_k=3)
        assert r["response"] == "fallback:capital of France?"
    finally:
        t._adapter.disconnect()
