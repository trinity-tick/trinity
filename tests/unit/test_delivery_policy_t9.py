# -*- coding: utf-8 -*-
"""T9 投递层回路 v2（覆盖优先）单测。

判据（全部是**可证伪的行为**，不是"跑过了"）：
  ① 门控 off ⇒ `apply_coverage_policy` 返回**同一个对象**（`is` 断言）⇒
     引擎行为与改动前逐字节等价（回滚杠杆是真的）；
  ② 门控 on + 近期已投过 ⇒ 换进"近期没投过"的条目，且 `sources` 不下降；
  ③ 冷通道配额**不被轮换挤掉**（冷条目优先占位）；
  ④ 条数 ≤ top_k、token ≤ 预算（超限整行丢弃，不截半行）；
  ⑤ 失败一律 fail-open：冷集对象抛异常 / 账本缺失 / 入参畸形 ⇒ 原样返回、不抛；
  ⑥ 白名单纪律：候选池里非白名单来源**不得**被换进上下文（不新增注入面）。
"""
from __future__ import annotations

import os

import pytest

from trinity.bridges import delivery_policy as dp


def _item(mid: str, content: str = "", cat: str = "general", **extra) -> dict:
    # 正文必须过 `opening_surface.cold_text_ok`（≥40 字、非 enc:v1: 密文），
    # 否则夹具本身不合法 —— 那会把"夹具标定错误"读成"策略缺陷"。
    row = {"memory_id": mid, "content": content or (f"正文-{mid}-" * 12), "category": cat}
    row.update(extra)
    return row


def _surface(ids, md="相关记忆（开场浮现，仅作参考语境）:\n1. [general] x\n", cold=()):
    return {"surface_md": md, "sources": len(ids), "delivered_ids": list(ids),
            "cold_ids": list(cold), "cold_sources": len(list(cold))}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (dp.POLICY_GATE, dp.NOVEL_SLOTS_ENV, dp.RECENT_WINDOW_ENV,
              dp.TOKEN_BUDGET_ENV, dp.COLD_SLOTS_V2_ENV, dp.EXPLAIN_IN_BLOCK_ENV):
        monkeypatch.delenv(k, raising=False)
    yield


def test_gate_off_returns_same_object(monkeypatch):
    monkeypatch.setenv(dp.POLICY_GATE, "off")
    surf = _surface(["a", "b", "c"])
    out = dp.apply_coverage_policy(surf, pool=[_item("a"), _item("d")], top_k=5)
    assert out is surf, "门控 off 必须返回**同一对象**（逐字节等价）"
    assert dp.cold_slots_v2(0, 5) == 0, "门控 off 时冷配额必须原样返回 base"


def test_gate_on_swaps_recent_for_novel(monkeypatch):
    monkeypatch.setenv(dp.POLICY_GATE, "on")
    monkeypatch.setenv(dp.NOVEL_SLOTS_ENV, "2")
    # 近 24h 内 a/b/c 都投过 ⇒ novel 格必须换人
    recent = {"a": 3, "b": 3, "c": 3}
    surf = _surface(["a", "b", "c"])
    pool = [_item(m) for m in ("a", "b", "c", "d", "e", "f")]
    out = dp.apply_coverage_policy(surf, pool=pool, top_k=3, recent=recent)
    ids = out["delivered_ids"]
    assert out["sources"] == 3, "sources 不得下降（预留而非追加）"
    assert len(ids) == 3
    assert "d" in ids and "e" in ids, f"novel 格没换进新条目: {ids}"
    plan = out["delivery_plan"]
    assert plan["policy"] == "coverage-first/v2"
    assert plan["recent_ids_seen"] == 3
    whys = {it["memory_id"]: it["why"] for it in plan["items"]}
    assert whys["d"] == "novel" and whys["e"] == "novel"


def test_cold_slot_survives_rotation(monkeypatch):
    monkeypatch.setenv(dp.POLICY_GATE, "on")
    monkeypatch.setenv(dp.NOVEL_SLOTS_ENV, "2")
    surf = _surface(["a", "b", "cold1"], cold=["cold1"])
    pool = [_item("a"), _item("b"), _item("d"), _item("e")]
    picks = [_item("cold1", count=1), _item("cold2", count=1)]
    out = dp.apply_coverage_policy(surf, pool=pool, cold_candidates=picks, top_k=3,
                                   recent={"a": 1, "b": 1, "d": 1, "e": 1})
    assert "cold1" in out["delivered_ids"], "冷配额被轮换挤掉了（把病治反了）"
    assert out["delivery_plan"]["cold_ids"] == ["cold1"]


