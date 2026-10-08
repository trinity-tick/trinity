# -*- coding: utf-8 -*-
"""t60/H4 判据：**PG 批量通道**的返回必须与 SQLite（t59）**同一套字段与口径**。

⚠️ 本轮**实测推翻**了 H3-R1 的前提：PG 这条批量 INSERT **没有 `ON CONFLICT`**、本表
`content_hash` **没有唯一索引**（只有 `memories_pkey` 唯一）⇒ **PG 批量不去重、不丢写**
（实测 20 送 20 行）。所以本轮的"修"是**契约与可观测性**面：
  · 逐条 `inserted`/`deduped`（**由"按 memory_id 回查是否真的落行"实测得出，不是常量**）；
  · 表级 `sent / rows_added（实测 count 差）/ inserted_count / deduped_count / failed_count / silent_drop`。
⚠️ 正因为 PG 目前**永不**去重，**"恒返回 inserted=True"的实现也能骗过天真判据** ⇒
本文件专门加了两条：**回查说没落行 ⇒ 必须报 `deduped=True`**、**回查失败 ⇒ 必须报"未知"**。

判据全部用**假连接**（`_get_conn` 代理）⇒ 不需真 PG、零副作用；
真 PG 的"事务内 + ROLLBACK + 基线逐值相同"证据在 `evidence/t60_pg_batch_return_repro.json`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PG = REPO / "trinity" / "adapters" / "postgresql.py"

N_TOTAL, N_UNIQUE = 20, 5


class _FakeCursor:
    def __init__(self, outer):
        self.o = outer

    def executemany(self, sql, seq):
        self.o.log.append(("executemany", sql, list(seq)))
        if "INSERT INTO MEMORIES" in (sql or "").upper():
            self.o.inserted_ids.update(str(r[0]) for r in (seq or []) if r)

    def execute(self, sql, params=None):
        self.o.log.append(("execute", sql, params))
        up = (sql or "").upper()
        if "COUNT(*) FROM MEMORIES" in up:
            idx = self.o.count_calls
            self.o.count_calls += 1
            seq = self.o.count_sequence
            self.o.last_count = seq[min(idx, len(seq) - 1)]
        if "WHERE MEMORY_ID = ANY" in up:
            if self.o.probe_raises:
                raise RuntimeError("probe failure (模拟回查失败)")
            self.o.probe_called = True
            # 默认：**回查看到同事务内已插入的那些行**（= 真 PG 行为）；
            # `probe_ids=set()` / 自定义集合 用于反事实与牙齿。
            if self.o.probe_ids is None:
                self.o.probe_result = set(self.o.inserted_ids)
            else:
                self.o.probe_result = set(self.o.probe_ids)

    def fetchone(self):
        return (int(self.o.last_count),)

    def fetchall(self):
        return [(i,) for i in sorted(self.o.probe_result)]

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    """可调假连接：`count_sequence` 控制行数读数；`probe_ids` 控制"回查说谁落了行"。"""

    def __init__(self, count_sequence=(100, 120), probe_ids=None, probe_raises=False):
        self.log = []
        self.count_sequence = list(count_sequence)
        self.count_calls = 0
        self.last_count = self.count_sequence[0]
        self.inserted_ids = set()
        #: `None` ⇒ 回查返回**本事务内实际插入的 id**（贴近真 PG）；`set()`/自定义 ⇒ 反事实/牙齿
        self.probe_ids = probe_ids
        self.probe_result = set()
        self.probe_called = False
        self.probe_raises = probe_raises

    def cursor(self, *a, **k):
        return _FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _adapter(monkeypatch, fake: _FakeConn):
    from trinity.adapters.postgresql import PostgreSQLAdapter

    ad = PostgreSQLAdapter(host="127.0.0.1", port=5432, dbname="trinity",
                           user="trinity", password="x", auto_connect=False)
    ad._connected = True
    monkeypatch.setattr(ad, "_get_conn", lambda: fake, raising=False)
    return ad


def _recs(n_total, n_unique, prefix="PG探针"):
    return [{"content": "%s 唯一 %02d" % (prefix, i % n_unique), "agent_id": "t60",
             "persona_id": "t60", "category": "general"} for i in range(n_total)]


def _run(monkeypatch, fake=None, **kw):
    """跑一次批量。回查默认看到"同事务内已插入的行"（= 真 PG 行为，见 `_FakeCursor`）。"""
    fake = fake or _FakeConn(**kw)
    ad = _adapter(monkeypatch, fake)
    return fake, ad.ingest_batch(_recs(N_TOTAL, N_UNIQUE))


# ── ① 核心：inserted_count == rows_added，且逐条 inserted^deduped 互斥 ────────
def test_核心_inserted计数必须等于实测新增行数_t60(monkeypatch):
    fake = _FakeConn(count_sequence=(100, 100 + N_TOTAL))
    _, out = _run(monkeypatch, fake)
    assert len(out) == N_TOTAL, "返回条数应等于送入条数：%d" % len(out)
    assert out.rows_added == N_TOTAL, "`rows_added` 必须来自实测 count 差：%d" % out.rows_added
    assert out.inserted_count == out.rows_added == N_TOTAL, (
        "`inserted_count`(%d) 必须等于 `rows_added`(%d)" % (out.inserted_count, out.rows_added))
    assert out.silent_drop is False, "20 送 20 落 ⇒ 不该报静默丢写：%r" % out.counts()
    for r in out:
        assert (r.get("inserted") is True) ^ (r.get("deduped") is True), (
            "每条必须能机器判定 inserted/deduped 且互斥：%r" % r)
    assert sorted(out[0].keys()) != sorted(k for k in out[0] if k != "inserted"), "字段存在"


# ── ② 关键反事实（PG 专用）：回查说**没落行** ⇒ 必须报 `deduped=True` ────────
def test_回查说没落行必须报deduped_不得恒判inserted_t60(monkeypatch):
    """**这条是防"恒 true"的关键**：PG 目前永不去重 ⇒ 天真的"永远 inserted=True"也能过①。

    这里让"回查"报告**一个都没落行**（模拟将来出现去重/忽略语义）⇒ 实现必须如实报 `deduped`。
    """
    fake = _FakeConn(count_sequence=(100, 100), probe_ids=set())
    fake.probe_raises = False
    ad = _adapter(monkeypatch, fake)
    out = ad.ingest_batch(_recs(N_TOTAL, N_UNIQUE))
    assert out.inserted_count == 0, "回查没找到任何 memory_id ⇒ inserted_count 必须为 0（不得恒真）"
    assert out.deduped_count == N_TOTAL, "没落行的每条都应报 deduped=True：%d" % out.deduped_count
    assert all(r.get("deduped") is True and r.get("inserted") is False for r in out)


# ── ③ 牙齿：报 inserted 但表**没涨**（不写却报成功）⇒ 判据①必须红 ────────────
def test_牙齿_报inserted但表没涨必须判红_t60(monkeypatch):
    """`count(*)` 读数不变（= 实际没新增行）而"回查"仍说都在 ⇒ **①的核心断言必须失败**。"""
    fake = _FakeConn(count_sequence=(100, 100))       # 表没涨
    _, out = _run(monkeypatch, fake)                  # 但回查说 20 条都在 ⇒ inserted=20
    assert out.inserted_count == N_TOTAL
    assert out.rows_added == 0
    assert out.silent_drop is True, "表没涨却报 20 条成功 ⇒ `silent_drop` 必须为真"
    with pytest.raises(AssertionError):
        assert out.inserted_count == out.rows_added, (
            "不写却报 inserted ⇒ 判据①本应失败（inserted_count=%d, rows_added=%d）"
            % (out.inserted_count, out.rows_added))


# ── ④ 去重行为未变：PG 本就**不去重**（实测），且我未改 SQL ──────────────────
def test_去重行为未变_PG批量本就不去重_t60(tmp_path, monkeypatch):
    fake = _FakeConn(count_sequence=(100, 100 + N_TOTAL))
    _, out = _run(monkeypatch, fake)
    assert out.deduped_count == 0, "当前 PG 无唯一索引/无 ON CONFLICT ⇒ 不该出现 deduped：%d" % (
        out.deduped_count)
    # 结构证据：本次改动**没有动**这两条 INSERT（批量仍是批量）
    src = PG.read_text(encoding="utf-8")
    assert "INSERT INTO memories" in src and "INSERT INTO memory_versions" in src
    assert "executemany" in src, "批量仍走 executemany（未被改成逐条）"
    assert "ON CONFLICT" not in src.split("def ingest_batch")[1].split("def ")[0], (
        "ingest_batch 里不该出现 ON CONFLICT（去重语义未被引入）")


# ── ⑤ 回查失败 ⇒ 必须诚实报"未知"（进 failed_count），**不猜** ────────────────
def test_回查失败时必须报未知而不是硬编码_t60(monkeypatch):
    fake = _FakeConn(count_sequence=(100, 100 + N_TOTAL), probe_raises=True)
    ad = _adapter(monkeypatch, fake)
    out = ad.ingest_batch(_recs(N_TOTAL, N_UNIQUE))
    assert out.inserted_count == 0 and out.deduped_count == 0, (
        "回查失败时不得猜 inserted/deduped：%r" % out.counts())
    assert out.failed_count == N_TOTAL, "未知应计入 failed_count：%r" % out.counts()
    assert all(r.get("inserted") is None and r.get("deduped") is None for r in out), (
        "未知必须是 None（显式），不能被硬编码成 True/False")


# ── ⑥ 与 SQLite 同套字段与口径 + 向后兼容 ──────────────────────────────────
def test_与SQLite同套字段且向后兼容_t60(monkeypatch):
    from trinity.adapters.sqlite._batch import BatchResults

    fake = _FakeConn(count_sequence=(100, 100 + N_TOTAL))
    _, out = _run(monkeypatch, fake)
    assert isinstance(out, BatchResults), "必须复用 t59 的 `BatchResults`（同一套字段）"
    assert isinstance(out, list) and out == list(out), "仍是 list（len/索引/迭代语义不变）"
    for f in ("sent", "rows_added", "inserted_count", "deduped_count", "failed_count", "silent_drop"):
        assert hasattr(out, f), "缺少 t59 的表级字段：%s" % f
    assert out.counts()["sent"] == N_TOTAL
    src = PG.read_text(encoding="utf-8")
    assert "SELECT count(*) FROM memories" in src, "`rows_added` 必须来自实测 count 差（t59 口径）"
    assert "WHERE memory_id = ANY" in src, "逐条标记必须来自**回查实测**"
