"""G13/t156 判据 —— **镜像不再把密文写进 `tokenized_content`**（只读驱动真适配器 + 临时库）。

背景（t154-P0-1 的现场）：
  `scripts/backfill_sqlite_from_pg.py::_insert` 原本把 `rec["content"]` 当**明文**推进分词，
  而 PG 侧 `perception` 的 `content` **已经是密文**（`TRINITY_PG_ENCRYPT_CATEGORIES=perception`）
  ⇒ SQLite 的 `tokenized_content` 里装的是**密文**（实测 2,623 行），该行在 FTS 里也按密文分词。

本判据五条（含**反向**与**牙齿**）：
  T1 正向：密文 `content` ⇒ `tokenized_content` **不含 `enc:v1:` 前缀**、且等于"明文分词"的结果；
  T2 反向：明文 `content` ⇒ `tokenized_content` 仍是**正确的明文分词**（正常路径没被改坏）；
  T3 牙齿：`TRINITY_MIRROR_TOKENIZE_PLAIN=0` ⇒ `tokenized_content` **又变回密文**（修法承重）；
  T4 健壮：`decrypt_content` 是 **fail-open** 的 ⇒ 损坏密文必须**留空**、**不得**回退成密文；
  T5 形态：改前/改后 `tok_kind = cipher/plaintext/empty` 计数（临时库口径，打印出来给报告用）。

⛔ 不动：`content` 列存什么 · `content_hash`（`sha`）口径 —— 属**用户决定**（t154 §①②）。
本判据用**真** `SQLiteAdapter`（真加密 + 真分词）与**临时库**；⛔ 不碰生产库、不连 PG。
⚠️ 仪器教训（本轮自伤）：首版把 `TemporaryDirectory` 与**未关闭的 sqlite 连接**一起用 ⇒ **Windows 上
   清理时报 `PermissionError [WinError 32]`**，**5 条全红**——而那**全是 teardown 假红**，
   **不能当反向基线**。现在改为：显式 `_finish()` 先关连接、再尽力清理目录。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "backfill_sqlite_from_pg.py")
sys.path.insert(0, REPO)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

from trinity.adapters.sqlite import SQLiteAdapter  # noqa: E402
from trinity.security.crypto import encrypt_content  # noqa: E402
# L1 静默失败治理（t162/G19）：**吞但计数**（与 docs/SILENT_FAILURE_BUDGETS.json 的 `_policy` 一致）
try:
    from trinity._swallow import swallow  # noqa: E402
except Exception:                          # 极早期/无 trinity 时退化为空操作
    def swallow(*_a, **_k):                # type: ignore[misc]
        return None


def _load_mirror():
    spec = importlib.util.spec_from_file_location("_g13_mirror", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_g13_mirror"] = mod
    spec.loader.exec_module(mod)
    return mod


MIRROR = _load_mirror()

COLS = ["memory_id", "session_id", "persona_id", "tenant_id", "agent_id", "app_id",
        "content", "tokenized_content", "role", "importance", "tags", "category",
        "memory_layer", "sha256_hash", "status", "version", "ttl_seconds",
        "last_accessed_at", "access_count", "importance_score", "content_hash",
        "conflict_group_id", "is_resolved", "modality", "metadata", "source_uri",
        "created_at", "updated_at"]


def _mk():
    """真适配器 + 临时库（**不自动清理**，由 `_finish` 显式关连接后再删）。"""
    tmp = tempfile.mkdtemp(prefix="g13_case_")
    db = os.path.join(tmp, "t.db")
    ad = SQLiteAdapter(db)
    try:
        ad.connect()
    except Exception as _e:            # t162/G19：原为静默 `pass` ⇒ 改为"吞但计数"
        swallow(__name__ + ":connect", _e)
    with sqlite3.connect(db) as c:
        have = {r[1] for r in c.execute("PRAGMA table_info(memories)")}
        if "memory_id" not in have:
            ddl = ", ".join("%s %s" % (k, "INTEGER" if k in ("version", "access_count",
                                                             "is_resolved") else "TEXT")
                            for k in COLS)
            c.execute("CREATE TABLE memories (%s, PRIMARY KEY (memory_id))" % ddl)
    return ad, db, tmp


def _finish(ad, tmp):
    for name in ("close", "disconnect"):
        fn = getattr(ad, name, None)
        if callable(fn):
            try:
                fn()
            except Exception as _e:    # t162/G19：原为静默 `pass` ⇒ 改为"吞但计数"
                swallow(__name__ + ":finish_" + str(name), _e)
    shutil.rmtree(tmp, ignore_errors=True)   #: ⚠️ 必须先关连接（Windows 上句柄未释放会 WinError 32）


def _rec(mid: str, content: str) -> dict:
    return {"memory_id": mid, "content": content, "persona_id": "p", "agent_id": "a",
            "session_id": "s", "category": "perception", "tags": ["t"], "importance": 0.5}


def _tok(db: str, mid: str):
    with sqlite3.connect(db) as c:
        r = c.execute("SELECT tokenized_content, content, content_hash FROM memories "
                      "WHERE memory_id=?", (mid,)).fetchone()
    return r or (None, None, None)


def _kind(tok) -> str:
    if not tok:
        return "empty"
    return "cipher" if tok.startswith("enc:v1:") else "plaintext"


def test_T1_ciphertext_content_is_tokenized_as_plaintext(monkeypatch):
    monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        plain = "hello 明文 world"
        ct = encrypt_content(plain)
        MIRROR._insert(ad, _rec("m1", ct), db)
        tok, content_col, chash = _tok(db, "m1")
        assert tok is not None, "没插进去"
        assert not tok.startswith("enc:v1:"), "tokenized 里还是密文：%r" % str(tok)[:40]
        assert tok, "tokenized 为空（应当有明文分词）"
        assert tok == ad._tokenized_for_storage(plain, ad._tokenize_content_for_fts(plain)), \
            "分词结果 != 常规路径对同一明文的做法"
        #: ⛔ 不在本任务范围：content 列未被改动
        assert content_col == ct, "content 列被改了（本任务不该动它）"
        #: ⚠️ G29/t172（2026-10-08 队长拍板）**取代**了本条原先的"content_hash 算密文"口径：
        #: 契约 `docs/STORAGE_ENCRYPTION_20260815.md:35` 要求两个 hash **基于明文** ⇒ 这里改为断言**新口径**。
        assert chash == MIRROR._sha(plain), "content_hash 应为明文口径（t172）：%r" % chash
    finally:
        _finish(ad, tmp)


def test_T2_plaintext_content_still_tokenized_correctly(monkeypatch):
    monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        plain = "普通明文记忆 abc123"
        MIRROR._insert(ad, _rec("m2", plain), db)
        tok, _, _ = _tok(db, "m2")
        assert tok == ad._tokenized_for_storage(plain, ad._tokenize_content_for_fts(plain))
        assert not tok.startswith("enc:v1:")
    finally:
        _finish(ad, tmp)


def test_T3_teeth_switch_off_reproduces_ciphertext(monkeypatch):
    """⭐ 牙齿：把"已检测密文 ⇒ 解密再分词"那一步关掉 ⇒ tokenized **又变回密文**。"""
    monkeypatch.setenv("TRINITY_MIRROR_TOKENIZE_PLAIN", "0")
    ad, db, tmp = _mk()
    try:
        ct = encrypt_content("hello 明文 world")
        MIRROR._insert(ad, _rec("m3", ct), db)
        tok, _, _ = _tok(db, "m3")
        assert tok.startswith("enc:v1:"), "牙齿失效：关掉开关后 tokenized=%r" % str(tok)[:40]
    finally:
        _finish(ad, tmp)


def test_T4_fail_open_decrypt_must_not_fall_back_to_ciphertext(monkeypatch):
    """⭐ 健壮性：`decrypt_content` **fail-open**（损坏密文原样返回）⇒ 必须留空，不得回退成密文。"""
    monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
    ad, db, tmp = _mk()
    try:
        MIRROR._insert(ad, _rec("m4", "enc:v1:not-base64-@@@"), db)
        tok, _, _ = _tok(db, "m4")
        assert tok in (None, ""), "损坏密文必须留空，实测 %r" % str(tok)[:40]
    finally:
        _finish(ad, tmp)


def test_T5_morphology_counts_before_and_after(monkeypatch):
    """⭐ 形态对照（临时库口径）：改后 cipher=0；关掉开关（≈改前）cipher≥2。"""
    rows = {"c1": encrypt_content("甲 密文一"), "c2": encrypt_content("乙 密文二"),
            "p1": "丙 明文三", "b1": "enc:v1:broken@@"}
    counts = {}
    for label, switch in (("after", None), ("before", "0")):
        if switch is None:
            monkeypatch.delenv("TRINITY_MIRROR_TOKENIZE_PLAIN", raising=False)
        else:
            monkeypatch.setenv("TRINITY_MIRROR_TOKENIZE_PLAIN", switch)
        ad, db, tmp = _mk()
        try:
            for mid, content in rows.items():
                MIRROR._insert(ad, _rec("%s_%s" % (label, mid), content), db)
            kinds = {"cipher": 0, "plaintext": 0, "empty": 0}
            for mid in rows:
                tok, _, _ = _tok(db, "%s_%s" % (label, mid))
                kinds[_kind(tok)] += 1
            counts[label] = kinds
        finally:
            _finish(ad, tmp)
    print("\nG13 形态对照（临时库，4 条 = 2 密文 + 1 明文 + 1 损坏密文）：")
    for k, v in counts.items():
        print("   %-8s %s" % (k, v))
    assert counts["after"]["cipher"] == 0, "改后仍有密文入 tokenized：%s" % counts["after"]
    assert counts["after"]["plaintext"] >= 2 and counts["after"]["empty"] == 1, counts["after"]
    assert counts["before"]["cipher"] >= 2, "开关关掉后应重现密文：%s" % counts["before"]
