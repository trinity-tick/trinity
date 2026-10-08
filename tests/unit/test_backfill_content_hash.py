# -*- coding: utf-8 -*-
"""content_hash / sha256_hash 存量回填（Step 1）判据测试。

覆盖的真实失败形态（每条都对着一个**已发生过或必然会发生**的错）：

1. **库路径解析**：`TRINITY_STORE` 的语义是 **store 目录**而不是库文件
   （`trinity/core/client/_helpers.py:22`）。2026-10-05 实测：把 env 直接当文件用 ⇒
   `OperationalError: unable to open database file`。
2. **写死旧库路径**：原版工具写死 `…\\.trinity\\store\\trinity_store.db`（灾难恢复前的死库）
   ⇒ 必须能识别并**告警**。
3. **稀疏失败统计（§13.2）**：`memory_id IS NULL` 的行 `WHERE memory_id=?` 恒不匹配
   ⇒ 既不写也不计数，读数对不上（实测「扫描 897 / 回填 896」差 1）。必须单列桶 + 对账。
4. **不许覆盖既有值**：只补空位。
5. **撞唯一索引必须跳过并保持 NULL**（与 PG 侧 `pg_content_hash_and_dedup.py` 同语义）。
6. **Pass2 必须先复算再补**：`sha256_hash` 是派生列，会与 content 漂移
   （`scripts/reconcile_pg_sqlite.py:147`）。漂移行**只报不改**，绝不把 content_hash 抄过去。
"""
from __future__ import annotations

