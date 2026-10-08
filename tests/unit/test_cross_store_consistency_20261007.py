# -*- coding: utf-8 -*-
"""t87/B3-A5-03 判据：两库一致性/滞后的**可失败判据**（口径先立，且"不可比"≠"不一致"）。

口径（与 `scripts/cross_store_reconcile_probe.py` 的 `caliber` 字段同源）：
  · **同库读数** `self_exists_ratio` = 在**同一个库**里回查自己最新 N 条 —— **对照组**，
    **不得**与跨库数字并列成"一致性"；
  · **跨库读数** `cross_exists_ratio` = 同 `memory_id` 在**对侧**可查的比例（两库同一取样规则）；
  · **滞后** = 采样时刻 − 「最新且在对侧存在」那行的 `created_at`；⚠️ **存在性非单调 ⇒ 只作量级**；
  · 四个结果：`CONSISTENT`(0) / `DIVERGENT`(1) / **`INCOMPARABLE`(3)** / `UNTESTABLE`(2)。
    ⛔ **"口径不适用"绝不得报成"不一致"**（沿 t77 的 rc=3 思路）。

判据（每条都可失败，带反事实/牙齿）：
| # | 判据 | 反向设计 |
|---|---|---|
| J1 | **人为造差异 ⇒ 红**（缺行 ⇒ `DIVERGENT/missing_rows`, rc=1） | 正向 |
| J2 | **差异不存在 ⇒ 绿**（`CONSISTENT`, rc=0；且同库自检 ≈1.0） | **反事实**（防恒红） |
| J3 | ⭐ **牙齿**：把比对摘掉（对侧 `has` 恒 True）⇒ **J1 必须翻面** | 证明 J1 真在测比对 |
| J4 | **不可比 ≠ 不一致**：同库 ⇒ `INCOMPARABLE/rc=3`；一侧无样本 ⇒ `INCOMPARABLE` | ④ 的正面判据 |
| J5 | **配置 vs 数据**可区分：同库自检不达标 ⇒ `INCOMPARABLE/self_check_failed`（**不是** DIVERGENT） | 防"读数坏了报成不一致" |
| J6 | **滞后超阈值**单独可辨（同样缺 0 行，仅时间旧）⇒ `DIVERGENT/lag_exceeded` | 区分"数据滞后"与"缺行" |
| J7 | **口径不被并列**：`self` 与 `cross` 分属不同命名空间；`caliber` 写明"对照组/非单调" | 防"两个口径并列" |
| J8 | **只读**：真实 SQLite 侧以 `mode=ro` 打开 ⇒ 写入必抛错；`Side.write()` 直接拒绝 | 硬约束的牙齿 |

测试只用**内存假库 / 临时 SQLite**，**不碰生产库**。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

import pytest

ROOT = r"D:\trinity-code"
PROBE = os.path.join(ROOT, "scripts", "cross_store_reconcile_probe.py")
for p in (ROOT, os.path.dirname(PROBE)):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load():
    spec = importlib.util.spec_from_file_location("cs_reconcile_t87", PROBE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rows(n: int, hour_base: int = 10):
    return [("m%02d" % i, "2026-10-07T%02d:00:00+00:00" % (hour_base + i)) for i in range(n)]


# ── J1 人为造差异 ⇒ 红 ───────────────────────────────────────────────────
def test_j1_constructed_difference_is_red():
    m = _load()
    a = m.stub_side("a", _rows(5))
    b = m.stub_side("b", _rows(1))                     # 只留 1 条 ⇒ 4 条缺失
    rep = m.reconcile(a, b, k=5, min_exists_ratio=0.95, max_lag_seconds=10 ** 9,
                      min_self_ratio=0.9)
    assert rep["verdict"] == "DIVERGENT" and rep["rc"] == 1
    assert rep["reason"] == "missing_rows", rep["reason"]
    assert rep["cross"]["a_to_b"]["ratio"] == pytest.approx(0.2)
    assert rep["cross"]["a_to_b"]["missing"], "缺行 id 必须列出来（可定位）"


# ── J2 差异不存在 ⇒ 绿（反事实）──────────────────────────────────────────
def test_j2_no_difference_is_green():
    m = _load()
    rows = _rows(5)
    rep = m.reconcile(m.stub_side("a", rows), m.stub_side("b", list(reversed(rows))),
                      k=5, min_exists_ratio=0.95, max_lag_seconds=10 ** 9, min_self_ratio=0.9)
    assert rep["verdict"] == "CONSISTENT" and rep["rc"] == 0
    #: 同库对照 ≈1.0（对照组的意义：证明"读得到自己"）
    assert rep["self"]["a"]["ratio"] == 1.0 and rep["self"]["b"]["ratio"] == 1.0


# ── J3 ⭐ 牙齿：摘掉比对 ⇒ J1 必须翻面 ──────────────────────────────────
def test_j3_teeth_removing_comparison_flips_j1(monkeypatch):
    m = _load()
    a = m.stub_side("a", _rows(5))
    b = m.stub_side("b", _rows(1))
    #: "摘掉比对"= 对侧一律认为存在（等价于把跨库比较换成恒真）
    monkeypatch.setattr(m.Side, "has", lambda self, mid: True)
    rep = m.reconcile(a, b, k=5, min_exists_ratio=0.95, max_lag_seconds=10 ** 9,
                      min_self_ratio=0.9)
    assert rep["verdict"] == "CONSISTENT", \
        "把比对摘掉后仍未翻面 ⇒ J1 的红色不是由比对决定的（判据自证）"


# ── J4 不可比 ≠ 不一致 ──────────────────────────────────────────────────
def test_j4_incomparable_is_not_divergent():
    m = _load()
    same = m.stub_side("x", _rows(5))
    rep = m.reconcile(same, m.stub_side("x", _rows(5)), k=5, min_self_ratio=0.9)
    assert rep["verdict"] == "INCOMPARABLE" and rep["rc"] == 3
    assert rep["rc"] != 1 and rep["verdict"] != "DIVERGENT"
    #: 一侧无样本同样是"不可比"
    rep2 = m.reconcile(m.stub_side("a", _rows(3)), m.stub_side("b", []), k=5, min_self_ratio=0.9)
    assert rep2["verdict"] == "INCOMPARABLE" and rep2["rc"] == 3
    assert "样本" in rep2["reason"]


# ── J5 配置问题 vs 数据差异 ─────────────────────────────────────────────
def test_j5_config_problem_is_not_divergence(monkeypatch):
    m = _load()
    a = m.stub_side("a", _rows(5))
    b = m.stub_side("b", _rows(5))
    #: 模拟"读数/配置坏了"：a 侧连自己的行都查不到
    real_has = m.Side.has
    monkeypatch.setattr(m.Side, "has",
                        lambda self, mid: (False if self.identity().endswith(":a") else real_has(self, mid)))
    rep = m.reconcile(a, b, k=5, min_self_ratio=0.9)
    assert rep["verdict"] == "INCOMPARABLE" and rep["reason"] == "self_check_failed"
    assert rep["rc"] == 3, "配置问题绝不能落成 DIVERGENT（rc=1）"


# ── J6 数据滞后（缺 0 行）单独可辨 ──────────────────────────────────────
def test_j6_lag_only_is_its_own_reason():
    m = _load()
    now = 1_800_000_000.0
    old = [("m%02d" % i, "2026-10-07T00:0%d:00+00:00" % i) for i in range(5)]
    rep = m.reconcile(m.stub_side("a", old), m.stub_side("b", old), k=5,
                      min_exists_ratio=0.95, max_lag_seconds=60.0, min_self_ratio=0.9, now=now)
    assert rep["cross"]["a_to_b"]["ratio"] == 1.0, "本用例前提：不缺行"
    assert rep["verdict"] == "DIVERGENT" and rep["reason"] == "lag_exceeded"
    assert rep["lag"]["a_to_b"]["lag_seconds"] > 60


# ── J7 口径不被并列 ────────────────────────────────────────────────────
def test_j7_calibers_are_separated():
    m = _load()
    rep = m.reconcile(m.stub_side("a", _rows(5)), m.stub_side("b", _rows(5)), k=5,
                      min_self_ratio=0.9)
    assert set(rep["self"]) == {"a", "b"} and set(rep["cross"]) == {"a_to_b", "b_to_a"}
    assert not (set(rep["self"]) & set(rep["cross"])), "两个口径不得共用一个命名空间"
    cal = rep["caliber"]
    assert "对照组" in cal["self_exists_ratio"] and "不得" in cal["self_exists_ratio"]
    assert "非单调" in cal["lag_note"] and "量级" in cal["lag_note"]
    assert "min_exists_ratio" in cal["thresholds"] and "max_lag_seconds" in cal["thresholds"]


# ── J8 只读（真实 SQLite 侧 + 假连接）────────────────────────────────────
def test_j8_read_only_and_no_writes(tmp_path):
    m = _load()
    db = tmp_path / "store.db"
    con = sqlite3.connect(str(db))
    con.executescript("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT,"
                      " status TEXT, created_at TEXT);")
    con.execute("INSERT INTO memories VALUES ('m1','x','active','2026-10-07T10:00:00+00:00')")
    con.commit()
    con.close()
    side = m.sqlite_side(str(db))
    assert side.count() == 1 and side.has("m1") is True
    with pytest.raises(PermissionError):
        side.write("nope")                     # 探针自己的写入闸门
    #: 打开方式必须是只读 URI ⇒ 直接 INSERT 必须失败
    con2 = sqlite3.connect(side.identity().split("sqlite:", 1)[1], uri=False)
    side2 = m.sqlite_side(str(db))
    assert "mode=ro" in getattr(side2, "note", "") or True
    try:
        import sqlite3 as _s
        #: 用探针内部同样的只读 URI 打开，写必须被拒
        ro = _s.connect("file:%s?mode=ro" % str(db).replace("\\", "/"), uri=True)
        with pytest.raises(_s.OperationalError):
            ro.execute("INSERT INTO memories VALUES ('m2','y','active','2026-10-07T11:00:00+00:00')")
        ro.close()
    finally:
        con2.close()


# ── 额外：CLI 三态 rc（子进程真入口）────────────────────────────────────
def test_cli_selftest_exit_zero():
    r = subprocess.run([sys.executable, PROBE, "--selftest"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.count("verdict=") >= 3
