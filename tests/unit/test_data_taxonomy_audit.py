# -*- coding: utf-8 -*-
"""数据资产分类审计（Step 0）的判据测试。

## 为什么这些断言长这样

本仓纪律：**判据本身要先能失败**，否则等于没有判据（AGENTS.md §13.3/§13.5）。
所以这里的每条测试都对着一个**真实发生过的失败形态**，而不是对着「函数返回了东西」。

覆盖的失败形态：
1. `classify()` 的边界 —— 空名空间必须归 residue（实测库里真有 1 条 agent_id 为空）；
2. **本工具第一版自己的假警报**（2026-10-05 实测）：
   `exclusion_name_mismatch` 只扫 active 行 ⇒ `lme`（13,743 行、**active=0**）
   被误报成「类目名对不上 ⇒ 排除静默失效」。真因是**观察面太窄**（§16）。
   这里的回归测试用「类目只存在于 archived」来钉死它。
3. 库不可读 / 缺列 ⇒ **INCONCLUSIVE 且带原因**，不许把「取不到」读成「不存在」（§13.2）。
4. `retrieval_excluded_rows` 必须**显式报数**（§13.0 的附带纪律：剔除不许静默）。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_mod():
    """按路径加载脚本（scripts/ 不是包，不能 import）。"""
    path = os.path.join(ROOT, "scripts", "trinity_data_taxonomy_audit.py")
    spec = importlib.util.spec_from_file_location("trinity_data_taxonomy_audit", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["trinity_data_taxonomy_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_mod()


# ---- 1. 纯函数边界 -----------------------------------------------------------

@pytest.mark.parametrize("agent,cat,expect", [
    ("dsh-agent", "perception", "retrieval_excluded"),
    ("dsh-agent", "benchmark", "retrieval_excluded"),
    ("", "general", "residue"),                      # 空名空间：实测存在 1 条
    ("cache_test", "general", "residue"),
    ("ablate-locomo", "general", "residue"),
    ("loop-compress-1791145173", "general", "machine_self"),   # 前缀规则
    ("brain-cycle", "general", "machine_self"),
    ("memory-revival", "general", "machine_self"),
    ("dsh-agent", "kb_harvested", "doc_chunk"),
    ("dsh-agent", "knowledge", "user_knowledge"),
    ("dsh-agent", "consolidated", "user_knowledge"),
    ("dsh-agent", "something_new", "unknown"),
])
def test_classify_boundaries(agent, cat, expect):
    assert mod.classify(agent, cat, ["perception", "benchmark", "lme", "stress-test"]) == expect


def test_classify_excluded_wins_over_agent_rule():
    """排除类目优先于 agent 规则 —— 否则 perception 会被别的规则抢走。"""
    assert mod.classify("brain-cycle", "perception", ["perception"]) == "retrieval_excluded"


@pytest.mark.parametrize("agent", ["reader", "reader-ops", "reader-value",
                                   "usage-feedback", "u1"])
def test_ambiguous_agents_are_not_residue(agent):
    """回归（2026-10-05 自我更正）：这 5 个名空间看着像测试、也可能是**合法子 agent**
    （类目 observation / analysis / general，共 60 行）。

    第一版把它们判成 residue ⇒ 会误归档真实数据。现在必须归 `unknown`（等人工定性）。

    反事实：把 AMBIGUOUS_AGENTS 并回 RESIDUE_AGENTS，本测试当场红。
    """
    tag = mod.classify(agent, "observation", ["perception"])
    assert tag == "unknown", "%s 不得被判为 residue（可能是合法 agent）" % agent


def test_unambiguous_scratch_is_still_residue():
    """反向断言：无歧义的测试名空间仍必须判 residue，否则这个分类就没有用了。"""
    for agent in ["cache_test", "kw_test", "inc-test", "probe-agent", "t1", "a",
                  "b1", "sig_0", "_warmup", "ingest_test", "smoke", "test-vec-agent"]:
        assert mod.classify(agent, "general", ["perception"]) == "residue", agent


def test_classify_without_exclusions_still_works():
    """引擎常量取不到时 classify 不该崩（由 audit 层判 INCONCLUSIVE）。"""
    assert mod.classify("dsh-agent", "perception", []) == "unknown"


# ---- 2. 观察面回归：类目只在 archived 时不许报「对不上」----------------------

def _mk_store(tmp_path, rows, columns=None):
    """建一个最小 memories 表。rows = (agent_id, category, status, access_count)。

    `columns` 可缩表（用于「缺列 ⇒ INCONCLUSIVE」的用例）；此时只插存在的列，
    否则建表与插入的列集不一致会先炸在 sqlite 上，测不到被测逻辑。
    """
    p = os.path.join(str(tmp_path), "store.db")
    con = sqlite3.connect(p)
    cols = columns or ["memory_id", "agent_id", "category", "status",
                       "access_count", "source_uri"]
    con.execute("CREATE TABLE memories (%s)" % ",".join("%s TEXT" % c for c in cols))
    wanted = [("memory_id", lambda i, r: i), ("agent_id", lambda i, r: r[0]),
              ("category", lambda i, r: r[1]), ("status", lambda i, r: r[2]),
              ("access_count", lambda i, r: r[3])]
    use = [(n, f) for n, f in wanted if n in cols]
    if use:
        sql = "INSERT INTO memories (%s) VALUES (%s)" % (
            ",".join(n for n, _ in use), ",".join("?" for _ in use))
        for i, r in enumerate(rows):
            con.execute(sql, tuple(f(i, r) for _, f in use))
    con.commit()
    con.close()
    return p


def test_exclusion_name_mismatch_scans_all_statuses(tmp_path, monkeypatch):
    """回归：`lme` 只存在于 archived ⇒ 不得报「类目名对不上」。

    这是本工具第一版的真实缺陷：只扫 active ⇒ 假警报。
    反事实说明：若把 all_categories 换回 active-only 的集合，本测试必须失败。
    """
    p = _mk_store(tmp_path, [
        # 四个排除类目**全部存在**，但 `lme` 只出现在 archived —— 这正是要测的形态。
        ("dsh-agent", "lme", "archived", 0),
        ("dsh-agent", "benchmark", "archived", 0),
        ("dsh-agent", "stress-test", "archived", 0),
        ("dsh-agent", "perception", "active", 0),
        ("dsh-agent", "general", "active", 3),
    ])
    monkeypatch.setattr(mod, "load_engine_exclusions",
                        lambda: (["perception", "benchmark", "lme", "stress-test"], "test"))
    r = mod.audit(p)
    assert r["verdict"] == "OK", r
    assert r["exclusion_name_mismatch"] == [], (
        "四个类目都真实存在（lme 在 archived）⇒ 不得判为对不上（观察面必须覆盖全部状态）")
    # 同时确认 active 口径下 lme 确实是 0 —— 说明旧版只看 active 为何会误报
    assert r["exclusion_active_rows"]["lme"] == 0
    assert r["exclusion_active_rows"]["perception"] == 1


def test_exclusion_name_mismatch_fires_when_truly_absent(tmp_path, monkeypatch):
    """反向断言：类目**真的**不存在时必须报出来，否则这个检查没有判别力。"""
    p = _mk_store(tmp_path, [
        ("dsh-agent", "perception", "active", 1),      # 存在
        ("dsh-agent", "general", "active", 1),         # 普通类目
    ])
    monkeypatch.setattr(mod, "load_engine_exclusions",
                        lambda: (["perception", "nonexistent_cat"], "test"))
    r = mod.audit(p)
    assert r["exclusion_name_mismatch"] == ["nonexistent_cat"]


# ---- 3. 取不到 ≠ 不存在（§13.2）---------------------------------------------

def test_missing_store_is_inconclusive_not_zero(tmp_path):
    r = mod.audit(os.path.join(str(tmp_path), "nope.db"))
    assert r["verdict"] == "INCONCLUSIVE"
    assert "not found" in r["error"]


def test_missing_column_is_inconclusive(tmp_path):
    """形状没对上必须与「数据为空」分开报（§13.2 的四种外衣之一）。"""
    p = _mk_store(tmp_path, [("a", "general", "active", 0)],
                  columns=["memory_id", "status"])
    r = mod.audit(p)
    assert r["verdict"] == "INCONCLUSIVE"
    assert "missing columns" in r["error"]


def test_engine_exclusions_unavailable_is_inconclusive(tmp_path, monkeypatch):
    """引擎常量取不到 ⇒ 无法按 §13.0 剔除 ⇒ 读数不可信，必须 INCONCLUSIVE。"""
    p = _mk_store(tmp_path, [("dsh-agent", "general", "active", 0)])
    monkeypatch.setattr(mod, "load_engine_exclusions", lambda: ([], "UNAVAILABLE: boom"))
    r = mod.audit(p)
    assert r["verdict"] == "INCONCLUSIVE"
    assert "unavailable" in r["error"]


# ---- 4. §13.0：剔除必须显式报数 ---------------------------------------------

def test_retrieval_excluded_rows_is_reported_and_subtracted(tmp_path, monkeypatch):
    p = _mk_store(tmp_path, [
        ("dsh-agent", "perception", "active", 0),     # 排除，且从未被读
        ("dsh-agent", "perception", "active", 5),
        ("dsh-agent", "knowledge", "active", 0),
        ("dsh-agent", "knowledge", "active", 7),
    ])
    monkeypatch.setattr(mod, "load_engine_exclusions", lambda: (["perception"], "test"))
    r = mod.audit(p)
    assert r["verdict"] == "OK", r
    assert r["retrieval_excluded_rows"] == 2, "剔除必须显式报数"
    assert r["retrievable_rows"] == 2
    # perception 的「从未被读」不得进分母：可检索面只有 2 行、其中 1 行冷 ⇒ 0.5
    assert r["retrievable_never_read"] == 1
    assert r["retrievable_cold_rate"] == 0.5, (
        "不参与检索的类目进了读率分母 ⇒ §13.0 违规")


def test_archived_rows_do_not_enter_active_buckets(tmp_path, monkeypatch):
    p = _mk_store(tmp_path, [
        ("dsh-agent", "knowledge", "active", 3),
        ("dsh-agent", "knowledge", "archived", 0),
        ("dsh-agent", "knowledge", "deleted", 0),
    ])
    monkeypatch.setattr(mod, "load_engine_exclusions", lambda: ([], "x"))
    # 排除清单为空 ⇒ INCONCLUSIVE，先验这一条不被绕过
    assert mod.audit(p)["verdict"] == "INCONCLUSIVE"
    monkeypatch.setattr(mod, "load_engine_exclusions", lambda: (["perception"], "test"))
    r = mod.audit(p)
    assert r["active_rows"] == 1
    assert r["total_rows"] == 3
    assert r["buckets"]["user_knowledge"]["rows"] == 1
