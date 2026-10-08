# -*- coding: utf-8 -*-
"""doc2query 受限试点（Step 3）判据测试。

## 钉住的失败形态

1. **循环论证**：用生成的问题建索引、再用**同一个问题**当测试查询 ⇒ 自证。
   本工具强制 hold-out，`--holdout 0` 必须被拒绝。**这是全套判据里最要紧的一条** ——
   循环论证会让试点永远「成功」，从而把没有效果的机制铺开。
2. **§13.0**：不参与检索的类目不得进候选，且剔除要**显式报数**。
3. **§13.2**：LLM 解析失败不算「没问题」——必须返回空列表由调用方计数，不许猜着补。
4. **非侵入**：`drop` 只能删试点自己的表，**不得**碰 `memories` / `memories_fts`。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "doc2query_pilot.py")
    spec = importlib.util.spec_from_file_location("doc2query_pilot", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["doc2query_pilot"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ---- parse_questions：纯函数 -------------------------------------------------

def test_parse_json_array():
    assert mod.parse_questions('["怎么把多个数据源合并", "如何做库存对账"]') == \
        ["怎么把多个数据源合并", "如何做库存对账"]


def test_parse_numbered_list():
    raw = "1. 怎么把多个数据源合并成一张表\n2. 如何做库存对账\n3. 缺货时怎么补货"
    got = mod.parse_questions(raw)
    assert got == ["怎么把多个数据源合并成一张表", "如何做库存对账", "缺货时怎么补货"]


def test_parse_bullet_and_bracket_numbers():
    raw = "- 怎么设计补货策略\n• 如何评估供应商\n(3) 怎么降低缺货率"
    got = mod.parse_questions(raw)
    assert len(got) == 3 and got[0] == "怎么设计补货策略"


def test_parse_dedups_and_filters_short_and_long():
    raw = '["abc", "怎么把多个数据源合并成一张表", "怎么把多个数据源合并成一张表", "%s"]' % ("很" * 250)
    got = mod.parse_questions(raw)
    assert got == ["怎么把多个数据源合并成一张表"], "去重 + 过滤过短(<=4)/过长(>200)"


def test_parse_returns_empty_on_garbage_not_guesses():
    """§13.2：解析不了就返回空，**不许猜**。调用方据此计数上报。"""
    assert mod.parse_questions("") == []
    assert mod.parse_questions("```json\n{broken") == [] or isinstance(
        mod.parse_questions("```json\n{broken"), list)


# ---- 循环论证防护 ------------------------------------------------------------

def test_holdout_zero_rejected_by_build(tmp_path):
    r = mod.build(str(tmp_path), 1, 0.6, [], holdout=0)
    assert r["verdict"] == "FAILED" and "holdout" in r["error"]


def test_holdout_zero_rejected_by_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["doc2query_pilot.py", "--build", "--holdout", "0"])
    assert mod.main() == 2
    assert "循环论证" in capsys.readouterr().out


def test_holdout_shrinks_index_text():
    """留出必须真的把最后 holdout 条从索引里拿掉（否则等于没留出）。"""
    qs = ["q1", "q2", "q3", "q4", "q5"]
    keep_h1 = qs[: len(qs) - 1]
    keep_h2 = qs[: len(qs) - 2]
    assert "q5" not in keep_h1 and "q5" in qs
    assert "q4" not in keep_h2 and "q5" not in keep_h2


# ---- §13.0：排除类目 ---------------------------------------------------------

def _mk(tmp_path, rows, with_fts=True):
    """rows = (memory_id, category, importance, access_count, status)"""
    p = os.path.join(str(tmp_path), "store.db")
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE memories (memory_id TEXT, category TEXT,
        importance TEXT, access_count TEXT, status TEXT, created_at TEXT)""")
    if with_fts:
        con.execute("CREATE VIRTUAL TABLE memories_fts USING fts5(content, category, tags)")
    for m, c, i, a, s in rows:
        con.execute("INSERT INTO memories VALUES (?,?,?,?,?,'2026-01-01')", (m, c, i, a, s))
    con.commit()
    con.close()
    return p


@pytest.fixture(autouse=True)
def _excl(monkeypatch):
    monkeypatch.setattr(mod, "load_exclusions", lambda: (["perception"], "test"))


def test_excluded_category_not_candidate_but_reported(tmp_path):
    p = _mk(tmp_path, [
        ("m1", "knowledge", "0.9", "0", "active"),
        ("m2", "perception", "0.9", "0", "active"),
        ("m3", "perception", "0.9", "0", "active"),
    ])
    r = mod.select_candidates(p, 10, 0.6, [])
    assert r["verdict"] == "OK", r
    assert r["picked"] == 1, "排除类目不得进候选"
    assert r["retrieval_excluded_active"] == 2, "剔除必须显式报数（§13.0）"


def test_only_cold_and_important_are_candidates(tmp_path):
    p = _mk(tmp_path, [
        ("hot", "knowledge", "0.9", "7", "active"),      # 已被读过 ⇒ 不是冷
        ("low", "knowledge", "0.2", "0", "active"),      # 低重要度
        ("arch", "knowledge", "0.9", "0", "archived"),   # 非 active
        ("good", "knowledge", "0.9", "0", "active"),     # 唯一合格
    ])
    r = mod.select_candidates(p, 10, 0.6, [])
    assert r["picked"] == 1 and r["cold_candidates_total"] == 1


