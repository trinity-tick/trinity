"""
聚合池 vs 引擎库一致性校验单测（scripts/consistency_check.py）
覆盖：active/inactive 构造；missing/extra/hash 漂移三种场景统计正确；
--fail-threshold 触发 exit 1；只读打开不存在文件报错友好；hash 抽样上限生效。
"""
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import consistency_check as cc


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_db(path, rows):
    """rows: list of dict with at least memory_id/content/status（可含内容哈希列）。"""
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS memories")
    conn.execute("""
        CREATE TABLE memories (
            memory_id TEXT PRIMARY KEY,
            content TEXT NOT NULL,
            status TEXT DEFAULT 'active',
            content_hash TEXT,
            sha256_hash TEXT
        )
    """)
    for r in rows:
        conn.execute(
            "INSERT INTO memories (memory_id, content, status, content_hash, sha256_hash) "
            "VALUES (?,?,?,?,?)",
            (r["memory_id"], r["content"], r.get("status", "active"),
             r.get("content_hash"), r.get("sha256_hash")),
        )
    conn.commit()
    conn.close()


def _make_pool(path, entries):
    """entries: list of dict with memory_id/content（可含 source_agents）。"""
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"version": "test", "memories": entries}, f, ensure_ascii=False)


def _default_rows():
    return [
        {
            "memory_id": "mem_a",
            "content": "alpha",
            "status": "active",
            "content_hash": _sha("alpha")[:20],
            "sha256_hash": _sha("alpha"),
        },
        {
            "memory_id": "mem_b",
            "content": "beta",
            "status": "active",
            "content_hash": _sha("beta")[:20],
            "sha256_hash": _sha("beta"),
        },
        # inactive：不应计入 total_active / missing
        {
            "memory_id": "mem_c",
            "content": "gamma",
            "status": "archived",
            "content_hash": _sha("gamma")[:20],
            "sha256_hash": _sha("gamma"),
        },
    ]


def _default_pool():
    return [
        {"memory_id": "h10", "content": "alpha", "source_agents": ["db-sync"]},
        {"memory_id": "h20", "content": "beta", "source_agents": ["db-sync", "session"]},
    ]


@pytest.fixture()
def basic_setup(tmp_path):
    db = str(tmp_path / "store.db")
    pool = str(tmp_path / "aggregator_pool.json")
    _make_db(db, _default_rows())
    _make_pool(pool, _default_pool())
    return db, pool


def test_basic_counts_and_inactive_exclusion(basic_setup):
    db, pool = basic_setup
    rep = cc.run_check(db, pool, hash_sample=200)
    assert rep["total_active"] == 2          # gamma archived 不计数
    assert rep["pool_entries"] == 2
    assert rep["missing_in_pool"] == 0
    assert rep["extra_in_pool"] == 0
    assert rep["hash_mismatch"] == 0


def test_missing_in_pool_when_active_absent(basic_setup, tmp_path):
    db, pool = basic_setup
    # 池里缺了 alpha → 库 active alpha 成 missing
    _make_pool(pool, [{"memory_id": "h20", "content": "beta", "source_agents": ["db-sync"]}])
    rep = cc.run_check(db, pool, hash_sample=200)
    assert rep["missing_in_pool"] == 1
    assert "alpha" in rep["samples"]


def test_extra_in_pool_when_pool_only(basic_setup, tmp_path):
    db, pool = basic_setup
    # 池多出 "zeta"（库任意状态都没这条）→ extra
    _make_pool(pool, _default_pool() + [{"memory_id": "h99", "content": "zeta", "source_agents": ["session"]}])
    rep = cc.run_check(db, pool, hash_sample=200)
    assert rep["extra_in_pool"] == 1
    assert "zeta" in rep["extra_samples"]
    # archived 的 gamma 在库中，池里出现不算 extra
    _make_pool(pool, _default_pool() + [{"memory_id": "h31", "content": "gamma", "source_agents": ["db-sync"]}])
    rep2 = cc.run_check(db, pool, hash_sample=200)
    assert rep2["extra_in_pool"] == 0


def test_hash_mismatch_detected(basic_setup, tmp_path):
    db, pool = basic_setup
    # 库 alpha 的 content_hash 被改错 → 同 content 双端哈希不一致
    bad_rows = _default_rows()
    bad_rows[0]["content_hash"] = "0" * 20          # 错误的 sha 前缀
    bad_rows[0]["sha256_hash"] = "0" * 64
    _make_db(db, bad_rows)
    rep = cc.run_check(db, pool, hash_sample=200)
    assert rep["hash_mismatch"] == 1
    assert rep["hash_checked"] == 2
    total_drift = rep["missing_in_pool"] + rep["extra_in_pool"] + rep["hash_mismatch"]
    assert rep["drift"] == total_drift


def test_fail_threshold_exit_code(basic_setup, tmp_path):
    db, pool = basic_setup
    # 池缺 alpha 且多 zeta → drift=2 > 默认阈值 1 → exit 1
    _make_pool(pool, [{"memory_id": "h99", "content": "zeta", "source_agents": ["session"]}])
    assert cc.main(["--sqlite-path", db, "--pool-path", pool, "--fail-threshold", "1"]) == 1
    # 同漂移但 fail-threshold 调大 → 不失败
    assert cc.main(["--sqlite-path", db, "--pool-path", pool, "--fail-threshold", "5"]) == 0
    # fail-threshold 0 = 从不失败
    assert cc.main(["--sqlite-path", db, "--pool-path", pool, "--fail-threshold", "0"]) == 0


def test_nonexistent_db_friendly_error(tmp_path):
    db = str(tmp_path / "no_such.db")
    pool = str(tmp_path / "pool.json")
    _make_pool(pool, _default_pool())
    code = cc.main(["--sqlite-path", db, "--pool-path", pool, "--json"])
    assert code == 2


def test_nonexistent_pool_friendly_error(tmp_path):
    db = str(tmp_path / "store.db")
    pool = str(tmp_path / "no_such_pool.json")
    _make_db(db, _default_rows())
    code = cc.main(["--sqlite-path", db, "--pool-path", pool, "--json"])
    assert code == 2


def test_readonly_does_not_write(basic_setup):
    # 打开后库文件 mtime 不应变化（只读模式），记录前后
    db, pool = basic_setup
    before = os.path.getmtime(db)
    rep = cc.run_check(db, pool, hash_sample=200)
    after = os.path.getmtime(db)
    assert before == after
    assert rep["total_active"] == 2


def test_hash_sample_limit(basic_setup, tmp_path):
    db, pool = basic_setup
    # 库 2 条 active，抽查上限 1 → hash_checked 至多 1
    rep = cc.run_check(db, pool, hash_sample=1)
    assert rep["hash_checked"] <= 1
    # 抽查上限 0 → 跳过哈希比对
    rep0 = cc.run_check(db, pool, hash_sample=0)
    assert rep0["hash_checked"] == 0
    assert rep0["hash_mismatch"] == 0


def test_json_flag_output(basic_setup, capsys):
    db, pool = basic_setup
    code = cc.main(["--sqlite-path", db, "--pool-path", pool, "--json"])
    assert code == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["total_active"] == 2
    assert "source_breakdown" in parsed
