# -*- coding: utf-8 -*-
"""keyword 多词 FTS5 检索——**缺陷复核 + 回归闸门**（2026-10-06, T3）

这条链历史上有两次互相矛盾的登记，本文件用**可复现的最小语料**把事实钉死：

  · 2026（PRE457 §8.3）：报「`Trinity.search(mode='keyword')` 在 persona/tenant
    过滤下**多词 FTS5 返回 0** 的 bug」。
  · 同文件 §12.4 + §10.1：判定为**假警报**（根因是 `store_path` 被当目录 ⇒
    adapter 初始化失败 ⇒ 一切查询恒 0），并另有一版「适配器双形态查询，多词 0→5 条」
    的修复登记。
  · 当前实现（`trinity/adapters/sqlite/_search.py::_search_fts`）把词条拼成
    `t1* OR t2* OR ...`（**纯 OR**），而 §"双形态查询"的修复**已不在代码里**。

本文件同时测**两件不同的事**，因为它们会被互相混淆：

  T1（"返回 0" 缺陷）：多词 + persona 过滤是否仍然返回 0 条？
     → 实测**已修**（本文件 T1 两条通过）。本测试是它的常驻闸门（回归即红）。
  T2（OR 稀释嫌疑，**实测不成立**）：OR-only 语义让"只含一个不相关词的填充文档"
     占用名次，把同时含全部查询词的 gold 文档挤到后面。
     → 最小语料上 gold 仍排**第 1 名**（OR-only 与 AND 两臂对照记为断言消息）；
     → 真实题集上 AND-first **不可行**：120/120 条多词查询的 AND 候选**恒为 0**
       （见 `_probe_fts_or_vs_and.py`）⇒ "AND 优先"这条修法等价于现状。
"""
from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

FILLERS = 120  # 填充文档数：每篇只含两个查询词中的一个
PERSONA = "fts-multiword-persona"
GOLD = ("gold",
        "Priya Nandakumar certified the Orion turbine calibration on 14 March 2024. "
        "The turbine calibration certificate is filed under project Helios.")


def _store(tmp_path):
    """建临时库。

    **必须用 pytest 的 `tmp_path`**：审计脚本 `scripts/temp_leak_audit.py` 把
    「文件里出现 `tempfile.mkdtemp(` 且本文件内没有清理机制」记为泄漏点
    （棘轮面，`tests/unit` 在扫描范围内）⇒ 用 `mkdtemp` 会把棘轮顶红。
    `tmp_path` 由 pytest 自己按用例生命周期清理，是**真清理**，不是标记。
    """
    from trinity import Trinity
    return Trinity(adapter="sqlite", store_path=str(tmp_path / "store"))


def _seed(mem, n_fillers: int = FILLERS) -> None:
    """填充语料：每篇只含 `turbine` **或** 只含 `calibration`（都不含 gold 的另一半）。"""
    for i in range(n_fillers):
        word = "turbine" if i % 2 == 0 else "calibration"
        mem.ingest("Filler note %d mentions %s in an unrelated context." % (i, word),
                   persona_id=PERSONA, session_id="fill", category="general",
                   tags=["filler"])
    mem.ingest(GOLD[1], persona_id=PERSONA, session_id="gold", category="general",
               tags=["gold"])
    mem.ingest("A gold decoy about turbine calibration belonging to another persona.",
               persona_id="other-persona", session_id="decoy", category="general",
               tags=["decoy"])


def _rank_of(mem, query: str, marker: str, top_k: int = 50):
    """返回 marker 出现在第几名（1 起）；未出现返回 None。"""
    res = mem.search(query=query, mode="keyword", top_k=top_k, persona_id=PERSONA)
    rows = res.get("results", []) if isinstance(res, dict) else res
    for i, r in enumerate(rows):
        if marker in str(r.get("content") or ""):
            return i + 1
    return None


