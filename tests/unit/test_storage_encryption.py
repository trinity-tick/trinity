"""Trinity — 存储加密单元测试（B5, 2026-08-15）。

覆盖：
- StorageCipher AES-256-GCM 加解密往返与格式
- 加密开关与密钥管理（env 开关、key 持久化）
- SQLiteAdapter 集成：密文落盘、API 解密、FTS 检索、版本链、更新路径
- 明文对照组：不加密时行为不变
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

from trinity.adapters.sqlite import SQLiteAdapter
from trinity.security.crypto import StorageCipher, get_storage_cipher, is_enabled

_SECRET = "a" * 64  # 32 字节 hex


def test_cipher_roundtrip() -> None:
    c = StorageCipher(bytes.fromhex(_SECRET))
    enc = c.encrypt("机密内容 13800138000")
    assert enc.startswith("enc:v1:")
    assert "机密内容" not in enc
    assert c.decrypt(enc) == "机密内容 13800138000"
    assert c.is_encrypted(enc)


def test_cipher_unique_nonce() -> None:
    c = StorageCipher(bytes.fromhex(_SECRET))
    e1, e2 = c.encrypt("same"), c.encrypt("same")
    assert e1 != e2  # 随机 nonce → 密文不同


def test_cipher_wrong_key_fails() -> None:
    c = StorageCipher(bytes.fromhex(_SECRET))
    enc = c.encrypt("秘密")
    bad = StorageCipher(bytes.fromhex("b" * 64))
    with pytest.raises(Exception):
        bad.decrypt(enc)


def test_cipher_passthrough_plaintext() -> None:
    """未加密历史数据（无前缀）原样返回。"""
    c = StorageCipher(bytes.fromhex(_SECRET))
    assert c.decrypt("plain legacy text") == "plain legacy text"
    assert c.is_encrypted("plain") is False


def test_cipher_bad_key_len() -> None:
    with pytest.raises(ValueError):
        StorageCipher(b"short")


def test_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """2026-08-24（R8 P1-5）：默认 on（安全默认），off 显式关闭。"""
    monkeypatch.delenv("TRINITY_STORAGE_ENCRYPTION", raising=False)
    monkeypatch.delenv("TRINITY_STORAGE_KEY", raising=False)
    assert is_enabled() is True   # 默认开启
    monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "off")
    assert is_enabled() is False
    monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "on")
    assert is_enabled() is True


def test_cipher_from_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRINITY_STORAGE_KEY", _SECRET)
    monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "on")
    c = get_storage_cipher()
    assert c is not None
    assert c.decrypt(c.encrypt("x")) == "x"


# ── SQLiteAdapter 集成 ──────────────────────────────────────────────

_CONTENT = "Trinity 存储加密演示：这是机密记忆，包含电话号码 13800138000。"


def _make_adapter(tmp_path: Path, encrypted: bool,
                  monkeypatch: pytest.MonkeyPatch) -> SQLiteAdapter:
    if encrypted:
        monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "on")
        monkeypatch.setenv("TRINITY_STORAGE_KEY", _SECRET)
    else:
        # 2026-08-24（R8 P1-5）：默认 on，明文对照组须显式 off
        monkeypatch.setenv("TRINITY_STORAGE_ENCRYPTION", "off")
        monkeypatch.delenv("TRINITY_STORAGE_KEY", raising=False)
    db = str(tmp_path / "test.db")
    a = SQLiteAdapter(db)
    a.connect()
    return a


@pytest.mark.parametrize("encrypted", [False, True])
def test_store_read_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                              encrypted: bool) -> None:
    a = _make_adapter(tmp_path, encrypted, monkeypatch)
    r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a", tags=["机密"])
    got = a.get_memory(r["memory_id"])
    assert got["content"] == _CONTENT
    a.disconnect()


def test_encrypted_content_ciphertext_on_disk(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    a = _make_adapter(tmp_path, True, monkeypatch)
    r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a")
    a.disconnect()
    raw = sqlite3.connect(str(tmp_path / "test.db"))
    raw.row_factory = sqlite3.Row
    row = raw.execute(
        "SELECT content, tokenized_content FROM memories WHERE memory_id = ?",
        (r["memory_id"],)
    ).fetchone()
    raw.close()
    assert row["content"].startswith("enc:v1:")
    assert "机密记忆" not in row["content"]
    # tokenized 为明文（jieba 分词），FTS 可用
    assert "机密" in row["tokenized_content"]


def test_plaintext_content_on_disk(tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    a = _make_adapter(tmp_path, False, monkeypatch)
    r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a")
    a.disconnect()
    raw = sqlite3.connect(str(tmp_path / "test.db"))
    row = raw.execute("SELECT content FROM memories WHERE memory_id = ?",
                      (r["memory_id"],)).fetchone()
    raw.close()
    assert row[0] == _CONTENT


@pytest.mark.parametrize("encrypted", [False, True])
def test_fts_search_cjk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        encrypted: bool) -> None:
    a = _make_adapter(tmp_path, encrypted, monkeypatch)
    r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a")
    hits = a.search_memories("机密记忆", persona_id="p", top_k=5)
    assert any(h["memory_id"] == r["memory_id"] for h in hits)
    a.disconnect()


@pytest.mark.parametrize("encrypted", [False, True])
def test_fts_search_english(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                            encrypted: bool) -> None:
    a = _make_adapter(tmp_path, encrypted, monkeypatch)
    r = a.store_memory(content="second memory for english fts query test",
                       persona_id="p", agent_id="a")
    hits = a.search_memories("english", persona_id="p", top_k=5)
    assert any(h["memory_id"] == r["memory_id"] for h in hits)
    a.disconnect()


@pytest.mark.parametrize("encrypted", [False, True])
def test_version_chain_decrypt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                               encrypted: bool) -> None:
    a = _make_adapter(tmp_path, encrypted, monkeypatch)
    r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a")
    a.update_memory(r["memory_id"], content="更新后的机密记忆内容")
    chain = a.get_version_chain(r["memory_id"])
    assert len(chain) == 2
    assert chain[0]["content"] == _CONTENT
    assert chain[1]["content"] == "更新后的机密记忆内容"
    a.disconnect()


@pytest.mark.parametrize("encrypted", [False, True])
def test_update_and_persona_memories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     encrypted: bool) -> None:
    a = _make_adapter(tmp_path, encrypted, monkeypatch)
    r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a")
    upd = a.update_memory(r["memory_id"], content="更新后的机密记忆内容")
    assert upd["content"] == "更新后的机密记忆内容"
    pm = a.get_persona_memories("p", limit=10)
    assert all(not m["content"].startswith("enc:v1:") for m in pm)
    a.disconnect()


def test_external_content_fts_migration(tmp_path: Path) -> None:
    """旧库 external content FTS 表必须迁移为独立表并回填。"""
    db = str(tmp_path / "legacy.db")
    # 1) 先建正常 schema
    a = SQLiteAdapter(db)
    a.connect()
    r = a.store_memory(content="legacy 机密记忆 content", persona_id="p", agent_id="a")
    a.disconnect()
    # 2) 手工降级为 external content FTS 表（模拟旧库结构）
    c = sqlite3.connect(db)
    c.executescript("""
        DROP TABLE memories_fts;
        DROP TRIGGER IF EXISTS memories_ai;
        DROP TRIGGER IF EXISTS memories_ad;
        DROP TRIGGER IF EXISTS memories_au;
        CREATE VIRTUAL TABLE memories_fts USING fts5(
            content, category, tags, content='memories', content_rowid='rowid');
        INSERT INTO memories_fts(rowid, content, category, tags)
        SELECT rowid, content, category, tags FROM memories;
    """)
    c.commit()
    c.close()
    # 3) 重新 connect → 应检测并迁移为独立表
    a = SQLiteAdapter(db)
    a.connect()
    a.disconnect()
    c = sqlite3.connect(db)
    sql = c.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories_fts'"
    ).fetchone()[0]
    assert "content='memories'" not in sql
    assert c.execute("SELECT COUNT(*) FROM memories_fts").fetchone()[0] >= 1
    # 4) 检索仍可用
    a = SQLiteAdapter(db)
    a.connect()
    hits = a.search_memories("机密记忆", top_k=5)
    assert any(h["memory_id"] == r["memory_id"] for h in hits)
    a.disconnect()
    c.close()