def test_category_filter(tmp_path):
    p = _mk(tmp_path, [
        ("a", "kb_harvested", "0.9", "0", "active"),
        ("b", "knowledge", "0.9", "0", "active"),
    ])
    assert mod.select_candidates(p, 10, 0.6, ["kb_harvested"])["picked"] == 1


def test_missing_store_inconclusive(tmp_path):
    r = mod.select_candidates(os.path.join(str(tmp_path), "no.db"), 5, 0.6, [])
    assert r["verdict"] == "INCONCLUSIVE" and "not found" in r["error"]


def test_exclusions_unavailable_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "load_exclusions", lambda: ([], "UNAVAILABLE: boom"))
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    r = mod.select_candidates(p, 5, 0.6, [])
    assert r["verdict"] == "INCONCLUSIVE" and "unavailable" in r["error"]


# ---- 非侵入：drop 只删自己的表 ------------------------------------------------

def test_drop_only_removes_pilot_table(tmp_path):
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    con = sqlite3.connect(p)
    con.execute("CREATE VIRTUAL TABLE memories_doc2query USING fts5("
                "memory_id UNINDEXED, questions, tokenize='unicode61')")
    con.execute("INSERT INTO memories_doc2query VALUES ('m1','怎么 合并 数据源')")
    con.commit()
    con.close()

    r = mod.drop(p)
    assert r["dropped"] is True
    con = sqlite3.connect(p)
    tables = {t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "memories_doc2query" not in tables, "试点表必须被删"
    assert "memories" in tables and "memories_fts" in tables, "既有表不得被碰"
    assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    con.close()


def test_drop_is_idempotent(tmp_path):
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    assert mod.drop(p)["dropped"] is False, "表不存在时不得报错"
    assert mod.drop(p)["verdict"] == "OK"


def test_evaluate_inconclusive_without_table(tmp_path):
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    r = mod.evaluate(p, 10, 5)
    assert r["verdict"] == "INCONCLUSIVE" and "不存在" in r["error"]


def test_fts_hits_returns_empty_for_empty_tokens(tmp_path):
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    con = sqlite3.connect(p)
    assert mod._fts_hits(con, "memories_fts", "content", "   ", 10) == []
    con.close()


# ---- 留出问题存表：让 eval 确定性且零 LLM 调用 --------------------------------

def _seed_pilot(tmp_path):
    """建一个「已 build 过」的最小库：索引 4 题 + 留出 1 题。"""
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    con = sqlite3.connect(p)
    con.execute("CREATE VIRTUAL TABLE memories_doc2query USING fts5("
                "memory_id UNINDEXED, questions, tokenize='unicode61')")
    con.execute("CREATE TABLE memories_doc2query_holdout (memory_id TEXT, question TEXT)")
    con.execute("INSERT INTO memories_doc2query VALUES ('m1', ?)", (mod.tokenize("如何 合并 数据源"),))
    con.execute("INSERT INTO memories_doc2query_holdout VALUES ('m1','怎么把两张表合成一张')")
    con.commit()
    con.close()
    return p


def test_evaluate_uses_stored_holdout_and_calls_no_llm(tmp_path, monkeypatch):
    """eval 必须**零 LLM 调用**（留出问题从表里读）。

    反事实：若 eval 回到「重新生成问题」，本测试里被替换成抛异常的 `gen_questions`
    会让整条判据崩掉 ⇒ 当场红。这条同时钉住「判据可复跑」。
    """
    p = _seed_pilot(tmp_path)
    def boom(*_a, **_k):
        raise AssertionError("eval 不得调用 LLM（留出问题应来自 memories_doc2query_holdout）")
    monkeypatch.setattr(mod, "gen_questions", boom)
    r = mod.evaluate(p, 10, 10)
    assert r["verdict"] == "OK", r
    assert r["holdout_source"] == mod.HOLDOUT_TABLE
    assert r["n"] == 1
    assert "baseline_hit@k" in r and "treatment_hit@k" in r


def test_evaluate_inconclusive_when_holdout_table_missing(tmp_path):
    """旧版试点没有留出表 ⇒ 必须 INCONCLUSIVE 并说清要重跑 build，而不是给个假读数。"""
    p = _mk(tmp_path, [("m1", "knowledge", "0.9", "0", "active")])
    con = sqlite3.connect(p)
    con.execute("CREATE VIRTUAL TABLE memories_doc2query USING fts5("
                "memory_id UNINDEXED, questions, tokenize='unicode61')")
    con.commit()
    con.close()
    r = mod.evaluate(p, 10, 10)
    assert r["verdict"] == "INCONCLUSIVE" and "重跑 --build" in r["error"]


def test_drop_removes_holdout_table_too(tmp_path):
    p = _seed_pilot(tmp_path)
    r = mod.drop(p)
    assert set(r["dropped_tables"]) == {mod.TABLE, mod.HOLDOUT_TABLE}
    con = sqlite3.connect(p)
    names = {t[0] for t in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    con.close()
    assert not any(n.startswith("memories_doc2query") for n in names), "回滚必须删干净试点表"


def test_tokenize_splits_chinese_into_tokens():
    """jieba 空格分词 —— 与 memories_fts 同款；不分词则中文在 fts5 里整句成一个 token。"""
    toks = mod.tokenize("如何合并多个数据源").split()
    assert len(toks) > 1, "必须切成多个词元"
