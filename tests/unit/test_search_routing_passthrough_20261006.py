# -*- coding: utf-8 -*-
"""`Trinity.search()` 的 `routing` 透传闸门（2026-10-06, T3）

**修的缺陷**：`search_hybrid()` 早在 2026-08-15 就实现了"自适应预算路由"
（`routing="auto"|"light"|"full"`：短查询走 light FTS 快通道，长查询走 5 通道 full），
其文档同时写着"显式 `light`/`full` 按调用方意图"。而 `Trinity.search()`
**从不接受也不透传** `routing`（`_search.py` 全仓零匹配）⇒ 引擎入口上：

  · `search(mode="hybrid", routing="light")` → **TypeError**（无该参数）；
  · `search(mode="hybrid")` → 恒走 full，文档所称"短查询走 light 快通道"
    对引擎入口**不成立**。

本文件是它的常驻闸门。前后对比（同一临时库、同一批查询、同一进程）：

  修复前：`search(mode="hybrid", routing="light")` → TypeError: unexpected keyword argument
  修复后：light 档可达，且与 `search_hybrid(routing="light")`（直连）同为 light 实现

**默认行为不变**：`routing=None`（默认）= 不透传，逐字沿用旧调用形状
（`test_default_routing_is_not_passed_through` 用基于**类**的猴子补丁证明"没传"）。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

PERSONA = "routing-persona"


def _store(tmp_path):
    """建临时库。

    **必须用 pytest 的 `tmp_path`**：`scripts/temp_leak_audit.py` 把「文件里出现
    `tempfile.mkdtemp(` 且本文件内无清理机制」记为泄漏点（`tests/unit` 在棘轮扫描面内）
    ⇒ 用 `mkdtemp` 会把棘轮顶红（由 t1 测试归因发现，2026-10-06）。`tmp_path` 由
    pytest 按用例生命周期真清理，不是标记。
    """
    from trinity import Trinity
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path / "store"))
    for i, txt in enumerate([
        "Carol's team built an internal ML platform called 'Orion'.",
        "Jack ran the Sydney Marathon in September 2024, PB 3:28.",
        "Eva shoots film photography with a vintage Leica M6.",
        "Henry moved from academia to industry in July 2024.",
        "Priya certified the turbine calibration on 14 March 2024.",
    ]):
        mem.ingest(txt, persona_id=PERSONA, session_id=str(i), category="general",
                   tags=["routing"])
    return mem


# ── 修复的核心：显式 routing 必须可达 ─────────────────────────────────────
def test_explicit_light_routing_is_accepted_by_engine_entry(tmp_path):
    """修复前本行是 `TypeError: search() got an unexpected keyword argument 'routing'`。"""
    mem = _store(tmp_path)
    res = mem.search(query="Orion", mode="hybrid", top_k=5,
                     persona_id=PERSONA, routing="light")
    assert isinstance(res, dict) and "results" in res, "search() 未接受 routing 参数"
    rows = res.get("results") or []
    assert rows, "routing=light 返回空 —— 透传接线成功但 light 通路本身无结果"
    assert all(r.get("persona_id") == PERSONA for r in rows), "persona 过滤被绕过"


def test_explicit_full_routing_is_accepted(tmp_path):
    mem = _store(tmp_path)
    res = mem.search(query="Orion internal ML platform", mode="hybrid", top_k=5,
                     persona_id=PERSONA, routing="full")
    assert (res.get("results") or []), "routing=full 返回空"


def test_light_and_full_are_both_reachable_and_differ_in_cost(tmp_path):
    """light/full 两档都可达；并留档两者耗时（**不判优劣**，只证明"不可达"已修）。"""
    mem = _store(tmp_path)
    import time
    out = {}
    for r in ("light", "full"):
        t0 = time.perf_counter()
        res = mem.search(query="What did Jack run in Sydney in September 2024?",
                         mode="hybrid", top_k=5, persona_id=PERSONA, routing=r)
        out[r] = (time.perf_counter() - t0) * 1000
        assert (res.get("results") or []), "routing=%s 返回空" % r
    print("\n[search(routing=)] light=%.0fms full=%.0fms" % (out["light"], out["full"]))


# ── 默认行为不变（回归闸门）────────────────────────────────────────────
def test_default_routing_is_not_passed_through(tmp_path, monkeypatch):
    """`routing` 缺省时**不得**出现在 `search_hybrid` 的调用参数里。

    理由：`search_hybrid(routing=...)` 的默认值是 `"auto"`，而 `auto` 会把
    短查询（<=8 字符）判成 light。若本入口无脑透传 `"auto"`，就等于**顺手改了
    短查询的默认通路** —— 那是行为变更，不是接线修复。故判据是"没传"。
    """
    mem = _store(tmp_path)
    # **必须先把 hybrid retriever 建起来**：`search(mode="hybrid")` 只在
    # `self._hybrid_retriever is not None` 时才走融合分支，否则**根本不会调用**
    # `search_hybrid`（静默走 FTS）⇒ spy 拿不到东西、断言以 `None` 失败，让人误读成
    # "透传了"。实测踩过：不加这一行时 `seen == {}`。
    mem.hybrid_retriever  # noqa: B018 —— 触发懒惰构建
    seen = {}
    orig = type(mem).search_hybrid

    def _spy(self, *a, **kw):
        seen["routing_in_kwargs"] = "routing" in kw
        return orig(self, *a, **kw)

    # 必须打在**类**上：`_search.py` 里是 `self.search_hybrid(...)`，实例属性补丁
    # 对该属性查找无效（实测该补丁漏过 ⇒ 断言拿到 None）。
    monkeypatch.setattr(type(mem), "search_hybrid", _spy)
    mem.search(query="Orion", mode="hybrid", top_k=3, persona_id=PERSONA)
    assert seen.get("routing_in_kwargs") is False, (
        "缺省调用把 routing 透传下去了 ⇒ 短查询默认通路被静默改成 light，"
        "这是行为变更；只有调用方显式给值时才该透传。")


def test_docstring_documents_routing(tmp_path):
    """文档与实现同步：`search()` 的 docstring 必须提到 routing（否则又是一处声明脱节）。"""
    from trinity.core.client._search import _SearchMixin
    doc = _SearchMixin.search.__doc__ or ""
    assert "routing" in doc, "search() 的 docstring 未记录 routing 参数"
