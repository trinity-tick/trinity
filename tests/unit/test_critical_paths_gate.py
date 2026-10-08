#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""critical_paths_gate 的判据测试（2026-10-02，事故后建立）。

**为什么必须配套测试**：本仓纪律「判据必须先证明自己有判别力」。
本次事故的成因恰恰是**没有判据**；所以新判据必须带**反事实**：
同一个判定函数，换输入必须换结论，否则它是恒真装饰。

覆盖：
  1. 坏库（头全 0 / quick_check 非 ok）⇒ 硬违规
  2. 好库 ⇒ 无硬违规
  3. store 目录不存在 ⇒ 硬违规
  4. 库文件缺失 ⇒ 硬违规
  5. `_sibling_warning`：指向未标注恢复的老名字 + 同层有 `*-restored` ⇒ 出警告（事故同形）
  6. 反事实：指向 `*-restored` 时**不得**再出该警告（否则判据恒真）
  7. 密钥文件缺失/为空 ⇒ 硬违规；`TRINITY_SQLCIPHER=off` ⇒ 只报软信息
  8. 未设 `TRINITY_STORE` ⇒ 缺省 fail-closed；`--ci-safe` ⇒ 软信息（显式跳过，不是静默通过）
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from scripts import critical_paths_gate as g  # noqa: E402


def _make_store(tmp_path, name="store", *, header=b"SQLite format 3\x00", rows=True):
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    db = d / g.DB_NAME
    if header is None:
        return d, None
    if rows:
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE memories (id TEXT)")
        con.execute("INSERT INTO memories VALUES ('a')")
        con.commit()
        con.close()
    else:
        db.write_bytes(header + b"\x00" * 4096)
    return d, db


# ── 1/2 库质量 ────────────────────────────────────────────────────────────
def test_good_store_has_no_hard_violation(tmp_path):
    d, _ = _make_store(tmp_path)
    r = g._probe_store(str(d))
    assert r["reasons"] == [], r
    assert r["detail"]["quick_check"] == "ok"


def test_header_right_but_payload_fake_is_hard_violation(tmp_path):
    """头对、内容假的库：打开即 `file is not a database` —— 也算硬违规（fail-closed）。"""
    d, _ = _make_store(tmp_path, rows=False)
    r = g._probe_store(str(d))
    assert "STORE_DB_OPEN_FAILED" in r["reasons"], r
    assert r["detail"]["open_error"].startswith("DatabaseError"), r


def test_really_corrupt_sqlite_reports_quick_check(tmp_path):
    """**真** SQLite 但页损坏：能打开，quick_check 非 ok ⇒ 硬违规（本次事故的形状）。"""
    d = tmp_path / "store"
    d.mkdir()
    db = d / g.DB_NAME
    con = sqlite3.connect(str(db))
    con.execute("CREATE TABLE memories (id TEXT, blob BLOB)")
    con.executemany("INSERT INTO memories VALUES (?, randomblob(800))",
                    [("m%d" % i,) for i in range(400)])
    con.commit()
    con.close()
    raw = bytearray(db.read_bytes())
    # 打坏中段若干页（保留头 100 字节，确保仍是识别的 SQLite 文件）
    for off in range(8192, min(len(raw), 40960), 4096):
        raw[off:off + 64] = b"\xde\xad\xbe\xef" * 16
    db.write_bytes(bytes(raw))

    r = g._probe_store(str(d))
    assert r["reasons"], "损坏的库必须被判红，实际 reasons 为空（判据无判别力）"
    assert ("STORE_DB_QUICK_CHECK_NOT_OK" in r["reasons"]
            or "STORE_DB_OPEN_FAILED" in r["reasons"]), r


def test_all_zero_header_is_named(tmp_path):
    d = tmp_path / "store"
    d.mkdir()
    (d / g.DB_NAME).write_bytes(b"\x00" * 4096)
    r = g._probe_store(str(d))
    assert "STORE_DB_HEADER_ALL_ZERO" in r["reasons"], r


def test_missing_dir_and_missing_db(tmp_path):
    r = g._probe_store(str(tmp_path / "nope"))
    assert r["reasons"] == ["STORE_DIR_MISSING"]
    d = tmp_path / "empty_store"
    d.mkdir()
    r2 = g._probe_store(str(d))
    assert r2["reasons"] == ["STORE_DB_MISSING"]