# ── T1：多词 + persona 过滤返回 0 的旧缺陷（实测**已修**）────────────────
def test_multiword_keyword_with_persona_filter_returns_rows(tmp_path):
    """多词查询 + persona 过滤 **不得**返回 0 条（历史"返回 0"缺陷的闸门）。"""
    mem = _store(tmp_path)
    _seed(mem)
    res = mem.search(query="turbine calibration certificate", mode="keyword",
                     top_k=5, persona_id=PERSONA)
    rows = res.get("results", []) if isinstance(res, dict) else res
    assert rows, (
        "多词 + persona 过滤返回 0 条 —— 历史缺陷复发。"
        "查 trinity/adapters/sqlite/_search.py::_search_fts 的 where 参数拼接顺序。")
    assert all(r.get("persona_id") == PERSONA for r in rows), "persona 过滤被绕过"


def test_multiword_keyword_finds_gold(tmp_path):
    """T1 的加强版：gold 必须至少被召回（不管名次）。"""
    mem = _store(tmp_path)
    _seed(mem)
    rank = _rank_of(mem, "turbine calibration certificate", "Orion turbine calibration")
    assert rank is not None, (
        "gold 文档完全未被召回。若 T1 的上一条通过而本条失败，说明是**排序/截断**问题"
        "（gold 掉出 top_k），不是过滤问题。")


# ── T2：OR 稀释嫌疑（**实测不成立**，本测试是它的反例闸门）──────────────
def test_or_dilution_does_not_sink_gold(tmp_path):
    """gold（含全部 3 个查询词）在 OR-only 语义下**仍须排在前面**。

    语料设计成 OR 最不利：122 篇里 120 篇是"单侧词"填充文档（每篇只含
    `turbine` 或只含 `calibration`），只有 gold 同时含全部查询词。
    实测 gold 排**第 1 名** ⇒ "OR 稀释把真实命中的名次打下去"这一嫌疑
    在本语料上**不成立**。
    """
    mem = _store(tmp_path)
    _seed(mem)
    rank = _rank_of(mem, "turbine calibration certificate", "Orion turbine calibration")
    assert rank is not None, "gold 未被召回"
    assert rank <= 5, (
        "gold（同时含全部查询词）排到第 %d 名，被只含单个查询词的填充文档挤下去。"
        "根因候选：`_search_fts` 用 `\" OR \"` 拼词条（OR-only 语义），任一命中即进候选。"
        "注意：**AND-first 修法已被证否**（真实题集 120/120 多词查询 AND 候选恒为 0），"
        "若要修只能改成「有下界的 OR」（至少命中 j 个词），见 scripts/retrieval_contribution.py"
        " 的 fts_or_min* 臂。" % rank)


def test_or_pool_is_superset_of_and_pool(tmp_path):
    """可观测性：把 OR/AND 两臂的候选数与 gold 名次一起打进断言消息。

    本用例不判红/绿（`assert True` 之外只留一条恒真断言），失败时 `pytest -s` 会打印
    两臂读数，供 `RETRIEVAL-CONTRIBUTION.md` 的"多词 FTS5"一节引用。
    """
    mem = _store(tmp_path)
    _seed(mem)
    q = "turbine calibration certificate"
    or_rank = _rank_of(mem, q, "Orion turbine calibration")
    ad = mem._adapter
    terms = [t for t in ad._tokenize_fts_query(q) if t.strip()]
    # 2026-10-06（t74/I14）：原为 `esc = lambda j: …（原本带 E731 抑制）` ⇒ 改 `def`（真修，不再靠抑制）
    def esc(j):
        return j.join('"%s"*' % t.replace('"', '""') for t in terms)
    with ad._get_read_conn() as conn:
        def _n(mq):
            return conn.execute(
                "SELECT COUNT(*) FROM memories_fts WHERE memories_fts MATCH ?",
                (mq,)).fetchone()[0]
        n_or, n_and = _n(esc(" OR ")), _n(esc(" AND "))
    print("\n[FTS 多词] terms=%r\n  OR-only    命中文档数=%d  gold 名次=%s\n"
          "  AND-first  命中文档数=%d" % (terms, n_or, or_rank, n_and))
    assert n_or >= n_and, "OR 的候选集必然是 AND 的超集"
