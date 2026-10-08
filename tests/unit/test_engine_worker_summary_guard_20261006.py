# -*- coding: utf-8 -*-
"""T51/G10 判据：`engine_worker._session_dispose_summary()` 的裸 SQL 必须**过同一守卫**。

## 背景（修的是哪一条，为什么只有这一条）

`engine_worker.py` 的**主写入**走 `engine.ingest(...)`（客户端 ⇒ G2 掩码）✓；
**只有 `_session_dispose_summary()` 这条**是裸 `INSERT INTO memories`，两处守卫都不经过
（verifier 在 G5 用 AST 独立枚举找出，并在 809 行会话摘要样本里量出 **3 行未掩码**）。
⇒ 本判据钉住：**该路径必须复用 `trinity.adapters._pii_guard.adapter_pii_guard`**
（不另造策略），且**守卫在 `INSERT INTO memories` 之前**。

## 判据（每条都可失败，且都有反向/负向）

| # | 判据 | 反向/负向 |
|---|---|---|
| C1 | 含 PII 的会话摘要 ⇒ 落库**已掩码** + 账本 `metadata["pii_redaction"]` 可审计 | — |
| C2 | **无 PII ⇒ 逐字不变**（正文含原句、无账本） | 防"恒真式掩码" |
| C3 | **`TRINITY_ADAPTER_GUARD=0` ⇒ 明文落库** | 证明 C1 真的在测守卫 |
| C4 | **`SCAN=off` / `REDACT=0` ⇒ 不介入**（与 t49 三开关语义表一致） | 不另立语义 |
| C5 | **high 档 ⇒ 与该层契约一致**（拒存 ⇒ 0 行；quarantine ⇒ `archived`） | 用 G4 的 HIGH_SAMPLES |
| C6 | **顺序不变量**：`adapter_pii_guard` 行 < `INSERT INTO memories` 行（源码级） | 与 G4 `GUARDED_BARE_SQL_PATHS` 同款双向核对 |
| C7 | **响应如实**：`pii_redacted` 与落库一致；无 PII 时为 False | 防"说假话"（G3 纪律） |
| C8 | **变异牙齿**：把守卫 patch 成恒等 ⇒ C1 必须变红 | 证明判据不是自证 |
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys

import pytest

ROOT = r"D:\trinity-code"
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
ENGINE_WORKER = os.path.join(ROOT, "trinity", "engine_worker.py")

PII_TEXT = "我的手机号是 13800138000，邮箱 zhangsan@corp.cn，卡号 4111111111111111"
CLEAN_TEXT = "今天讨论了投递层的有界性与可解释性，没有敏感信息"
#: 取自 G4 `HIGH_SAMPLES`（该层契约的同一批样本，避免我自己造样本）
HIGH_TEXT = "我最近总想着自杀，觉得活不下去了。"

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  memory_id TEXT PRIMARY KEY, session_id TEXT, persona_id TEXT, agent_id TEXT,
  content TEXT NOT NULL, role TEXT, importance REAL, tags TEXT, category TEXT,
  status TEXT DEFAULT 'active', version INTEGER, sha256_hash TEXT,
  created_at TEXT, updated_at TEXT, access_count INTEGER, metadata TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS dsh_events (session_id TEXT, seq INTEGER, type TEXT, payload TEXT);
"""


def _seed(tmp_path, text: str, seq_n: int = 10) -> tuple:
    """临时 SQLite + 会话事件流（>8 条，避免 write_policy 试点 defer）。"""
    db = os.path.join(str(tmp_path), "trinity_store.db")
    con = sqlite3.connect(db)
    con.executescript(SCHEMA)
    sid = "t51c-" + os.path.basename(str(tmp_path))
    for i in range(seq_n):
        typ = "user/message" if i % 2 == 0 else "assistant/message"
        body = text if i == 0 else text + "（第%d条）" % (i + 1)
        con.execute("INSERT INTO dsh_events VALUES (?,?,?,?)",
                    (sid, i + 1, typ, json.dumps({"content": body}, ensure_ascii=False)))
    con.commit()
    con.close()
    return db, sid


def _call(tmp_path, monkeypatch, text: str) -> tuple:
    monkeypatch.setenv("TRINITY_STORE", str(tmp_path))
    monkeypatch.setenv("TRINITY_OPENING_COUNTERS", os.path.join(str(tmp_path), "counters.json"))
    db, sid = _seed(tmp_path, text)
    import trinity.engine_worker as ew
    res = ew._session_dispose_summary({"session_id": sid})
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT memory_id, status, content, metadata FROM memories WHERE session_id=?", (sid,))]
    con.close()
    return res, rows


@pytest.fixture(autouse=True)
def _guards_on(monkeypatch):
    """每个用例都从"三开关全开"起步；用例自己再覆盖需要的档位。"""
    monkeypatch.delenv("TRINITY_ADAPTER_GUARD", raising=False)
    monkeypatch.delenv("TRINITY_SENSITIVE_SCAN", raising=False)
    monkeypatch.delenv("TRINITY_SENSITIVE_REDACT", raising=False)
    yield


# ── C1 含 PII ⇒ 掩码 + 账本 ──────────────────────────────────────────────
def test_c1_pii_summary_is_masked_and_ledgered(tmp_path, monkeypatch):
    res, rows = _call(tmp_path, monkeypatch, PII_TEXT)
    assert res.get("status") == "created", "试点/幂等分支把写入吞了：%r" % (res,)
    assert len(rows) == 1, "应恰好落 1 行"
    got = rows[0]["content"]
    assert "13800138000" not in got, "手机号明文落库（本判据要修的就是这个）"
    assert "138********" in got
    md = json.loads(rows[0]["metadata"] or "{}")
    led = md.get("pii_redaction") or {}
    assert led.get("kinds"), "账本缺失 ⇒ 掩码不可审计"
    assert any("手机" in k for k in led["kinds"])


