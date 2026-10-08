# -*- coding: utf-8 -*-
"""T12 判据：冷集口径（响亮 vs 静默空集）+ 计数按 origin 分列 + 投递账本来源过滤。

背景（复现证据见 DELIVERY-COLD-FIX.md）：
  · SQLite 上 `hasattr(adapter,"_get_conn")` **恒假**（`_get_conn` 只在 PostgreSQLAdapter）
    ⇒ 冷集查询**根本没执行** ⇒ 状态落成 `empty`、无日志（= 静默空集）；
  · `empty_total` 判据把探针与生产混在一起 ⇒ 跑一次测试就红，**没有判别力**。

本文件把两件事都钉成可证伪的判据，并包含**负向实测**：
把"响亮"那半边还原成旧行为（缺列 ⇒ `empty`）时，判据必须**红**。
"""
from __future__ import annotations

import json
import os
import sqlite3

import pytest

from trinity.bridges import cold_caliber as cc
from trinity.bridges import origin_split as osplit
from trinity.bridges import delivery_ledger as dl

COLS_PG = ["memory_id", "status", "last_retrieved_at", "access_count", "last_accessed_at"]
COLS_SQLITE = ["memory_id", "status", "access_count", "last_accessed_at"]   # 无 last_retrieved_at
OLD_PG_SQL = ("select memory_id from memories "
              "where status='active' and last_retrieved_at is null")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(cc.CALIBER_GATE, raising=False)
    yield


# ── ① 口径选择（纯函数）──────────────────────────────────────────────
def test_pg_caliber_unchanged_when_column_present():
    for mode in ("loud", "fallback", "off-not-set"):
        info = cc.resolve(COLS_PG, gate_mode="loud" if mode != "fallback" else "fallback")
        assert info["status"] == "loaded"
        assert info["sql"] == OLD_PG_SQL, "PG 口径的 SQL 必须与改动前逐字节相同"


def test_missing_column_is_unavailable_not_empty_by_default():
    info = cc.resolve(COLS_SQLITE)                      # 默认 loud
    assert info["status"] == "unavailable", "缺主口径列时不得记成 empty（静默空集）"
    assert "last_retrieved_at" in info["reason"] and info["reason"]
    assert info["sql"] == "", "unavailable 时不得给出可执行 SQL"


def test_missing_column_with_fallback_gate_is_labelled_lower_bound():
    info = cc.resolve(COLS_SQLITE, gate_mode="fallback")
    assert info["status"] == "loaded_fallback"
    assert "access_count" in info["sql"]
    # 2026-10-06（t41/N2）：原实现是 `assert "LAST" not in info["caliber"].upper() or True`
    # —— `or True` 让它**永远不可能失败**（`fake_green_audit --ratchet` 因此判红）。
    # 现在拆成两条**可失败**的断言：① 标签必须点名它实际用的列；② 不得冒称 LAST 权威口径。
    assert "access_count" in info["caliber"], (
        "回退口径的标签没有点名它实际用的列（access_count）⇒ 标签与 SQL 脱节：%r" % info["caliber"])
    assert "LAST" not in info["caliber"].upper(), (
        "回退口径的标签里出现了 LAST ⇒ 把 access_count 下界口径冒充成 last_retrieved_at 权威口径：%r"
        % info["caliber"])
    assert "下界" in info["caliber"], "回退口径必须显式标注为下界（不得冒充权威口径）"


def test_probe_failure_and_gate_off():
    assert cc.resolve(None, probe_error="boom")["status"] == "unavailable"
    off = cc.resolve(COLS_PG, gate_mode="off")
    assert off["status"] == "disabled" and off["reason"]
    neither = cc.resolve(["memory_id", "status"])
    assert neither["status"] == "unavailable" and neither["reason"]


def test_authoritative_sql_text_is_byte_identical_to_pre_t12():
    """PG 路径不变的最强证据：SQL 字面量与改动前完全一致。"""
    assert cc._AUTHORITATIVE_SQL == OLD_PG_SQL


# ── ② 真·SQLite 连接（走 `_tags._conn_ctx`，证明方言盲守卫已修）──────
class _SqliteFake:
    """最小适配器替身：**只有裸 `_conn`**（与 `SQLiteAdapter` 同形，无 `_get_conn`）。"""

    def __init__(self, ddl: str, rows=()):
        self._conn = sqlite3.connect(":memory:")
        cur = self._conn.cursor()
        cur.execute(ddl)
        for r in rows:
            cur.execute("insert into memories values (%s)" % ",".join("?" * len(r)), r)
        self._conn.commit()


DDL_NO_COL = ("create table memories (memory_id text, status text, access_count integer, "
              "last_accessed_at text)")
DDL_WITH_COL = ("create table memories (memory_id text, status text, access_count integer, "
                "last_accessed_at text, last_retrieved_at text)")


