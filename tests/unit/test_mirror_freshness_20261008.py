"""G17/t160 判据 —— **镜像新鲜度**（只读函数 + CLI 三态）。

对象：`scripts/mirror_freshness.py`（本轮新建，只读）
  · 数据源：SQLite `audit_log` 里 `PG_MIRROR_STATUS`/`PG_BACKFILL` 的 `max(timestamp)`
  · 输出：滞后 **小时数**（**tz-aware 相减**）+ `fresh`/`stale` + 退出码 0/1/2

五条：
  T1 反向（fresh 侧）：**刚刚跑过**的时间戳 ⇒ 报 fresh；
  T2 正向（stale 侧）：48 小时前 ⇒ 报 stale；
  T3 牙齿：**阈值调到 0** ⇒ 任何正滞后立刻 stale（且"正好=上限"仍算 fresh ⇒ 边界口径明确）；
  T4 tz 陷阱（t158 D1/D2 的教训）：`...Z` 与 `...+08:00` 必须解析成**同一个 aware 瞬间**，
     且**缺时区的字符串按 UTC** 解释 ⇒ **±28800 s 的整齐假读数在结构上不可能出现**；
  T5 CLI 三态集成：真库（临时）⇒ 0 fresh / 1 stale / **2 untestable**（无镜像行）。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "mirror_freshness.py")


def _load():
    spec = importlib.util.spec_from_file_location("_g17_fresh", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_g17_fresh"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()
T0 = "2026-10-07T19:05:04Z"          # = 2026-10-08 03:05:04 +08（t159 记的真实最近一条）


def test_T1_reverse_just_ran_is_fresh():
    now = M.parse_ts(T0) + timedelta(hours=2)
    h = M.age_hours(T0, now)
    assert abs(h - 2.0) < 1e-6, h
    assert M.classify(h, 24.0) == "fresh"


def test_T2_stale_side_is_detected():
    now = M.parse_ts(T0) + timedelta(hours=48)
    h = M.age_hours(T0, now)
    assert abs(h - 48.0) < 1e-6, h
    assert M.classify(h, 24.0) == "stale"


def test_T3_teeth_threshold_zero_forces_stale():
    h = M.age_hours(T0, M.parse_ts(T0) + timedelta(hours=10.96))
    assert M.classify(h, 0.0) == "stale", "阈值 0 时必须陈旧（否则阈值没起作用）"
    assert M.classify(0.0, 0.0) == "fresh", "正好等于上限 ⇒ fresh（**严格大于**才算陈旧）"


def test_T4_tz_aware_arithmetic_no_28800_trap():
    a = M.parse_ts("2026-10-07T19:05:04Z")
    b = M.parse_ts("2026-10-08 03:05:04+08:00")
    assert a.tzinfo is not None and b.tzinfo is not None, "必须先成 aware（t158 D1/D2 教训）"
    assert a == b, "同一瞬间的两种写法必须相等（否则会出现整齐的 ±28800 s 假读数）"
    naive = M.parse_ts("2026-10-07T19:05:04")          #: 缺时区 ⇒ 按 UTC
    assert naive.tzinfo == timezone.utc and naive == a
    assert abs((b - a).total_seconds()) == 0.0


def _mk_db(tmp: str, rows, name: str = "t1.db"):
    """⚠️ 两条自伤都已修：
    (1) `with sqlite3.connect(...)` **只提交、不关闭** ⇒ Windows 上文件被占 ⇒ `TemporaryDirectory`
        清理报 `PermissionError [WinError 32]`（**同族第二次**：t156 判据也踩过）；
    (2) 同一个 tmp 目录里建**第二个**库必须换文件名 ⇒ 否则 `table audit_log already exists`
        （本轮实测：这才是 T5 的真失败点，不是 teardown）。"""
    db = os.path.join(tmp, name)
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, action TEXT, timestamp TEXT)")
        for i, (act, ts) in enumerate(rows):
            con.execute("INSERT INTO audit_log (id, action, timestamp) VALUES (?,?,?)", (i, act, ts))
        con.commit()
    finally:
        con.close()
    return db


def _run(db, *args):
    r = subprocess.run([sys.executable, SCRIPT, "--db", db, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def test_T5_cli_three_states():
    with tempfile.TemporaryDirectory(prefix="g17_") as tmp:
        #: ① 新鲜（把"现在"钉在镜像后 2 小时）
        db = _mk_db(tmp, [("PG_MIRROR_STATUS", "2026-10-07T19:05:04Z")])
        rc, out = _run(db, "--now", "2026-10-07T21:05:04Z")
        assert rc == 0 and "fresh" in out, (rc, out)
        #: ② 陈旧（把"现在"钉在镜像后 48 小时）
        rc, out = _run(db, "--now", "2026-10-09T19:05:04Z")
        assert rc == 1 and "stale" in out, (rc, out)
        #: ③ 无镜像行 ⇒ 无法判定（退出码 2，不是 0/1）
        db2 = _mk_db(tmp, [("SOMETHING_ELSE", "2026-10-07T19:05:04Z")], name="t2.db")
        rc, out = _run(db2)
        assert rc == 2 and "untestable" in out, (rc, out)
        #: ④ 只在"真库缺失"时才 2：显式给不存在的库
        rc, out = _run(os.path.join(tmp, "nope.db"))
        assert rc == 2, (rc, out)