# ── 5/6 sibling 软判据 + 反事实 ───────────────────────────────────────────
def test_sibling_warning_fires_on_pre_restore_name(tmp_path):
    _make_store(tmp_path, "store-restored")
    old = tmp_path / "store"
    old.mkdir()
    w = g._sibling_warning(str(old))
    assert any("SIBLING_RECOVERY_DIR_EXISTS" in x for x in w), w


def test_sibling_warning_silent_when_pointing_at_restored(tmp_path):
    """反事实：指向 *-restored 时必须**不**再报（否则判据恒真）。"""
    _make_store(tmp_path, "store-restored")
    (tmp_path / "store").mkdir()
    w = g._sibling_warning(str(tmp_path / "store-restored"))
    assert w == [], w


def test_sibling_warning_silent_without_siblings(tmp_path):
    d, _ = _make_store(tmp_path)
    assert g._sibling_warning(str(d)) == []


# ── 7 密钥文件 ────────────────────────────────────────────────────────────
def test_key_file_missing_is_hard(tmp_path, monkeypatch):
    d, _ = _make_store(tmp_path)
    monkeypatch.setenv("TRINITY_STORE", str(d))
    monkeypatch.setenv("TRINITY_SQLCIPHER_KEY_FILE", str(tmp_path / "nokey"))
    monkeypatch.delenv("TRINITY_SQLCIPHER", raising=False)
    out = g.scan()
    assert any("KEY_FILE_MISSING" in h for h in out["hard"]), out["hard"]


def test_key_file_empty_is_hard(tmp_path, monkeypatch):
    d, _ = _make_store(tmp_path)
    kf = tmp_path / "sqlcipher.key"
    kf.write_bytes(b"   \n")
    monkeypatch.setenv("TRINITY_STORE", str(d))
    monkeypatch.setenv("TRINITY_SQLCIPHER_KEY_FILE", str(kf))
    monkeypatch.delenv("TRINITY_SQLCIPHER", raising=False)
    out = g.scan()
    assert any("KEY_FILE_EMPTY" in h for h in out["hard"]), out["hard"]


def test_key_file_not_judged_when_cipher_off(tmp_path, monkeypatch):
    d, _ = _make_store(tmp_path)
    monkeypatch.setenv("TRINITY_STORE", str(d))
    monkeypatch.setenv("TRINITY_SQLCIPHER_KEY_FILE", str(tmp_path / "nokey"))
    monkeypatch.setenv("TRINITY_SQLCIPHER", "off")
    out = g.scan()
    assert not any("KEY_FILE" in h for h in out["hard"]), out["hard"]
    assert any("TRINITY_SQLCIPHER" in s for s in out["soft"]), out["soft"]


# ── 8 fail-closed 与 --ci-safe ────────────────────────────────────────────
def test_unset_store_is_fail_closed(monkeypatch):
    monkeypatch.delenv("TRINITY_STORE", raising=False)
    monkeypatch.delenv("TRINITY_CRITICAL_PATHS_ALLOW_MISSING", raising=False)
    out = g.scan(ci_safe=False)
    assert any("未设置" in h for h in out["hard"]), out["hard"]


def test_ci_safe_downgrades_explicitly(monkeypatch):
    monkeypatch.delenv("TRINITY_STORE", raising=False)
    monkeypatch.delenv("TRINITY_CRITICAL_PATHS_ALLOW_MISSING", raising=False)
    out = g.scan(ci_safe=True)
    assert out["hard"] == [], out["hard"]
    # 必须是**显式**跳过：要留下原因，不能静默变绿
    assert any("ci-safe" in s for s in out["soft"]), out["soft"]


def test_explicit_allow_missing_env(monkeypatch):
    monkeypatch.delenv("TRINITY_STORE", raising=False)
    monkeypatch.setenv("TRINITY_CRITICAL_PATHS_ALLOW_MISSING", "TRINITY_STORE")
    out = g.scan(ci_safe=False)
    assert out["hard"] == [], out["hard"]


# ── 退出码：报告模式也必须能失败 ─────────────────────────────────────────
def test_report_mode_returns_nonzero_on_hard(tmp_path, monkeypatch):
    d = tmp_path / "store"
    d.mkdir()
    (d / g.DB_NAME).write_bytes(b"\x00" * 4096)
    monkeypatch.setenv("TRINITY_STORE", str(d))
    assert g.main([]) == 1


def test_report_mode_returns_zero_on_good(tmp_path, monkeypatch):
    d, _ = _make_store(tmp_path)
    monkeypatch.setenv("TRINITY_STORE", str(d))
    monkeypatch.setenv("TRINITY_CRITICAL_PATHS_ALLOW_MISSING", "")
    assert g.main([]) == 0


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