def test_sqlite_adapter_is_readable_now_and_reports_unavailable():
    ad = _SqliteFake(DDL_NO_COL, [("m1", "active", 0, "2026-01-01")])
    cols, backend, err = cc.detect_columns(ad)
    assert cols and "access_count" in cols and "last_retrieved_at" not in cols, \
        "SQLite 的连接必须能读到（旧的 hasattr(adapter,'_get_conn') 守卫在 SQLite 上恒假）"
    ids, info = cc.load_cold_ids(ad)
    assert info["status"] == "unavailable" and ids == []
    assert ids != [""] and info["reason"]


def test_sqlite_fallback_gate_computes_lower_bound_ids(monkeypatch):
    monkeypatch.setenv(cc.CALIBER_GATE, "fallback")
    ad = _SqliteFake(DDL_NO_COL, [("m1", "active", 0, "2026-01-01"),
                                  ("m2", "active", 3, "2026-01-02"),
                                  ("m3", "archived", 0, "2026-01-03")])
    ids, info = cc.load_cold_ids(ad)
    assert info["status"] == "loaded_fallback"
    assert ids == ["m1"], "下界口径：active 且 access_count=0"
    assert "下界" in info["caliber"]


def test_pg_shaped_table_still_loads_authoritative_caliber():
    ad = _SqliteFake(DDL_WITH_COL, [("m1", "active", 0, "x", None),
                                    ("m2", "active", 5, "x", "2026-01-01"),
                                    ("m3", "archived", 0, "x", None)])
    ids, info = cc.load_cold_ids(ad)
    assert info["status"] == "loaded"
    assert ids == ["m1"], "权威口径：active 且 last_retrieved_at IS NULL"


def test_connection_error_is_loud_not_empty():
    class Boom:
        def _get_conn(self):
            raise RuntimeError("pool exhausted")

        def __getattr__(self, name):
            raise RuntimeError("pool exhausted")

    ids, info = cc.load_cold_ids(Boom())
    assert ids == []
    assert info["status"] == "unavailable", "连接失败必须是 unavailable（响亮），不是 empty"
    assert info["reason"]


def test_format_log_marks_loud_states():
    assert "LOUD" in cc.format_log({"status": "unavailable", "reason": "r", "backend": "x"}, 0)
    assert "LOUD" in cc.format_log({"status": "disabled", "reason": "r"}, 0)
    normal = cc.format_log({"status": "loaded", "caliber": "c", "backend": "pg"}, 7)
    assert "size=7" in normal and "LOUD" not in normal


# ── ③ 负向实测：把"响亮"那半边还原成旧行为 ⇒ 判据必须红 ───────────────
def _assert_loud_on_missing_column() -> None:
    info = cc.resolve(COLS_SQLITE)
    assert info["status"] == "unavailable", "缺列必须是 unavailable"
    assert info["reason"], "必须给出 reason"


def test_negative_restoring_silent_empty_makes_judge_red(monkeypatch):
    """把 `resolve` 还原成旧语义（缺列 ⇒ empty）后，本套判据**必须失败**。

    这是"负向实测"，证明上面那些绿灯不是自证的：判据真的盯着那个行为。
    """
    monkeypatch.setattr(cc, "resolve", lambda columns, **kw: {
        "status": "empty", "sql": "", "caliber": "", "reason": "", "backend": ""})
    with pytest.raises(AssertionError):
        _assert_loud_on_missing_column()


# ── ④ empty_total 按 origin 分列（判据要有判别力）────────────────────
def _counters_sample(prod_empty: int = 1, probe_empty: int = 195, prod_enabled: int = 173):
    # 探针 bucket 的 empty 之和 == probe_empty（默认 128+30+30+2+2+3 = 195，与实测一致）
    _others = 30 + 30 + 2 + 2 + 3
    first_probe = max(0, int(probe_empty) - _others)
    return {
        "empty_total": prod_empty + first_probe + _others,
        "enabled": prod_enabled + 437 + 54 + 54 + 4 + 2 + 4 + 4,
        "by_origin": {
            "dsh-plugin": {"calls": 176, "enabled": prod_enabled, "surfaced": 860,
                           "cold": 190, "empty": prod_empty},
            "probe:test_opening_directory_empty": {"calls": 437, "enabled": 437,
                                                   "surfaced": 309, "cold": 0,
                                                   "empty": first_probe},
            "probe:t9_selftest_v1_off": {"calls": 54, "enabled": 54, "empty": 30},
            "probe:t9_selftest_v2_on": {"calls": 54, "enabled": 54, "empty": 30},
            "probe:_p816_directory_empty_ab": {"calls": 4, "enabled": 4, "empty": 2},
            "probe:_p816_premise": {"calls": 2, "enabled": 2, "empty": 2},
            "probe:t9_diag": {"calls": 4, "enabled": 4, "empty": 3},
            "unknown": {"calls": 4, "enabled": 4, "surfaced": 20, "cold": 8, "empty": 0},
        },
    }


