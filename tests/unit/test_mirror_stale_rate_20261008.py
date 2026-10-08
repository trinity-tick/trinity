"""G56/t199 判据 —— **per-retrieval 陈旧率 + 退化测试**（纯函数 + CLI 三态；不依赖活库）。

对象：`scripts/mirror_stale_rate.py`（本轮新建；只读）
  · **L1 抽样陈旧率**：`滞后 = PG.updated_at − SQLite.updated_at` ⇒ `> 阈值` 记陈旧；
  · **L1 可读天花板**：`PG.max(created_at) − SQLite.max(created_at)` ⇒ `> 阈值` ⇒ **有"读不到的更新内容"**；
  · **L2 退化测试**：`窗口内 PG 独有行数` + `最旧一条已存在多久`。

五条：
  T1 正向：混合滞后 ⇒ 陈旧数/率正确，且 `classify_rate` 按阈值给 stale；
  T2 **反向**：**陈旧率为 0** 的样本 ⇒ 报 fresh（"若陈旧率为 0 会看到什么"）；
  T3 **牙齿**：① **负滞后（副本反而更新）不计入陈旧**（本轮实测 SQLite 侧 `updated_at` 会被本地改写）；
              ② **大滞后必须被算进陈旧**（已知陈旧样本的分辨力）；
  T4 **tz 陷阱**：跨库时间**必须先成 aware**（`...Z` 与 `...+08:00` 是同一瞬间 ⇒ 差 0.0 h，不出现 ±28800 s 假读数）；
  T5 CLI 三态：`--skip-per-retrieval` 时仍给出全局行（rc 由可测侧决定）· 无镜像行 ⇒ **rc=2 不可判定**。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "mirror_stale_rate.py")


def _load():
    spec = importlib.util.spec_from_file_location("_g56_test_msr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_g56_test_msr"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load()


def test_T1_positive_rate_and_verdict():
    s = M.summarize_lags([0.2, 3.0, 0.5], 1.0)
    assert s["n"] == 3 and s["stale"] == 1, s
    assert abs(s["rate"] - (1 / 3)) < 1e-9, s
    assert M.classify_rate(s["rate"], 0.0) == "stale"
    assert M.classify_rate(s["rate"], 0.9) == "fresh"          #: 上限放宽 ⇒ 不报


def test_T2_reverse_zero_rate_reads_fresh():
    """⭐ 反向：陈旧率为 0 时必须报 fresh（证明"0"与"有陈旧"两侧都测得到）。"""
    s = M.summarize_lags([0.1, 0.2, 0.05], 1.0)
    assert s["stale"] == 0 and s["rate"] == 0.0, s
    assert M.classify_rate(s["rate"], 0.0) == "fresh"
    assert M.summarize_lags([], 1.0)["rate"] is None            #: 空样本 ⇒ 不可判定，不是 0
    assert M.classify_rate(None, 0.0) == "untestable"


def test_T3_teeth_negative_not_stale_and_big_lag_is_stale():
    s = M.summarize_lags([-12.8, -12.8, 0.0], 1.0)
    assert s["stale"] == 0 and s["copy_newer"] == 2, s          #: 负滞后单列，不算陈旧
    t = M.summarize_lags([6.76], 1.0)
    assert t["stale"] == 1 and t["rate"] == 1.0, t              #: 大滞后必须被算进陈旧
    assert M.classify_rate(t["rate"], 0.0) == "stale"


def test_T4_tz_aware_no_28800_trap():
    """⭐ 跨库时间必须先成 aware；⭐ 并**把"缺时区按 UTC"这条约定写成断言**（它就是"±8h 假读数"的来源）。"""
    a = M.lag_hours("2026-10-08 03:05:04+08:00", "2026-10-07T19:05:04Z")
    assert abs(a) < 1e-9, "同一瞬间必须差 0（否则是时区假读数）：%r" % a
    c = M.lag_hours("2026-10-07T19:05:04Z", "2026-10-08T03:05:04+08:00")
    assert abs(c) < 1e-9, c
    #: ⚠️ **约定**：缺时区的字符串**按 UTC 解释** ⇒ 若它其实是 +08 的本地时间，就会差 **正好 8.0 h**
    #: （不是 28800 s 的随机错，而是**可解释的** 8h）⇒ 因此 `mirror_freshness.py::assumed_tz` 必须被打印出来。
    b = M.lag_hours("2026-10-08T03:05:04", "2026-10-07T19:05:04Z")
    assert abs(abs(b) - 8.0) < 1e-9, ("缺时区 ⇒ 按 UTC 解释，故与 +08 的写法差 8h（**约定，已在输出里标记 "
                                      "assumed_tz**）：%r" % b)
    assert abs(b) != 28800.0


def _mk_db(tmp: str, rows, name="t1.db"):
    db = os.path.join(tmp, name)
    con = sqlite3.connect(db)
    try:
        con.execute("CREATE TABLE audit_log (id INTEGER PRIMARY KEY, action TEXT, timestamp TEXT)")
        for i, (act, ts) in enumerate(rows):
            con.execute("INSERT INTO audit_log (id, action, timestamp) VALUES (?,?,?)", (i, act, ts))
        con.execute("CREATE TABLE memories (memory_id TEXT PRIMARY KEY, created_at TEXT, "
                    "updated_at TEXT)")
        con.commit()
    finally:
        con.close()
    return db


def _run(db, *args):
    r = subprocess.run([sys.executable, SCRIPT, "--db", db, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def test_T5_cli_three_states():
    with tempfile.TemporaryDirectory(prefix="g56_") as tmp:
        #: ① 全局新鲜 + per-retrieval 跳过 ⇒ rc=0，且**两个指标都出现在同一张表里**
        db = _mk_db(tmp, [("PG_MIRROR_STATUS", "2026-10-07T19:05:04Z")])
        rc, out = _run(db, "--now", "2026-10-07T21:05:04Z", "--skip-per-retrieval")
        assert rc == 0, (rc, out)
        assert "全局 max-age" in out and "L1" in out and "L2" in out, out
        #: ② 很旧的镜像 ⇒ rc=1
        rc, out = _run(db, "--now", "2026-10-09T19:05:04Z", "--skip-per-retrieval")
        assert rc == 1 and "stale" in out, (rc, out)
        #: ③ 无镜像行 ⇒ 不可判定 rc=2（不是 0）
        db2 = _mk_db(tmp, [("SOMETHING_ELSE", "2026-10-07T19:05:04Z")], name="t2.db")
        rc, out = _run(db2, "--skip-per-retrieval")
        assert rc == 2, (rc, out)