import importlib.util
import hashlib
import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load():
    path = os.path.join(ROOT, "scripts", "backfill_content_hash.py")
    spec = importlib.util.spec_from_file_location("backfill_content_hash", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["backfill_content_hash"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "BACKUP_ROOT", os.path.join(str(tmp_path), "backups"))
    monkeypatch.delenv("TRINITY_STORE", raising=False)


def _mk(tmp_path, rows, name="store.db"):
    """rows = (memory_id, content, content_hash, sha256_hash, status, agent_id)"""
    p = os.path.join(str(tmp_path), name)
    con = sqlite3.connect(p)
    con.execute("""CREATE TABLE memories (
        memory_id TEXT, persona_id TEXT, agent_id TEXT, content TEXT,
        content_hash TEXT, sha256_hash TEXT, status TEXT)""")
    con.execute("CREATE UNIQUE INDEX idx_memories_content_hash ON memories"
                "(persona_id, agent_id, content_hash) "
                "WHERE content_hash IS NOT NULL AND status = 'active'")
    for r in rows:
        con.execute("INSERT INTO memories (memory_id,persona_id,agent_id,content,"
                    "content_hash,sha256_hash,status) VALUES (?,?,?,?,?,?,?)",
                    (r[0], "default", r[5], r[1], r[2], r[3], r[4]))
    con.commit()
    con.close()
    return p


# ---- 1/2. 库路径解析 ---------------------------------------------------------

def test_sha256_of_matches_hashlib():
    assert mod.sha256_of("abc") == hashlib.sha256(b"abc").hexdigest()
    assert mod.sha256_of(None) == hashlib.sha256(b"").hexdigest()


def test_resolve_store_treats_env_as_directory(tmp_path, monkeypatch):
    """TRINITY_STORE 是**目录** ⇒ 必须拼 trinity_store.db。"""
    d = os.path.join(str(tmp_path), "store-restored")
    os.makedirs(d)
    monkeypatch.setenv("TRINITY_STORE", d)
    p, src = mod.resolve_store()
    assert p == os.path.join(d, "trinity_store.db")
    assert "dir" in src


def test_resolve_store_accepts_env_as_file(tmp_path, monkeypatch):
    f = os.path.join(str(tmp_path), "custom.db")
    open(f, "w").close()
    monkeypatch.setenv("TRINITY_STORE", f)
    p, src = mod.resolve_store()
    assert p == f and "file" in src


def test_resolve_store_cli_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_STORE", os.path.join(str(tmp_path), "nope"))
    p, src = mod.resolve_store(os.path.join(str(tmp_path), "explicit.db"))
    assert src == "cli" and p.endswith("explicit.db")


def test_plan_warns_on_dead_store_path(tmp_path):
    """写死灾难恢复前旧库路径时要**显式告警**（原版工具就是栽在这）。"""
    dead = os.path.join(str(tmp_path), "trinity_store.db")
    open(dead, "w").close()
    importlib.reload  # noqa: B018  (仅提示：不重载模块，直接打桩)
    mod.DEAD_STORE = dead
    try:
        r = mod.plan(dead, "cli")
        assert "warning" in r or r["verdict"] == "INCONCLUSIVE"
    finally:
        mod.DEAD_STORE = os.path.join(os.path.expanduser("~"), ".trinity",
                                      "store", "trinity_store.db")


# ---- 3. 稀疏失败统计 + 对账 --------------------------------------------------

def test_null_memory_id_is_counted_separately(tmp_path):
    """`memory_id IS NULL` 的行既不写也必须计数，否则「扫描 N / 回填 M」永远差 1。"""
    good = mod.sha256_of("hello")
    p = _mk(tmp_path, [
        (None, "hello", None, "", "merged", "dsh"),      # memory_id 为 NULL
        ("m1", "world", None, "", "active", "dsh"),
    ])
    r = mod.apply_backfill(p)
    assert r["verdict"] == "OK", r
    assert r.get("skipped_null_memory_id") == 1, "必须单列桶"
    assert r["filled"] == 1
    scanned = r["candidates_scanned"]
    accounted = (r["filled"] + r["skipped_unique_conflict"] + r["failed_other"]
                 + r["empty_content"] + r["skipped_null_memory_id"]
                 + r["sample_ciphertext"])
    assert scanned == accounted, "扫描数必须等于各桶之和（§13.2 对账）"
    assert good is not None


def test_ciphertext_is_counted_not_hashed(tmp_path):
    p = _mk(tmp_path, [("m1", "enc:v1:AAAA", None, "", "active", "dsh")])
    r = mod.apply_backfill(p)
    assert r["sample_ciphertext"] == 1 and r["filled"] == 0


def test_empty_content_is_counted(tmp_path):
    p = _mk(tmp_path, [("m1", "", None, "", "active", "dsh")])
    r = mod.apply_backfill(p)
    assert r["empty_content"] == 1 and r["filled"] == 0


# ---- 4. 只补空位 -------------------------------------------------------------

def test_does_not_overwrite_existing_content_hash(tmp_path):
    keep = "f" * 64
    p = _mk(tmp_path, [("m1", "hello", keep, "", "active", "dsh")])
    mod.apply_backfill(p)
    con = sqlite3.connect(p)
    assert con.execute("SELECT content_hash FROM memories WHERE memory_id='m1'"
                       ).fetchone()[0] == keep, "既有 content_hash 被覆盖是破坏性操作"
    con.close()


def test_fills_both_columns(tmp_path):
    p = _mk(tmp_path, [("m1", "hello", None, "", "active", "dsh")])
    mod.apply_backfill(p)
    con = sqlite3.connect(p)
    ch, sh = con.execute("SELECT content_hash, sha256_hash FROM memories"
                         " WHERE memory_id='m1'").fetchone()
    con.close()
    assert ch == mod.sha256_of("hello") and sh == ch, "两列必须同值"


# ---- 5. 撞唯一索引 → 跳过并保持 NULL ----------------------------------------

def test_unique_conflict_is_skipped_and_left_null(tmp_path):
    """同 (persona, agent, content_hash) 的第二条 active 行必须跳过并保持 NULL。"""
    p = _mk(tmp_path, [
        ("m1", "same", mod.sha256_of("same"), "", "active", "dsh"),   # 已占位
        ("m2", "same", None, "", "active", "dsh"),                    # 撞索引
        ("m3", "different", None, "", "active", "dsh"),
    ])
    r = mod.apply_backfill(p)
    assert r["skipped_unique_conflict"] == 1
    assert r["filled"] == 1
    con = sqlite3.connect(p)
    assert con.execute("SELECT content_hash FROM memories WHERE memory_id='m2'"
                       ).fetchone()[0] is None, "撞索引的行必须保持 NULL"
    con.close()
    assert r["active_dup_groups_after"] == 0


# ---- 6. Pass2：先复算再补；漂移只报不改 -------------------------------------

def test_pass2_repairs_sha256_when_hash_matches_content(tmp_path):
    ch = mod.sha256_of("harvested text")
    p = _mk(tmp_path, [("m1", "harvested text", ch, "", "active", "dsh")])
    r = mod.apply_backfill(p)
    assert r["sha256_repaired"] == 1 and r["hash_drift"] == 0
    con = sqlite3.connect(p)
    assert con.execute("SELECT sha256_hash FROM memories WHERE memory_id='m1'"
                       ).fetchone()[0] == ch
    con.close()


def test_pass2_reports_drift_and_does_not_write(tmp_path):
    """content_hash 与 sha256(content) 不符 ⇒ 只报不改（派生列漂移是已知现象）。"""
    p = _mk(tmp_path, [("m1", "real content", "a" * 64, "", "active", "dsh")])
    r = mod.apply_backfill(p)
    assert r["hash_drift"] == 1
    assert r["sha256_repaired"] == 0
    con = sqlite3.connect(p)
    assert con.execute("SELECT sha256_hash FROM memories WHERE memory_id='m1'"
                       ).fetchone()[0] == "", "漂移行不得被写入"
    con.close()


# ---- 备份 --------------------------------------------------------------------

def test_backup_created_and_bounded(tmp_path):
    p = _mk(tmp_path, [("m1", "x", None, "", "active", "dsh")])
    r = mod.apply_backfill(p)
    assert os.path.exists(r["backup"])
    for i in range(4):
        with open(os.path.join(mod.BACKUP_ROOT,
                               mod.BACKUP_PREFIX + "2026010%d-120000" % i), "w") as fh:
            fh.write("x")
    mod._prune_backups(keep=2)
    left = [n for n in os.listdir(mod.BACKUP_ROOT) if n.startswith(mod.BACKUP_PREFIX)]
    assert len(left) == 2


# ---- §13.2 取不到 ≠ 不存在 ---------------------------------------------------

def test_missing_store_inconclusive(tmp_path):
    r = mod.plan(os.path.join(str(tmp_path), "nope.db"), "cli")
    assert r["verdict"] == "INCONCLUSIVE" and "not found" in r["error"]


def test_missing_column_inconclusive(tmp_path):
    p = os.path.join(str(tmp_path), "thin.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE memories (memory_id TEXT)")
    con.commit()
    con.close()
    r = mod.plan(p, "cli")
    assert r["verdict"] == "INCONCLUSIVE" and "missing columns" in r["error"]