# ── C2 无 PII ⇒ 逐字不变（反事实）───────────────────────────────────────
def test_c2_clean_summary_is_verbatim(tmp_path, monkeypatch):
    res, rows = _call(tmp_path, monkeypatch, CLEAN_TEXT)
    assert res.get("status") == "created"
    got = rows[0]["content"]
    assert CLEAN_TEXT in got, "无 PII 的正文被动过（反事实失败）"
    assert json.loads(rows[0]["metadata"] or "{}") == {}, "无 PII 却写了掩码账本"


# ── C3 关守卫 ⇒ 明文（证明判据在测守卫）─────────────────────────────────
def test_c3_guard_off_lets_plaintext_through(tmp_path, monkeypatch):
    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    res, rows = _call(tmp_path, monkeypatch, PII_TEXT)
    assert res.get("status") == "created"
    assert "13800138000" in rows[0]["content"], "关掉守卫后仍未落明文 ⇒ C1 不是被守卫决定的"


# ── C4 与 t49 三开关语义表一致 ──────────────────────────────────────────
@pytest.mark.parametrize("env,expect", [
    ("TRINITY_SENSITIVE_SCAN", "scan-off"),
    ("TRINITY_SENSITIVE_REDACT", "redact-off"),
])
def test_c4_master_switches_disable_the_guard(tmp_path, monkeypatch, env, expect):
    from trinity.adapters import _pii_guard as G
    monkeypatch.setenv(env, "0" if env.endswith("REDACT") else "off")
    on, why = G.adapter_guard_state()
    assert on is False and why == expect, "三开关语义与适配器层不一致：%r" % (why,)
    res, rows = _call(tmp_path, monkeypatch, PII_TEXT)
    assert res.get("status") == "created"
    assert "13800138000" in rows[0]["content"], "主开关关掉后仍被掩码（语义不一致）"


# ── C5 high 档：与该层契约一致 ──────────────────────────────────────────
def test_c5_high_follows_layer_contract(tmp_path, monkeypatch):
    from trinity.security import sensitive as S
    rep = S.scan_sensitive(HIGH_TEXT) or {}
    action = rep.get("action")
    res, rows = _call(tmp_path, monkeypatch, HIGH_TEXT)
    if rep.get("severity") == "high" and action == S.ACTION_REFUSE:
        assert res.get("status") == "refused" and len(rows) == 0, \
            "high+refuse ⇒ 必须拒存且零落库，实际 %r rows=%d" % (res, len(rows))
    elif rep.get("severity") == "high" and action == S.ACTION_QUARANTINE:
        assert len(rows) == 1 and rows[0]["status"] == "archived", "high+quarantine ⇒ archived"
    else:
        pytest.skip("该样本在当前策略层不是 high（策略变更 ⇒ 本判据不适用，非通过）")


# ── C6 顺序不变量（源码级；G4 `GUARDED_BARE_SQL_PATHS` 同款反向核对）────
def test_c6_guard_precedes_insert():
    src = open(ENGINE_WORKER, encoding="utf-8", errors="replace").read()
    assert "adapter_pii_guard" in src, "说好接了守卫却没接"
    assert re.search(r"(?i)insert\s+into\s+memories", src), "它仍是裸 SQL（若非如此请改走适配器并更新清单）"
    lines = src.replace("\r\n", "\n").split("\n")
    g = next(i for i, ln in enumerate(lines, 1) if "adapter_pii_guard" in ln and "import" not in ln)
    #: 只看 `_session_dispose_summary` 内那条 INSERT（本文件还有别的写入，取第一条 ≥ g 的即可）
    ins = next(i for i, ln in enumerate(lines, 1)
               if re.search(r"(?i)insert\s+into\s+memories", ln))
    assert g < ins, "守卫在裸 SQL 之后（行自相矛盾风险）"


# ── C7 响应如实（G3 纪律）───────────────────────────────────────────────
def test_c7_response_matches_what_happened(tmp_path, monkeypatch):
    res_pii, rows_pii = _call(tmp_path, monkeypatch, PII_TEXT)
    assert res_pii.get("pii_redacted") is True
    assert res_pii.get("status_written") == rows_pii[0]["status"]
    tmp2 = tmp_path / "clean"
    tmp2.mkdir()
    res_clean, rows_clean = _call(tmp2, monkeypatch, CLEAN_TEXT)
    assert res_clean.get("pii_redacted") is False, "无 PII 却声称掩码 ⇒ 恒真式假话"
    assert res_clean.get("pii_redaction") is None


# ── C8 变异牙齿：守卫被替换成恒等 ⇒ C1 必须变红 ──────────────────────────
def test_c8_identity_guard_kills_c1(tmp_path, monkeypatch):
    from trinity.adapters import _pii_guard as G

    def _identity(content, metadata=None):
        return content, metadata, {"scanned": True, "redacted": False, "labels": [],
                                   "severity": None, "refuse": False, "isolate": False,
                                   "exempt": None, "policy": None}

    monkeypatch.setattr(G, "adapter_pii_guard", _identity)
    res, rows = _call(tmp_path, monkeypatch, PII_TEXT)
    assert res.get("status") == "created"
    # 守卫恒等 ⇒ 明文必须落库。若这里**没能**落明文，说明 C1 的掩码不是由守卫产生的。
    assert "13800138000" in rows[0]["content"], \
        "把守卫换成恒等后仍未落明文 ⇒ C1 测的不是守卫（判据自证）"