def test_bounded_slots_and_tokens(monkeypatch):
    monkeypatch.setenv(dp.POLICY_GATE, "on")
    monkeypatch.setenv(dp.TOKEN_BUDGET_ENV, "60")   # 极紧 ⇒ 必须丢行
    surf = _surface(["a", "b", "c", "d", "e"])
    pool = [_item(m, content="很长的正文" * 40) for m in ("a", "b", "c", "d", "e")]
    out = dp.apply_coverage_policy(surf, pool=pool, top_k=5, recent={})
    assert out["sources"] <= 5, "条数不得超过 top_k"
    assert dp._estimate_tokens(out["surface_md"]) <= 60, "token 预算没守住"
    assert out["delivery_plan"]["dropped_for_budget"], "超限必须记 dropped_for_budget"


def test_fail_open_on_errors(monkeypatch):
    monkeypatch.setenv(dp.POLICY_GATE, "on")

    class Boom:                      # 冷集对象本身抛异常
        def is_cold(self, _mid):
            raise RuntimeError("boom")

        def mark_touched(self, _ids):
            raise RuntimeError("boom")

    surf = _surface(["a", "b"])
    out = dp.apply_coverage_policy(surf, pool=[_item("a"), _item("c")], top_k=2,
                                   cold_set=Boom())
    assert out["sources"] == 2, "冷集抛异常不得让投递变空（fail-open）"

    class BadPool:                   # 畸形候选（不可迭代/非 dict）
        def __iter__(self):
            raise RuntimeError("boom")

    out2 = dp.apply_coverage_policy(_surface(["a"]), pool=BadPool(), top_k=2)
    assert out2["sources"] == 1
    # 空面不得被"制造"出内容
    empty = {"surface_md": "", "sources": 0, "delivered_ids": []}
    assert dp.apply_coverage_policy(empty, pool=[_item("a")], top_k=5) is empty


def test_recent_counts_reader_tolerates_missing_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(dp.POLICY_GATE, "on")
    missing = tmp_path / "nope.jsonl"
    assert dp.recent_delivery_counts(path=str(missing)) == {}
    good = tmp_path / "l.jsonl"
    good.write_text(
        '{"ts": 1000.0, "origin": "dsh-plugin", "ids": ["a", "b"]}\n'
        '{"ts": 1000.0, "origin": "probe:x", "ids": ["c"]}\n'
        '{"ts": 1.0, "origin": "dsh-plugin", "ids": ["old"]}\n', encoding="utf-8")
    now = 1000.0
    got = dp.recent_delivery_counts(window_s=10, path=str(good), now=now)
    assert got == {"a": 1, "b": 1, "c": 1}, "窗口外/坏行处理不对"
    got2 = dp.recent_delivery_counts(window_s=10, path=str(good), now=now,
                                     skip_origins=["probe:x"])
    assert "c" not in got2, "探针来源必须可跳过（生产读数不被夹具污染）"


def test_allowlist_discipline(monkeypatch):
    """候选池里的非白名单来源不得被换进上下文（不新增注入面）。

    非生产命名空间的判据在 `evidence_gate.atlas_source_allowed`：看的是
    `agent_id` / `persona_id` / `category` / `source_uri`（**不是** 自定义的 `source` 键）
    —— 夹具必须用真判据认的字段，否则测的是空气。
    """
    monkeypatch.setenv(dp.POLICY_GATE, "on")
    monkeypatch.setenv(dp.NOVEL_SLOTS_ENV, "2")
    surf = _surface(["a", "b", "c"])
    bad = _item("evil", agent_id="eval-bench")
    pool = [_item("a"), _item("b"), _item("c"), bad, _item("ok1"), _item("ok2")]
    out = dp.apply_coverage_policy(surf, pool=pool, top_k=3, recent={"a": 1, "b": 1, "c": 1})
    assert "evil" not in out["delivered_ids"], "白名单外的条目被策略放进了上下文"
    assert "ok1" in out["delivered_ids"] or "ok2" in out["delivered_ids"], "白名单内的候选没被换进来"


def test_cold_slots_v2_only_when_on(monkeypatch):
    monkeypatch.setenv(dp.COLD_SLOTS_V2_ENV, "2")
    monkeypatch.setenv(dp.POLICY_GATE, "off")
    assert dp.cold_slots_v2(0, 5) == 0
    monkeypatch.setenv(dp.POLICY_GATE, "on")
    assert dp.cold_slots_v2(0, 5) == 2
    assert dp.cold_slots_v2(2, 5) == 2, "不得降低既有的冷配额（只做 max）"
    assert dp.cold_slots_v2(0, 1) == 1, "冷配额不得超过 top_k"
