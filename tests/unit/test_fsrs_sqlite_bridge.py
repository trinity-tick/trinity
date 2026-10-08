# -*- coding: utf-8 -*-
"""`brain_cycle._fsrs_sqlite` —— 把 FSRS 接到服务中的 SQLite 库（Step 4b / D28）。

## 这个桥为什么必须存在

`brain_cycle.step_fsrs` 只连 PostgreSQL（`pg_connect()`），而**常驻 API 服务的是 SQLite**
（`TRINITY_STORE=~/.trinity/store-restored`；D28 已定保持 SQLite）。实测代价（2026-10-05）：
27,034 条 active 里 **26,848 条无任何排期**，仅存的 186 条全部逾期且是别处的一次性产物。

## 本文件钉住的失败形态

1. **失败必须留痕、不许抛出**：每日链上一个子步骤失败不该终止整轮，
   但也**绝不能静默**（§13.5：写侧错误不报错，只表现为「没有效果」）。
2. **dry 必须真的不写**：否则「只看一眼」会变成生产写入。
3. **导入失败要单独成一档**：`review_schedule_sqlite` 不在 `scripts/` 的 sys.path 上时，
   要报 `import_error` 而不是伪装成 `error` 或让整轮崩掉。
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name, relpath):
    path = os.path.join(ROOT, relpath)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def bc():
    """载入 brain_cycle（模块级无 DB 连接；pg_connect 是函数）。"""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    if os.path.join(ROOT, "scripts") not in sys.path:
        sys.path.insert(0, os.path.join(ROOT, "scripts"))
    return _load("brain_cycle", "scripts/brain_cycle.py")


@pytest.fixture(scope="module")
def rs():
    return _load("review_schedule_sqlite", "scripts/review_schedule_sqlite.py")


def _mk_store(tmp_path):
    p = os.path.join(str(tmp_path), "store.db")
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE memories (memory_id TEXT, agent_id TEXT, category TEXT,
        status TEXT, importance TEXT, review_interval_days TEXT, next_review_at TEXT,
        access_count TEXT)""")
    con.execute("INSERT INTO memories VALUES ('m1','dsh','knowledge','active','0.9','0',NULL,'0')")
    con.execute("INSERT INTO memories VALUES ('m2','dsh','perception','active','0.9','0',NULL,'0')")
    con.commit()
    con.close()
    return p


@pytest.fixture(autouse=True)
def _isolate(rs, tmp_path, monkeypatch):
    """钉住策略/排除面/备份目录，避免碰到真实环境与 ~/.trinity。

    ⚠ 必须**依赖 `rs` fixture**：本夹具是 autouse，若不声明依赖，它可能在
    `review_schedule_sqlite` 被载入**之前**运行 ⇒ `sys.modules[...]` KeyError。
    （且一律用模块对象而不是 `sys.modules[...]` 查表 —— 有一个用例会把该键置 None
    来复现导入失败，查表会跟着炸。）
    """
    monkeypatch.setattr(rs, "load_tiers",
                        lambda: ([{"min_imp": 0.8, "days": 30}, {"min_imp": 0.6, "days": 60}], "t"))
    monkeypatch.setattr(rs, "load_engine_exclusions", lambda: (["perception"], "t"))
    monkeypatch.setattr(rs, "load_default_days", lambda: 90)
    monkeypatch.setattr(rs, "load_budget", lambda: 500)
    monkeypatch.setattr(rs, "BACKUP_ROOT", os.path.join(str(tmp_path), "bk"))


def test_dry_run_does_not_write(bc, tmp_path, monkeypatch):
    p = _mk_store(tmp_path)
    monkeypatch.setattr(sys.modules["review_schedule_sqlite"], "resolve_store",
                        lambda explicit="": (p, "test"))
    r = bc._fsrs_sqlite(dry=True)
    assert r["status"] == "dry_run" and r["verdict"] == "OK", r
    con = sqlite3.connect(p)
    assert con.execute("SELECT next_review_at FROM memories WHERE memory_id='m1'"
                       ).fetchone()[0] is None, "dry 必须真的不写"
    con.close()


def test_apply_writes_and_consumes(bc, tmp_path, monkeypatch):
    p = _mk_store(tmp_path)
    monkeypatch.setattr(sys.modules["review_schedule_sqlite"], "resolve_store",
                        lambda explicit="": (p, "test"))
    r = bc._fsrs_sqlite(dry=False)
    assert r["status"] == "ok", r
    assert r["next_written"] == 1, "只排可检索面（perception 被 §13.0 排除）"
    con = sqlite3.connect(p)
    nxt = con.execute("SELECT next_review_at FROM memories WHERE memory_id='m1'"
                      ).fetchone()[0]
    exc = con.execute("SELECT next_review_at FROM memories WHERE memory_id='m2'"
                      ).fetchone()[0]
    acc = con.execute("SELECT access_count FROM memories WHERE memory_id='m1'"
                      ).fetchone()[0]
    con.close()
    assert nxt is not None and exc is None, "排除类目不得被排期"
    assert int(acc) == 0, "默认不得动 access_count（否则冷率读数做假）"
    assert r["touch_access_count"] is False


def test_error_is_reported_not_raised(bc, monkeypatch):
    """解析/写入失败必须**返回** error+原因，绝不抛给每日链调用方。"""
    def boom(explicit=""):
        raise RuntimeError("simulated store failure")
    monkeypatch.setattr(sys.modules["review_schedule_sqlite"], "resolve_store", boom)
    r = bc._fsrs_sqlite(dry=True)
    assert r["status"] == "error"
    assert "simulated store failure" in r["detail"], "失败原因必须留痕"


def test_import_error_is_its_own_status(bc, monkeypatch):
    """导入失败单独一档 —— 不许伪装成 error，也不许让整轮崩掉。"""
    monkeypatch.setitem(sys.modules, "review_schedule_sqlite", None)
    r = bc._fsrs_sqlite(dry=True)
    assert r["status"] == "import_error"
    assert "detail" in r


def test_step_fsrs_return_carries_sqlite_block(bc, monkeypatch):
    """step_fsrs 的返回体必须带上 sqlite 块（否则「接上了」在读数上不可见 —— §13.5）。"""
    src = open(os.path.join(ROOT, "scripts", "brain_cycle.py"), encoding="utf-8").read()
    assert '"sqlite": _sq' in src, "step_fsrs 必须把 sqlite 读数带出，否则无法从产物判断是否生效"
