# -*- coding: utf-8 -*-
"""T58/H2 判据：投递槽位「紧凑索引」的**有界 / 可解释 / 默认 off / 可回滚 / fail-open**。

设计要点（每条都能失败，且带负向或反事实）：
| # | 判据 | 反向 |
|---|---|---|
| D1 | 默认（env 未设）⇒ `apply_slot` 返回**入参同一对象**（`is`） | 回滚杠杆是真的 |
| D2 | `TRINITY_DELIVERY_SLOT=index` ⇒ 块换成索引：含 id、条数 ≤ top_k、**其余字段不动** | — |
| D3 | **有界**：预算压到 250 字符 ⇒ 超限**整行丢弃**（`truncated` 且有 `dropped_for_budget`），字符数 ≤ 预算 | 证明不是"截断半行" |
| D4 | **fail-open**：没有条目/条目无正文 ⇒ 返回入参（**不制造空面**） | 反事实 |
| D5 | **回滚实测**：`TRINITY_DELIVERY_SLOT=full` ⇒ 又是入参同一对象 | 回滚不需改代码 |
| D6 | **可解释**：每条带 `memory_id`/`position`，块内 id 与 `delivered_ids` 一致（子集且同序） | — |
| D7 | **不是偷偷塞全文**：5 条 × 300 字符正文 ⇒ 块 ≤ 900 字符（压缩 3 倍以上） | 负向（防"索引"名不副实） |
| D8 | **牙齿**：把 `build_slot_block` patch 成"返回全文"⇒ D7 必须变红 | 证明 D7 真的在量压缩 |
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = r"D:\trinity-code"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from trinity.bridges import delivery_slot as DS  # noqa: E402


def _items(n=5, content_len=300):
    return [{"memory_id": "mem_%02d" % i, "category": "episodic",
             "content": ("第%d条 " % i) + "投递层回路的覆盖优先与相关性权衡" * (content_len // 18),
             "why": "hot"} for i in range(1, n + 1)]


def _surface(ids=("mem_01", "mem_02", "mem_03")):
    return {"surface_md": "OLD-FULL-TEXT-BLOCK", "sources": len(ids),
            "delivered_ids": list(ids), "cold_ids": [], "cold_sources": 0}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (DS.SLOT_GATE, DS.SLOT_MAX_CHARS_ENV):
        monkeypatch.delenv(k, raising=False)
    yield


def test_d1_default_returns_same_object(monkeypatch):
    surf = _surface()
    monkeypatch.delenv(DS.SLOT_GATE, raising=False)
    assert DS.slot_mode() == "full"
    assert DS.apply_slot(surf, items=_items()) is surf, "默认必须逐字节等价（同一对象）"


def test_d2_index_mode_replaces_only_the_block(monkeypatch):
    monkeypatch.setenv(DS.SLOT_GATE, "index")
    surf = _surface()
    got = DS.apply_slot(surf, items=_items(), top_k=5)
    assert DS.slot_mode() == "index"
    assert got is not surf and got["surface_md"] != surf["surface_md"]
    assert "id=mem_01" in got["surface_md"] and "id=mem_05" in got["surface_md"]
    assert len(got["slot_plan"]["entries"]) <= 5
    assert got["delivered_ids"] == surf["delivered_ids"], "账本字段不得被槽位形态改动"
    assert got["cold_ids"] == surf["cold_ids"] and got["sources"] == surf["sources"]


def test_d3_budget_drops_whole_lines(monkeypatch):
    monkeypatch.setenv(DS.SLOT_GATE, "index")
    monkeypatch.setenv(DS.SLOT_MAX_CHARS_ENV, "250")
    got = DS.apply_slot(_surface(), items=_items(n=8), top_k=8)
    plan = got["slot_plan"]
    assert plan["chars"] <= 250, "字符预算没守住"
    assert plan["truncated"] is True and plan["dropped_for_budget"], "超限必须记丢弃（不是静默截断）"
    assert len(plan["entries"]) < 8


def test_d4_fail_open_when_no_items(monkeypatch):
    monkeypatch.setenv(DS.SLOT_GATE, "index")
    surf = _surface()
    assert DS.apply_slot(surf) is surf, "拿不到条目 ⇒ 不许改（更不能制造空面）"
    empty = {"surface_md": "", "sources": 0, "delivered_ids": []}
    assert DS.apply_slot(empty, items=_items()) is empty
    # 条目存在但没有正文/ID ⇒ 同样不改
    assert DS.apply_slot(surf, items=[{"category": "x", "content": "abc"}]) is surf


def test_d5_rollback_to_full_is_identity(monkeypatch):
    monkeypatch.setenv(DS.SLOT_GATE, "index")
    assert DS.apply_slot(_surface(), items=_items()) is not None
    monkeypatch.setenv(DS.SLOT_GATE, "full")
    surf = _surface()
    assert DS.slot_mode() == "full"
    assert DS.apply_slot(surf, items=_items()) is surf, "回滚（改 env）后必须与改动前逐字节一致"
    monkeypatch.setenv(DS.SLOT_GATE, "bogus-value")
    assert DS.slot_mode() == "full" and DS.apply_slot(surf) is surf, "非法值必须回落 full"


def test_d6_block_is_explainable_and_ids_match(monkeypatch):
    monkeypatch.setenv(DS.SLOT_GATE, "index")
    surf = _surface()
    got = DS.apply_slot(surf, items=_items(), top_k=5)
    entries = got["slot_plan"]["entries"]
    assert [e["memory_id"] for e in entries] == ["mem_01", "mem_02", "mem_03", "mem_04", "mem_05"]
    assert [e["position"] for e in entries] == [1, 2, 3, 4, 5]
    listed = [ln.split("id=")[-1].strip() for ln in got["surface_md"].splitlines() if " id=" in ln]
    assert listed == [e["memory_id"] for e in entries]
    assert set(listed) >= set(surf["delivered_ids"]), "原投递 id 不得从索引里消失"


def test_d7_index_is_not_fulltext(monkeypatch):
    monkeypatch.setenv(DS.SLOT_GATE, "index")
    items = _items(n=5, content_len=600)
    full = sum(len(i["content"]) for i in items)
    got = DS.apply_slot(_surface(), items=items, top_k=5)
    block = len(got["slot_plan"]["block"])
    assert full >= 2500, "夹具前提：5 条全文应有 2500+ 字符"
    assert block <= DS.slot_max_chars(), "块必须受预算约束"
    assert block < full / 3, "索引没有真正压缩（疑似偷偷塞全文）：block=%d full=%d" % (block, full)


def test_d8_teeth_fulltext_builder_kills_d7(monkeypatch):
    """牙齿：把建造器换成"原样拼全文" ⇒ D7 的压缩断言必须失败。"""
    monkeypatch.setenv(DS.SLOT_GATE, "index")

    def _fulltext(items, *, top_k=5, max_chars=None, pull_hint=True):
        block = "\n".join("- [%s] %s id=%s" % (i["category"], i["content"], i["memory_id"])
                          for i in items[:top_k])
        return {"mode": "index", "block": block, "chars": len(block),
                "tokens_est": DS._tokens(block), "truncated": False,
                "dropped_for_budget": [],
                "entries": [{"memory_id": i["memory_id"], "position": n + 1}
                            for n, i in enumerate(items[:top_k])]}

    monkeypatch.setattr(DS, "build_slot_block", _fulltext)
    items = _items(n=5, content_len=600)
    got = DS.apply_slot(_surface(), items=items, top_k=5)
    block = len(got["slot_plan"]["block"])
    full = sum(len(i["content"]) for i in items)
    assert block > DS.slot_max_chars() and block > full / 3, \
        "把建造器换成全文后仍未突破压缩断言 ⇒ D7 不是真的在量压缩"
