# -*- coding: utf-8 -*-
"""t59/H3 判据：`ingest_batch` 的返回必须**说真话** —— 每条可判定 inserted/deduped + 表级真实计数。

被修缺陷：200 条（50 唯一）送进 `ingest_batch` ⇒ **表格只涨 50 行**，而返回值是 200 条"成功"
（每条都有 `memory_id`、没有表级计数、**新写入的那 50 条一个标记都没有**）⇒ **去重被静默**。

⚠️ 本判据**不改去重本身**（那是有意设计）：所有断言都建立在"**去重仍然发生**"之上
（`rows_added == 唯一数`），只是要求返回值**如实报告**它。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BATCH = REPO / "trinity" / "adapters" / "sqlite" / "_batch.py"
CRUD = REPO / "trinity" / "adapters" / "sqlite" / "_crud.py"

N_TOTAL, N_UNIQUE = 200, 50
HIGH = "我得了抑郁症，手机号 13800138000。"      # scan_sensitive: action=refuse/severity=high


def _adapter(tmp_path, name="t59.db"):
    from trinity.adapters.sqlite import SQLiteAdapter

    ad = SQLiteAdapter(db_path=str(tmp_path / name))
    ad.connect()
    return ad


def _recs(n_total, n_unique, prefix="唯一"):
    return [{"content": "%s内容 %03d 用于 t59 去重判据" % (prefix, i % n_unique),
             "agent_id": "t59", "persona_id": "t59", "category": "general"}
            for i in range(n_total)]


def _rows(ad) -> int:
    return int(ad._conn.execute("SELECT count(*) FROM memories").fetchone()[0])


# ── ① 核心：200 送（50 唯一）⇒ 计数必须等于真实新增行数 ─────────────────────
def test_重复批次_计数必须等于真实新增行数_t59(tmp_path):
    ad = _adapter(tmp_path)
    before = _rows(ad)
    out = ad.ingest_batch(_recs(N_TOTAL, N_UNIQUE))
    added = _rows(ad) - before

    assert len(out) == N_TOTAL, "返回条数应等于送入条数：%d" % len(out)
    assert added == N_UNIQUE, "前提失效：去重行为变了（应恰好落 %d 行，实测 %d）" % (N_UNIQUE, added)

    # **这两条就是本次核心**：逐条标记的计数必须与真实新增行数对齐
    assert out.inserted_count == added == N_UNIQUE, (
        "`inserted_count`(%d) 必须等于**真实新增行数**(%d) —— 否则又是'假成功'"
        % (out.inserted_count, added))
    assert out.deduped_count == N_TOTAL - N_UNIQUE, (
        "`deduped_count` 应为其余 %d 条，实测 %d" % (N_TOTAL - N_UNIQUE, out.deduped_count))
    assert out.rows_added == added, "表级 `rows_added` 必须是实测值：%d vs %d" % (
        out.rows_added, added)
    assert out.silent_drop is True, "200 送只落 50 行 ⇒ `silent_drop` 必须为真（诚实标记）"
    # 逐条可判定：每条**恰好**是 inserted 或 deduped 之一
    assert all((r.get("inserted") is True) ^ (r.get("deduped") is True) for r in out), (
        "每条结果都必须能机器判定 inserted/deduped（且互斥）")
    assert out.counts()["inserted"] + out.counts()["deduped"] + out.counts()["failed"] == N_TOTAL


# ── ② 反事实：无重复批次 ⇒ 全部 inserted（不得恒判 deduped）──────────────────
def test_无重复批次必须全部inserted_t59(tmp_path):
    ad = _adapter(tmp_path, "t59_uniq.db")
    n = 30
    out = ad.ingest_batch(_recs(n, n, prefix="独一"))
    assert out.inserted_count == n, "无重复时 `inserted_count` 应为 %d，实测 %d" % (
        n, out.inserted_count)
    assert out.deduped_count == 0, "无重复时不该有 deduped：%d" % out.deduped_count
    assert out.rows_added == n and out.silent_drop is False, (
        "无重复 ⇒ rows_added==sent 且 silent_drop=False：%r" % out.counts())
    assert all(r.get("inserted") is True and r.get("deduped") is False for r in out)


# ── ③ 牙齿：把去重分支改成"不写但报 inserted" ⇒ 判据必须红 ──────────────────
def test_牙齿_去重报inserted必须被判红_t59(tmp_path, monkeypatch):
    """人为破坏：让去重早退**谎报** `inserted=True` ⇒ ①的"计数==真实新增行数"必须失败。"""
    ad = _adapter(tmp_path, "t59_tooth.db")
    real = ad.store_memory

    def _lying_store_memory(*a, **kw):
        r = real(*a, **kw)
        if isinstance(r, dict) and r.get("deduped") is True:
            r = dict(r, inserted=True, deduped=False)      # ← 假话
        return r

    monkeypatch.setattr(ad, "store_memory", _lying_store_memory)
    out = ad.ingest_batch(_recs(N_TOTAL, N_UNIQUE))
    added = _rows(ad)
    assert out.silent_drop is True                 # 真值仍然显示丢了 150 条
    # **判据①的核心断言在谎报下必须失败**（这里显式演示"红"）：
    with pytest.raises(AssertionError):
        assert out.inserted_count == added, (
            "谎报 inserted ⇒ 判据①本应失败：inserted_count=%d，真实新增=%d" % (
                out.inserted_count, added))


# ── ④ 去重本身未变（对照）+ 幂等：同一批再送 ⇒ 0 新增 ────────────────────────
def test_去重行为未变且幂等_t59(tmp_path):
    ad = _adapter(tmp_path, "t59_idem.db")
    recs = _recs(N_TOTAL, N_UNIQUE)
    first = ad.ingest_batch(recs)
    added1 = first.rows_added
    second = ad.ingest_batch(recs)
    assert added1 == N_UNIQUE, "第一次应恰好落 %d 行（去重行为未被改动）：%d" % (
        N_UNIQUE, added1)
    assert second.rows_added == 0, "同一批再送应 0 新增（幂等）：%d" % second.rows_added
    assert second.inserted_count == 0 and second.deduped_count == N_TOTAL


# ── ⑤ 第三桶：被策略拒存的条要被计成 failed（既不 inserted 也不 deduped）────
def test_拒存条计入failed_count_t59(tmp_path):
    ad = _adapter(tmp_path, "t59_failed.db")
    out = ad.ingest_batch([{"content": HIGH, "agent_id": "t59", "persona_id": "t59",
                            "category": "general"}])
    assert out.inserted_count == 0 and out.deduped_count == 0, out.counts()
    assert out.failed_count == 1, "被拒存的条应计入 failed：%r" % out.counts()
    assert out.rows_added == 0, "拒存 ⇒ 不该有新增行：%d" % out.rows_added


# ── ⑥ 单条 `store_memory` 同族字段（判定：需要，且与批量同一套）──────────────
def test_单条通道同族字段_inserted与deduped互斥_t59(tmp_path):
    ad = _adapter(tmp_path, "t59_single.db")
    a = ad.store_memory(content="单条判据内容 AAA", agent_id="t59", persona_id="t59",
                        category="general")
    assert a.get("inserted") is True and a.get("deduped") is False, (
        "新写入的单条结果应 inserted=True/deduped=False：%r" % a)
    assert a.get("version_id"), "新写入应有 version_id"
    b = ad.store_memory(content="单条判据内容 AAA", agent_id="t59", persona_id="t59",
                        category="general")
    assert b.get("inserted") is False and b.get("deduped") is True, (
        "去重早退的单条结果应 inserted=False/deduped=True：%r" % b)
    assert b.get("version_id") is None and b.get("dedup") is True, (
        "沿用既有 `dedup=True`/`version_id=None` 语义（未破坏老消费者）：%r" % b)
    assert b.get("memory_id") == a.get("memory_id"), "去重早退应返回既有 memory_id"


# ── ⑦ 结构性：批量返回值必须仍是 list（向后兼容）+ 计数来自实测 ──────────────
def test_返回值仍是list且计数来自实测_t59(tmp_path):
    ad = _adapter(tmp_path, "t59_shape.db")
    out = ad.ingest_batch(_recs(5, 5, prefix="形状"))
    assert isinstance(out, list), "返回值必须仍是 list（向后兼容）"
    assert out[0] is not None and len(out) == 5 and out == list(out)
    src = BATCH.read_text(encoding="utf-8")
    assert "SELECT count(*) FROM memories" in src, (
        "`rows_added` 必须来自**实测行数差**，不是逐条推断")
    assert "silent_drop" in src, "缺少 `silent_drop` 诚实标记"
    # 去重分支必须给出 inserted/deduped（与主返回同一套字段）
    csrc = CRUD.read_text(encoding="utf-8")
    assert '"inserted": False, "deduped": True' in csrc, "去重早退支缺少 inserted/deduped"
    assert '"inserted": True,' in csrc, "主返回支缺少 inserted"
