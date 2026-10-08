# -*- coding: utf-8 -*-
"""D-1 回填的【永久可告警】判据：那 2 行**有意跳过**的明文 PII。

作者：corpus-quality（t105/G2 D-1）· 建 2026-10-08 · 写域：本文件（队长裁定 (2) 只放行这一个新文件）

── 为什么有这 2 行（**不要"修"它们**）─────────────────────────────────────────────
D-1 回填把存量里**仍是明文的 PII 行**按 G2 口径掩码，并把 `content`/`content_hash`/`sha256_hash`
**三列同一事务同写**（hash 形态 = 候选 B = `sha256(掩码后文本)`）。
其中 2 行在掩码后**新 `content_hash` 与一条现存 `active` 行相撞**，而唯一索引是
`idx_memories_content_hash (persona_id, agent_id, content_hash) WHERE content_hash IS NOT NULL AND status='active'`
⇒ 写入会**违反唯一索引**。实测（`evidence/g2_collision_verdict.json`）：这两对**掩码前内容不同**、
仅**掩码后**变得相同 ⇒ **归档其一 = 不可逆地销毁一份不同的记忆** ⇒ 裁定 = **保持明文不动 + 永久可告警**。
政策本身（掩码不可逆性 vs 唯一索引假定）已登记为 **D-16，交用户裁定**。

── ⭐ 唯一权威 = 本文件内的 `SKIPPED_REGISTRY` 常量 ──────────────────────────────
`evidence/g2_skipped_rows.json` 只是**证据快照**，**不是权威**；两份不一致时**以本文件为准**。
⭐ **将来若出现第 3 条被跳过的行，必须同时把新条目加进本常量**（判据①的"恰好覆盖"会红，逼着后来者加）。
本条纪律与"登记必须可查"同族：**删登记不能当修好**（由判据③的牙齿钉住）。
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

# ── 唯一权威：有意跳过的行（**只加不删**由构造保证）──────────────────────────────
SKIPPED_REGISTRY = [
    {
        "memory_id": "mem_6acaa9a18e984576",
        "counterpart_memory_id": "mem_9fe1af3a96964c52",
        "reason": "因掩码后 hash 碰撞而跳过，仍是明文 PII",
        "date": "2026-10-08",
        "index": "idx_memories_content_hash (partial on active)",
    },
    {
        "memory_id": "mem_f4be0b81d6c14402",
        "counterpart_memory_id": "mem_2c3fc95d621f48dd",
        "reason": "因掩码后 hash 碰撞而跳过，仍是明文 PII",
        "date": "2026-10-08",
        "index": "idx_memories_content_hash (partial on active)",
    },
]
REQUIRED_FIELDS = ("memory_id", "counterpart_memory_id", "reason", "date", "index")
STORE = os.path.expanduser(os.path.join("~", ".trinity", "store", "trinity_store.db"))


def _registry_violations(registry) -> list:
    """登记表的问题（**字段缺失 / 重复 id / 与有意跳过集合不符**）⇒ 返回问题列表。"""
    bad = []
    if not registry:
        bad.append("登记表为空：有意跳过的行必须留在登记里（不许用'删登记'当修好）")
        return bad
    seen = set()
    for i, rec in enumerate(registry):
        for f in REQUIRED_FIELDS:
            if not str(rec.get(f) or "").strip():
                bad.append("第 %d 条缺字段 %s" % (i, f))
        mid = rec.get("memory_id")
        if mid in seen:
            bad.append("memory_id 重复：%s" % mid)
        seen.add(mid)
    return bad


def _store_violations(rows) -> list:
    """给定 [(memory_id, is_still_unmasked)] ⇒ 返回问题列表（**未登记却已掩码**= 有人绕过碰撞判定直接改过）。"""
    bad = []
    registered = {r["memory_id"] for r in SKIPPED_REGISTRY}
    for mid, still_unmasked in rows:
        if mid in registered and not still_unmasked:
            bad.append("已登记为'仍是明文'的行 %s 现在已是掩码态 ⇒ 有人绕过碰撞判定改了它" % mid)
    return bad


def _read_store_rows() -> list:
    """**只读**生产库，返回 [(memory_id, 解密后是否仍会被掩码改变)]。

    ⚠️ 看不到库就 `skip`（**给理由**）—— 本机实测**真跑**（见 t105 报告的 `-rA` 证据）。
    """
    if not os.path.exists(STORE):
        pytest.skip("生产库不存在，无法核对跳过行现状：%s（不是判据失败，是环境不具备）" % STORE)
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    from trinity.security import sensitive as S                      # noqa: PLC0415
    from trinity.security.crypto import decrypt_content              # noqa: PLC0415

    con = sqlite3.connect("file:%s?mode=ro" % STORE.replace("\\", "/"), uri=True)
    try:
        out = []
        for rec in SKIPPED_REGISTRY:
            r = con.execute("SELECT content FROM memories WHERE memory_id=?",
                            (rec["memory_id"],)).fetchone()
            if r is None:
                out.append((rec["memory_id"], None))     # 行不在了 ⇒ 由判据处理
                continue
            raw = r[0] or ""
            plain = decrypt_content(raw) if raw.startswith("enc:v1:") else raw
            masked, _labels = S.redact_identifiers(str(plain), cause="pii")
            out.append((rec["memory_id"], masked == plain))   # True = 解密后仍是未掩码的明文
        return out
    finally:
        con.close()


# ── 判据①：登记可查（恰好覆盖 + 字段齐全）───────────────────────────────────────
def test_d1_registry_covers_exactly_the_skipped_rows():
    assert _registry_violations(SKIPPED_REGISTRY) == [], (
        "登记表自身有问题（缺字段/重复/为空）⇒ 那 2 行就失去了可查性：%r"
        % _registry_violations(SKIPPED_REGISTRY))
    ids = {r["memory_id"] for r in SKIPPED_REGISTRY}
    assert ids == {"mem_6acaa9a18e984576", "mem_f4be0b81d6c14402"}, (
        "登记集合与 D-1 实测的'有意跳过'集合不符：%r" % sorted(ids))


# ── 判据②：那 2 行**必须仍是未掩码的明文 PII**（反向牙齿：被静默掩码 ⇒ 红）────────
def test_d1_skipped_rows_are_still_unmasked_plaintext_pii():
    rows = _read_store_rows()
    missing = [m for m, ok in rows if ok is None]
    assert not missing, "登记在册的行在库里不存在了：%r（要么补回，要么把登记改成'已删除'）" % missing
    assert _store_violations(rows) == [], _store_violations(rows)
    assert all(ok for _m, ok in rows), (
        "有登记行已不再是明文态 ⇒ 说明有人绕过碰撞判定直接改了它：%r" % rows)


# ── 判据③ 牙齿（防"删登记当修好"）：清空登记 ⇒ 校验必须红 ────────────────────────
def test_tooth_clearing_registry_must_fail(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "SKIPPED_REGISTRY", [])
    with pytest.raises(AssertionError):
        import types  # noqa: F401
        reg = sys.modules[__name__].SKIPPED_REGISTRY
        assert _registry_violations(reg) == [], "登记被清空 ⇒ 判据①本应变红（不许把删登记当修好）"


# ── 判据④ 牙齿（防"静默掩码"）：把某行伪装成已掩码 ⇒ 判据②的信道必须红 ────────────
def test_tooth_masked_skipped_row_must_fail():
    fake_rows = [(SKIPPED_REGISTRY[0]["memory_id"], False)]   # False = 已掩码
    with pytest.raises(AssertionError):
        assert _store_violations(fake_rows) == [], _store_violations(fake_rows)