def test_origin_class():
    assert osplit.origin_class("dsh-plugin") == "prod"
    assert osplit.origin_class("probe:x") == "probe"
    assert osplit.origin_class("") == "unknown"
    assert osplit.origin_class(None) == "unknown"
    assert osplit.origin_class("DSH-Plugin") == "unknown", "大小写/近似名不得冒充生产"


def test_empty_split_separates_probe_from_production():
    s = osplit.empty_split(_counters_sample())
    assert s["empty_by_class"]["prod"] == 1
    assert s["empty_by_class"]["probe"] == 195
    assert s["empty_total"] == 196 and s["empty_rate_all"] is not None
    assert s["criterion"]["applies_to"] == "prod"
    assert s["criterion"]["pass"] is False, "生产空率非 0 ⇒ 判据必须红"


def test_criterion_green_when_only_probes_are_empty():
    """判据的判别力：**探针再红**也不能让生产判据红。"""
    s = osplit.empty_split(_counters_sample(prod_empty=0, probe_empty=500))
    assert s["empty_by_class"]["probe"] == 500
    assert s["criterion"]["pass"] is True
    assert s["empty_rate_prod"] == 0.0


def test_empty_split_tolerates_missing_by_origin():
    s = osplit.empty_split({"empty_total": 3, "enabled": 10})
    assert s["empty_by_class"]["prod"] == 0 and s["legacy_note"]


# ── ⑤ 投递账本来源过滤（读侧）────────────────────────────────────────
def _ledger(tmp_path):
    p = tmp_path / "ledger.jsonl"
    now = 2_000_000_000.0
    rows = []
    # 生产：3 批，共 6 条投递，去重 4（mem-a 重复 3 次）
    for sid, ids in (("s1", ["mem-a", "mem-b"]), ("s2", ["mem-a", "mem-c"]),
                     ("s3", ["mem-a", "mem-d"])):
        rows.append({"ts": now - 60, "origin": "dsh-plugin", "session_id": sid, "ids": ids})
    # 探针：2 批，4 条投递，id 全是 m-2/m-1（T12 实测的污染形状）
    for _ in range(2):
        rows.append({"ts": now - 60, "origin": "probe:test_opening_directory_empty",
                     "session_id": "probe", "ids": ["m-2", "m-1"]})
    # 窗口外（必须被忽略）
    rows.append({"ts": now - 90 * 86400, "origin": "dsh-plugin", "session_id": "old",
                 "ids": ["mem-z"]})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return str(p), now


def test_delivery_stats_default_keeps_legacy_shape(tmp_path):
    p, now = _ledger(tmp_path)
    out = dl.delivery_stats(days=1, path=p, now=now)
    for k in ("records", "deliveries", "distinct", "repeat_rate", "max_repeat", "top"):
        assert k in out, "旧键必须仍在（additive 才能不破旧读侧）"
    assert out["records"] == 5 and out["deliveries"] == 10  # 混合口径（含探针）
    assert out["by_origin"]["probe:test_opening_directory_empty"]["records"] == 2


def test_delivery_stats_skip_probes_gives_production_numbers(tmp_path):
    p, now = _ledger(tmp_path)
    mixed = dl.delivery_stats(days=1, path=p, now=now)
    prod = dl.delivery_stats(days=1, path=p, now=now,
                             skip_origins=["probe:test_opening_directory_empty"])
    # 顶层是**混合口径**（含探针，5 批/10 条）；生产质量看 `filtered`
    assert mixed["records"] == 5 and mixed["deliveries"] == 10
    assert prod["filtered"]["records"] == 3 and prod["filtered"]["deliveries"] == 6
    assert prod["filtered"]["distinct"] == 4
    assert prod["filtered"]["repeat_rate"] == round((6 - 4) / 6, 4)
    assert prod["filtered"]["max_repeat"] == 3
    # 关键：混合口径把夹具的重复算进了"重复率"，过滤后才看得见真实质量
    assert mixed["repeat_rate"] != prod["filtered"]["repeat_rate"]


def test_delivery_stats_only_class_prod_and_whitelist(tmp_path):
    p, now = _ledger(tmp_path)
    prod = dl.delivery_stats(days=1, path=p, now=now, only_class="prod")
    assert prod["filtered"]["records"] == 3
    only = dl.delivery_stats(days=1, path=p, now=now, origins=["dsh-plugin"])
    assert only["filtered"]["deliveries"] == 6
    assert only["filter"]["note"]


def test_ledger_read_failure_is_reported_not_raised(tmp_path):
    out = dl.delivery_stats(days=1, path=str(tmp_path / "missing.jsonl"))
    assert out["records"] == 0 and "note" in out
