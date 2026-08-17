"""Trinity — 合规认证包单元测试（2026-08-15, V2 动作 B）。

覆盖 scripts/compliance_check.py：
- check_encryption：开关 + 密文样本判定
- check_audit_chain：审计条数 + checksum 链
- check_gdpr_tools：工具存在性
- check_rbac：无身份请求 401/403=生效、200=未强制
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from scripts.compliance_check import (
    check_audit_chain, check_encryption, check_gdpr_tools, check_rbac,
)


@pytest.fixture()
def db(tmp_path: Path) -> str:
    d = str(tmp_path / "t.db")
    c = sqlite3.connect(d)
    c.executescript("""
        CREATE TABLE memories (memory_id TEXT PRIMARY KEY, content TEXT, status TEXT);
        CREATE TABLE audit_log (
            id TEXT PRIMARY KEY, memory_id TEXT, action TEXT, agent_id TEXT,
            persona_id TEXT, timestamp TEXT, details TEXT, checksum TEXT
        );
    """)
    c.execute("INSERT INTO memories VALUES ('m1', 'plain content', 'active')")
    c.execute("""INSERT INTO audit_log (id, action, checksum) VALUES
                 ('a1', 'STORE', 'abc'), ('a2', 'SEARCH', 'def')""")
    c.commit()
    c.close()
    return d


def test_encryption_off_reports_fail(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRINITY_STORAGE_ENCRYPTION", raising=False)
    r = check_encryption(db)
    assert r["ok"] is False
    assert r["detail"]["switch_on"] is False


def test_encryption_on_without_cipher(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "on")
    r = check_encryption(db)  # 无密文样本 → fail（如实报告）
    assert r["ok"] is False
    assert r["detail"]["switch_on"] is True
    assert r["detail"]["cipher_content_found"] is False


def test_encryption_on_with_cipher(db: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "on")
    c = sqlite3.connect(db)
    c.execute("INSERT INTO memories VALUES ('m2', 'enc:v1:cipherdata', 'active')")
    c.commit()
    c.close()
    r = check_encryption(db)
    assert r["ok"] is True


def test_audit_chain_pass(db: str) -> None:
    r = check_audit_chain(db)
    assert r["ok"] is True
    assert r["detail"]["entries"] == 2
    assert r["detail"]["with_checksum"] == 2


def test_audit_chain_empty(tmp_path: Path) -> None:
    d = str(tmp_path / "e.db")
    c = sqlite3.connect(d)
    c.executescript("CREATE TABLE audit_log (id TEXT, checksum TEXT)")
    c.close()
    r = check_audit_chain(d)
    assert r["ok"] is False


def test_gdpr_tools_present() -> None:
    r = check_gdpr_tools()
    # 脚本存在（项目内）→ PASS
    assert r["ok"] is True


def test_rbac_unauthorized_means_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """无身份请求返回 401 → RBAC 生效 → PASS。"""
    class FakeHTTPError(Exception):
        def __init__(self):
            self.code = 401

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=5: (_ for _ in ()).throw(FakeHTTPError()))
    r = check_rbac(":memory:", "http://x")
    assert r["ok"] is True


def test_rbac_200_means_not_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    """无身份请求返回 200 → 未强制 → FAIL（如实报告）。"""
    import scripts.compliance_check as cc

    class FakeResp:
        status = 200

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=5: FakeResp())
    r = check_rbac(":memory:", "http://x")
    assert r["ok"] is False
