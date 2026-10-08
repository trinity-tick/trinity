"""G29/t172 判据 —— 镜像 `content_hash`/`sha256_hash` 的**明文口径**（含 fail-open 防护与"content 未动"）。

对象：`scripts/backfill_sqlite_from_pg.py::_insert`（只读驱动；**真** `SQLiteAdapter` + **临时库**）
契约：`docs/STORAGE_ENCRYPTION_20260815.md:35`「`sha256_hash / content_hash` | **基于明文计算**」；
      `:32` `memories.content` = **密文**、`:34` `tokenized_content` = **明文**。

五条：
  T1 正向：**密文 content** ⇒ `content_hash == sha256(明文)`（≠ 对密文取 sha）且 `tokenized` = 明文分词；
  T2 反向：**明文 content** ⇒ `content_hash` 与 `tokenized` 都与**常规路径**一致（没改坏正常路径）；
  T3 牙齿：**解不开的假密文** ⇒ **两个 hash 都必须是 NULL**（不得回退成"基于密文的 hash"），且留痕可观测；
  T4 断言 **content 列未动**（仍是传进来的那条密文串）；
  T5 回滚开关：`TRINITY_MIRROR_HASH_PLAIN=0` ⇒ 回到"对密文取 sha"（旧行为可复现 ⇒ 开关真在起作用）。

⚠️ 两条仪器教训（本会话踩过，这里预先规避）：① `with sqlite3.connect(...)` **只提交不关闭** ⇒ Windows
   `PermissionError [WinError 32]`；② 同一临时目录里**多个库要用不同文件名**（否则 `table already exists`）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "backfill_sqlite_from_pg.py")
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

from trinity.adapters.sqlite import SQLiteAdapter  # noqa: E402
from trinity.security.crypto import encrypt_content  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("_g29_mirror", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_g29_mirror"] = mod
    spec.loader.exec_module(mod)
    return mod


MIRROR = _load()

COLS = ["memory_id", "session_id", "persona_id", "tenant_id", "agent_id", "app_id", "content",
        "tokenized_content", "role", "importance", "tags", "category", "memory_layer",
        "sha256_hash", "status", "version", "ttl_seconds", "last_accessed_at", "access_count",
        "importance_score", "content_hash", "conflict_group_id", "is_resolved", "modality",
        "metadata", "source_uri", "created_at", "updated_at"]


def _sha(t: str) -> str:
    return hashlib.sha256(str(t).encode("utf-8")).hexdigest()


def _mk(name: str = "t1.db"):
    tmp = tempfile.mkdtemp(prefix="g29_case_")
    db = os.path.join(tmp, name)
    ad = SQLiteAdapter(db)
    try:
        ad.connect()
    except Exception as _e:      #: ⚠️ 不写静默 `pass`（结构门禁会点名）；留痕即可
        print("G29: adapter.connect() 跳过：%r" % (_e,))
    con = sqlite3.connect(db)
    try:
        have = {r[1] for r in con.execute("PRAGMA table_info(memories)")}
        if "memory_id" not in have:
            ddl = ", ".join("%s %s" % (k, "INTEGER" if k in ("version", "access_count",
                                                            "is_resolved") else "TEXT")
                            for k in COLS)
            con.execute("CREATE TABLE memories (%s, PRIMARY KEY (memory_id))" % ddl)
            con.commit()
    finally:
        con.close()
    return ad, db, tmp


def _finish(ad, tmp):
    for n in ("close", "disconnect"):
        fn = getattr(ad, n, None)
        if callable(fn):
            try:
                fn()
            except Exception as _e:   #: ⚠️ 留痕而非静默（关连接失败通常无妨，但必须可观测）
                print("G29: %s() 失败：%r" % (n, _e))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _rec(mid: str, content: str) -> dict:
    return {"memory_id": mid, "content": content, "persona_id": "p", "agent_id": "a",
            "session_id": "s", "category": "perception", "tags": ["t"], "importance": 0.5}


def _row(db: str, mid: str):
    con = sqlite3.connect(db)
    try:
        return con.execute("SELECT content, tokenized_content, sha256_hash, content_hash "
                           "FROM memories WHERE memory_id=?", (mid,)).fetchone()
    finally:
        con.close()


def test_T1_ciphertext_content_hash_is_plaintext_based(monkeypatch):
    monkeypatch.delenv("TRINITY_MIRROR_HASH_PLAIN", raising=False)
    monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        plain = "hello 明文 world"
        ct = encrypt_content(plain)
        MIRROR._insert(ad, _rec("m1", ct), db)
        content_col, tok, sh256, chash = _row(db, "m1")
        assert chash == _sha(plain), "content_hash 不是明文口径：%r" % chash
        assert chash != _sha(ct), "content_hash 仍然是'对密文取 sha'（旧口径）"
        assert sh256 == _sha(plain), "sha256_hash 也应基于明文（契约 :35 同行）"
        assert tok == ad._tokenized_for_storage(plain, ad._tokenize_content_for_fts(plain))
    finally:
        _finish(ad, tmp)


def test_T2_plaintext_content_matches_normal_path(monkeypatch):
    monkeypatch.delenv("TRINITY_MIRROR_HASH_PLAIN", raising=False)
    monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        plain = "普通明文记忆 abc123"
        MIRROR._insert(ad, _rec("m2", plain), db)
        _, tok, _, chash = _row(db, "m2")
        assert chash == _sha(plain)
        assert tok == ad._tokenized_for_storage(plain, ad._tokenize_content_for_fts(plain))
    finally:
        _finish(ad, tmp)


def test_T3_teeth_undecryptable_must_not_write_ciphertext_hash(monkeypatch, capsys):
    """⭐ 牙齿：`decrypt_content` fail-open（原样返回密文）必须被挡住 ⇒ **两个 hash 都是 NULL** + 留痕。"""
    monkeypatch.delenv("TRINITY_MIRROR_HASH_PLAIN", raising=False)
    monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        bad = "enc:v1:not-base64-@@@"
        MIRROR._insert(ad, _rec("m3", bad), db)
        _, tok, sh256, chash = _row(db, "m3")
        assert chash is None, "解不开却写了 hash（且它基于密文）：%r" % chash
        assert sh256 is None, "sha256_hash 也应为 NULL，实测 %r" % sh256
        assert chash != _sha(bad), "⛔ 把密文当明文取 sha 了"
        out = capsys.readouterr().out
        assert "MIRROR-PLAIN-CALIBER-FAIL" in out, "必须可观测（打印留痕）：%r" % out[-200:]
    finally:
        _finish(ad, tmp)


def test_T4_content_column_untouched(monkeypatch):
    """⭐ 断言"只改了 hash 口径"：`content` 列仍是传进来的那条**密文串**。"""
    monkeypatch.delenv("TRINITY_MIRROR_HASH_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        ct = encrypt_content("原样保留 content")
        MIRROR._insert(ad, _rec("m4", ct), db)
        content_col, _, _, _ = _row(db, "m4")
        assert content_col == ct, "content 列被改了（本任务不该动它）"
        assert content_col.startswith("enc:v1:")
    finally:
        _finish(ad, tmp)


def test_T5_rollback_switch_restores_old_caliber(monkeypatch):
    monkeypatch.setenv("TRINITY_MIRROR_HASH_PLAIN", "0")
    ad, db, tmp = _mk()
    try:
        plain = "回滚路径 hello"
        ct = encrypt_content(plain)
        MIRROR._insert(ad, _rec("m5", ct), db)
        _, _, _, chash = _row(db, "m5")
        assert chash == _sha(ct), "开关=0 应回到旧口径（对密文取 sha），实测 %r" % chash
    finally:
        _finish(ad, tmp)
