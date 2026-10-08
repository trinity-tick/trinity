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
#
# ⚠️ **2026-10-06（G2/t43 行为变更）**：写入路径现在**按设计掩码 PII**
#    （`trinity/adapters/_pii_guard.py` 在适配器写入边界上掩码）。因此：
#      · "存储加密往返"这条不变量**只在内容不被策略层改写时成立** ⇒ 往返样本改用**无 PII** 文本；
#      · **旧期望（原文往返）没有被删掉**，它的"新形态"由 `test_pii_sample_masked_by_write_path` 断言
#        （同一段 PII 样本 ⇒ 落库/读回是**掩码文本**）。
#    t56 之前的失败形态（实测，确定性）：
#        assert 'Trinity 存储加密演示：这是机密记忆，包含电话号码 138********。' \
#            == 'Trinity 存储加密演示：这是机密记忆，包含电话号码 13800138000。'
_CONTENT = "Trinity 存储加密演示：这是机密记忆，包含电话号码 13800138000。"
#: 往返专用样本：**不含 PII** ⇒ 策略层不改写它 ⇒ 加密往返不变量成立
_CONTENT_CLEAN = "Trinity 存储加密演示：这是机密记忆，不含电话号码等标识符。"
#: `_CONTENT` 经写入路径后的**实测**掩码形态（t56 修后实测；用它断言"行为被钉住"）
_CONTENT_MASKED = "Trinity 存储加密演示：这是机密记忆，包含电话号码 138********。"


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
    # 2026-10-06：往返样本改为无 PII 文本（旧期望 `== _CONTENT` 现在会被写入路径掩码掉；
    #   旧期望的"新形态"见 test_pii_sample_masked_by_write_path）
    r = a.store_memory(content=_CONTENT_CLEAN, persona_id="p", agent_id="a", tags=["机密"])
    got = a.get_memory(r["memory_id"])
    assert got["content"] == _CONTENT_CLEAN
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
    # 2026-10-06：同上 —— "明文落盘 == 输入"只对**无 PII** 文本成立（PII 会被写入边界掩码）
    r = a.store_memory(content=_CONTENT_CLEAN, persona_id="p", agent_id="a")
    a.disconnect()
    raw = sqlite3.connect(str(tmp_path / "test.db"))
    row = raw.execute("SELECT content FROM memories WHERE memory_id = ?",
                      (r["memory_id"],)).fetchone()
    raw.close()
    assert row[0] == _CONTENT_CLEAN


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
    # 2026-10-06：版本链的"逐字往返"同样只在无 PII 样本上成立（旧期望见上方 ⚠️ 注释）
    r = a.store_memory(content=_CONTENT_CLEAN, persona_id="p", agent_id="a")
    a.update_memory(r["memory_id"], content="更新后的机密记忆内容")
    chain = a.get_version_chain(r["memory_id"])
    assert len(chain) == 2
    assert chain[0]["content"] == _CONTENT_CLEAN
    assert chain[1]["content"] == "更新后的机密记忆内容"
    a.disconnect()


def test_pii_sample_masked_by_write_path(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """**2026-10-06 新增判据**：同一段含 PII 的样本经写入路径 ⇒ 落库/读回是**掩码文本**。

    这条把"**旧期望为什么会失效**"从"绕开"变成"**钉住新行为**"：
      · `_CONTENT`（含 13800138000）⇒ 读回 == `_CONTENT_MASKED`，且**明文手机号不在**；
      · 两种加密模式都要成立（掩码发生在**加密之前**的写入边界，与是否加密无关）；
      · 旧期望（原文往返）的原文保留在文件顶部 ⚠️ 注释与本函数的 `old_expectation` 里。
    """
    old_expectation = _CONTENT          # 旧行为期望的落库文本（现已不再成立）
    for encrypted in (False, True):
        sub = tmp_path / ("enc" if encrypted else "plain")
        sub.mkdir()
        a = _make_adapter(sub, encrypted, monkeypatch)
        try:
            r = a.store_memory(content=_CONTENT, persona_id="p", agent_id="a")
            got = a.get_memory(r["memory_id"])["content"]
        finally:
            a.disconnect()
        assert got == _CONTENT_MASKED, (
            "写入路径未按设计掩码 PII（encrypted=%s）：got=%r" % (encrypted, got))
        assert "13800138000" not in got, "明文手机号仍在（encrypted=%s）：%r" % (encrypted, got)
        assert "138********" in got, (encrypted, got)
        assert got != old_expectation, (
            "旧期望（原文落库）竟然仍成立 ⇒ 脱敏行为没生效" )


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
