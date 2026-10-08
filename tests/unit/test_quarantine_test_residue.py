# -*- coding: utf-8 -*-
"""测试残留隔离（Step 1 隔离半）判据测试。

## 钉住的失败形态

1. **只动无歧义残留**：`reader*` / `u1` / `usage-feedback`（疑似合法子 agent）与
   任何 `machine_self` 管道名空间**都不得被判为目标** —— 误归档会打断管道/丢真实数据。
2. **§13.0**：已在排除类目里的行**不再动**，且必须**显式报数**。
3. **非破坏 + 可回滚**：只把 `status` 改成 `archived`（不删除），
   且回滚清单能把它们原样还原。
4. **默认不动不可归属行**：`agent_id` 为空的行只有显式加 `--include-unattributed` 才处理。
5. **幂等**：重复 apply 第二次归档 0 行。
6. **幂等/取不到 ⇒ INCONCLUSIVE**，不得静默当作通过（§13.2）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "quarantine_test_residue.py")
    spec = importlib.util.spec_from_file_location("quarantine_test_residue", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quarantine_test_residue"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _mk(tmp_path, rows):
    """rows = (memory_id, agent_id, category, status)"""
    p = os.path.join(str(tmp_path), "store.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT, agent_id TEXT, "
                "category TEXT, status TEXT)")
    for r in rows:
        con.execute("INSERT INTO memories VALUES (?,?,?,?)", r)
    con.commit()
    con.close()
    return p


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """把备份目录与 `output/` 都改到 tmp —— 不碰真实的 `~/.trinity` 与仓库 output/。

    ⚠ 不要打桩 `os.makedirs`：`backup_store` 真的需要那个目录存在才能 `copy2`，
    打桩后会在 **copy 阶段**报 FileNotFoundError（测不到被测逻辑）。
    把 `ROOT` 指向 tmp 就够了（回滚清单写在 `<ROOT>/output/`）。
    """
    monkeypatch.setattr(mod, "load_exclusions", lambda: (["perception"], "test"))
    monkeypatch.setattr(mod, "BACKUP_ROOT", os.path.join(str(tmp_path), "bk"))
    monkeypatch.setattr(mod, "ROOT", str(tmp_path))


# ---- 1. 只动无歧义残留 -------------------------------------------------------

@pytest.mark.parametrize("agent,expect", [
    ("cache_test", True), ("kw_test", True), ("t1", True), ("a", True),
    ("b1", True), ("sig_0", True), ("_warmup", True), ("probe-agent", True),
    ("smoke", True), ("test-vec-agent", True), ("ingest_test", True),
    # 反面：疑似合法子 agent 一律不得判为目标
    ("reader", False), ("reader-ops", False), ("reader-value", False),
    ("usage-feedback", False), ("u1", False),
    # 反面：machine_self 管道名空间是**管道状态**，不是残留
    ("brain-procedure", False), ("brain-cycle", False), ("memory-revival", False),
    ("recurrence-consolidate", False), ("evolution", False), ("loop-compress-1", False),
    ("kb-harvester", False), ("doc-fusion", False), ("", False),
])
def test_only_unambiguous_residue_is_targeted(agent, expect):
    assert mod.is_residue_agent(agent) is expect


def test_reader_and_machine_self_are_not_selected(tmp_path):
    """端到端反向断言：误归档真实/管道数据是破坏性操作。"""
    p = _mk(tmp_path, [
        ("m1", "cache_test", "general", "active"),        # 目标
        ("m2", "reader-ops", "observation", "active"),    # 不得动
        ("m3", "brain-procedure", "procedural", "active"),  # 不得动
        ("m4", "kb-harvester", "kb_harvested", "active"),  # 不得动
    ])
    r = mod.apply_quarantine(p)
    assert r["verdict"] == "OK" and r["archived"] == 1
    con = sqlite3.connect(p)
    got = dict(con.execute("SELECT memory_id, status FROM memories"))
    con.close()
    assert got["m1"] == "archived"
    for m in ("m2", "m3", "m4"):
        assert got[m] == "active", "%s 不得被归档" % m


# ---- 2. §13.0：排除类目不动 + 显式报数 ---------------------------------------

def test_already_excluded_rows_untouched_and_reported(tmp_path):
    p = _mk(tmp_path, [
        ("m1", "cache_test", "general", "active"),
        ("m2", "smoke", "perception", "active"),       # 已在排除类目 ⇒ 不处理
    ])
    pl = mod.plan(p, "test")
    assert pl["verdict"] == "OK"
    assert pl["targets"] == 1, "排除类目里的残留不该被处理"
    assert pl["already_excluded_rows"] == 1, "必须显式报数（§13.0 附带纪律）"
    mod.apply_quarantine(p)
    con = sqlite3.connect(p)
    assert con.execute("SELECT status FROM memories WHERE memory_id='m2'"
                       ).fetchone()[0] == "active", "排除类目里的行不得被动"
    con.close()


# ---- 3. 非破坏 + 可回滚 ------------------------------------------------------

def test_apply_is_archive_not_delete_and_rollback_restores(tmp_path):
    p = _mk(tmp_path, [("m1", "cache_test", "general", "active"),
                       ("m2", "t1", "test", "active")])
    r = mod.apply_quarantine(p)
    assert r["archived"] == 2 and r["still_active_after"] == 0
    con = sqlite3.connect(p)
    assert con.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 2, "不得删除行"
    con.close()
    assert os.path.exists(r["rollback_manifest"]), "必须落回滚清单"

    rb = mod.rollback(p, r["rollback_manifest"])
    assert rb["verdict"] == "OK" and rb["restored"] == 2
    assert rb["still_archived"] == 0
    con = sqlite3.connect(p)
    assert all(s == "active" for (s,) in con.execute("SELECT status FROM memories"))
    con.close()


def test_rollback_manifest_records_prior_status(tmp_path):
    """清单必须记下原 status，否则回滚就是猜。"""
    p = _mk(tmp_path, [("m1", "cache_test", "general", "active")])
    r = mod.apply_quarantine(p)
    data = json.load(open(r["rollback_manifest"], encoding="utf-8"))
    assert data["rows"][0]["memory_id"] == "m1"
    assert data["rows"][0]["status"] == "active"
    assert "rollback_sql" in data


def test_rollback_bad_manifest_fails_loudly(tmp_path):
    p = _mk(tmp_path, [("m1", "cache_test", "general", "active")])
    r = mod.rollback(p, os.path.join(str(tmp_path), "nope.json"))
    assert r["verdict"] == "FAILED" and "unreadable" in r["error"]


# ---- 4. 不可归属行默认不动 ---------------------------------------------------

def test_unattributed_default_off(tmp_path):
    p = _mk(tmp_path, [("m1", "", "general", "active"),
                       ("m2", "cache_test", "general", "active")])
    pl = mod.plan(p, "test")
    assert pl["targets"] == 1, "默认不得处理 agent_id 为空的行"
    assert pl["unattributed_present"] == 1, "但要报告它的存在"


def test_unattributed_opt_in(tmp_path):
    p = _mk(tmp_path, [("m1", "", "general", "active")])
    pl = mod.plan(p, "test", include_unattributed=True)
    assert pl["targets"] == 1


# ---- 5. 幂等 -----------------------------------------------------------------

def test_apply_is_idempotent(tmp_path):
    p = _mk(tmp_path, [("m1", "cache_test", "general", "active")])
    assert mod.apply_quarantine(p)["archived"] == 1
    assert mod.apply_quarantine(p)["archived"] == 0, "第二次不得重复归档"


# ---- 6. 取不到 ⇒ INCONCLUSIVE ------------------------------------------------

def test_missing_store_inconclusive(tmp_path):
    r = mod.plan(os.path.join(str(tmp_path), "no.db"), "test")
    assert r["verdict"] == "INCONCLUSIVE" and "not found" in r["error"]


def test_exclusions_unavailable_inconclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "load_exclusions", lambda: ([], "UNAVAILABLE: boom"))
    p = _mk(tmp_path, [("m1", "cache_test", "general", "active")])
    r = mod.plan(p, "test")
    assert r["verdict"] == "INCONCLUSIVE" and "unavailable" in r["error"]
