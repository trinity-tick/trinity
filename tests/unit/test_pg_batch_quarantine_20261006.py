# -*- coding: utf-8 -*-
"""t55/G14 判据：**PG 批量通道的隔离行也必须先掩码**（"归档 ≠ 安全"）。

背景（队长 PG 冒烟实测）：high 档在批量通道里落 `status='archived'`，但**正文是原始明文**
（守卫对 refuse 档返回未掩码正文 —— 单条通道靠"根本不落库"兜住，批量通道要保 1:1 ⇒ 必须自己补掩）。

判据用**假连接**（`_get_conn` 代理）捕获真正交给 `executemany` 的参数 ⇒ 无需真 PG、零副作用；
真 PG 的"事务内读回 + ROLLBACK"证据在 `evidence/t55_pg_batch_smoke.json`（基线计数逐值相同、0 行残留）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PG = REPO / "trinity" / "adapters" / "postgresql.py"

HIGH = "我得了抑郁症，手机号 13800138000。"          # scan_sensitive: action=refuse severity=high
MEDIUM = "我正在做心理治疗，手机号 13800138000。"     # action=redact severity=medium
PII_ONLY = "队长PG冒烟：手机号 13800138000。"          # action=redact（仅 PII）
RAW = "13800138000"
MASKED = "138********"


# ── 假连接：捕获 executemany 的 SQL 与参数 ───────────────────────────────────
class _FakePG:
    def __init__(self):
        self.log = []          # [(sql, params_list_or_params)]

    class _Cur:
        def __init__(self, outer):
            self.o = outer

        def executemany(self, sql, seq):
            self.o.log.append((sql, list(seq)))

        def execute(self, sql, params=None):
            self.o.log.append((sql, params))

        def fetchall(self):
            return []

        def fetchone(self):
            return None

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def cursor(self):
        return _FakePG._Cur(self)

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

    # 取落 memories 那一条 executemany 的参数（每行是一个 tuple）
    def memory_rows(self) -> list:
        out = []
        for sql, params in self.log:
            if "INSERT INTO MEMORIES" in (sql or "").upper():
                out.extend(params if isinstance(params, list) else [params])
        return out


def _adapter(monkeypatch, fake: _FakePG):
    from trinity.adapters.postgresql import PostgreSQLAdapter

    ad = PostgreSQLAdapter(host="127.0.0.1", port=5432, dbname="trinity",
                           user="trinity", password="x", auto_connect=False)
    ad._connected = True
    monkeypatch.setattr(ad, "_get_conn", lambda: fake, raising=False)
    return ad


def _recs(*texts):
    return [{"content": t, "agent_id": "t55", "persona_id": "t55", "category": "general"}
            for t in texts]


def _content_status_of(rows, needle):
    """从落库参数里找含 `needle` 的那一行，返回 (content, status)。"""
    for row in rows:
        for v in row:
            if isinstance(v, str) and needle in v:
                content = v
                status = next((x for x in row if x in ("active", "archived")), None)
                return content, status
    return None, None


# ── ① 核心判据：high ⇒ **归档行正文必须已掩码** ─────────────────────────────
def test_high档归档行正文必须已掩码_t55(monkeypatch):
    fake = _FakePG()
    ad = _adapter(monkeypatch, fake)
    out = ad.ingest_batch(_recs(HIGH, MEDIUM, PII_ONLY))
    rows = fake.memory_rows()
    content, status = _content_status_of(rows, "抑郁症")
    assert content is not None, "没抓到 high 那行的落库参数 ⇒ 判据前提失效"
    assert status == "archived", "high 档在批量通道应落 archived：%r" % status
    assert RAW not in content, (
        "🔴 **归档 ≠ 安全**：high 档隔离行的正文里仍是**原始手机号** ⇒ 明文 PII 落库：%r" % content)
    assert MASKED in content, "归档行正文应是掩码后的：%r" % content


# ── ② 负向（牙齿）：关守卫 ⇒ 明文必须重新出现 ──────────────────────────────
def test_关守卫后high归档行必须回到明文_t55(monkeypatch):
    monkeypatch.setenv("TRINITY_ADAPTER_GUARD", "0")
    fake = _FakePG()
    ad = _adapter(monkeypatch, fake)
    ad.ingest_batch(_recs(HIGH))
    content, status = _content_status_of(fake.memory_rows(), "抑郁症")
    assert content is not None
    assert RAW in content and MASKED not in content, (
        "关掉守卫后 high 正文竟然还被掩 ⇒ 上面的核心判据不是在测守卫：%r" % content)


# ── ③ 1:1 契约仍成立（当初选隔离的理由）──────────────────────────────────
def test_批量1对1契约仍成立_t55(monkeypatch):
    fake = _FakePG()
    ad = _adapter(monkeypatch, fake)
    recs = _recs(HIGH, MEDIUM, PII_ONLY)
    out = ad.ingest_batch(recs)
    rows = fake.memory_rows()
    assert len(out or []) == len(recs), "返回条数应等于送入条数（1:1）：%d vs %d" % (
        len(out or []), len(recs))
    assert len(rows) == len(recs), "落库行数应等于送入条数（1:1）：%d vs %d" % (len(rows), len(recs))
    # 三条都必须已掩（high 那行是本次修复的重点，另两条本来就掩）
    for t in (HIGH, MEDIUM, PII_ONLY):
        c, _s = _content_status_of(rows, t[:6])
        assert c is not None, "没找到样本 %r 的落库行" % t[:6]
        assert RAW not in c, "样本 %r 的落库正文仍明文：%r" % (t[:6], c)


# ── ④ 与 t49 的三开关语义表一致 ────────────────────────────────────────────
@pytest.mark.parametrize("env,expect_masked", [
    ({"TRINITY_SENSITIVE_SCAN": "off"}, False),      # 主开关：完全不扫描 ⇒ 不掩
    ({"TRINITY_SENSITIVE_REDACT": "0"}, False),      # 扫描但不掩码 ⇒ 不掩
    ({}, True),                                      # 默认 ⇒ 掩
])
def test_三开关语义与t49一致_t55(monkeypatch, env, expect_masked):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    fake = _FakePG()
    ad = _adapter(monkeypatch, fake)
    ad.ingest_batch(_recs(PII_ONLY))
    content, _s = _content_status_of(fake.memory_rows(), "队长PG冒烟")
    assert content is not None
    assert (MASKED in content) is expect_masked, (
        "开关 %r 下掩码行为与 t49 语义表不一致：%r" % (env, content))


# ── ⑤ 单条通道的 refuse 行为**未变**（不得被本次修复改动）──────────────────
def test_单条通道high仍然拒存且不落行_t55(monkeypatch):
    fake = _FakePG()
    ad = _adapter(monkeypatch, fake)
    res = ad.store_memory(content=HIGH, agent_id="t55", persona_id="t55", category="general")
    assert res.get("error"), "单条通道对 high 应拒存（返回 error）：%r" % (res,)
    assert not res.get("memory_id"), "单条通道对 high 不应给出 memory_id：%r" % (res,)
    inserts = [1 for sql, _p in fake.log if "INSERT INTO MEMORIES" in (sql or "").upper()]
    assert not inserts, "拒存却仍然执行了 INSERT ⇒ 单条语义被改坏"


# ── ⑥ 结构性：修复点必须在 `_row` 内、且用 G2 的同一掩码器（不另造策略）────
def test_修复点复用G2掩码器且不新增正则_t55():
    src = PG.read_text(encoding="utf-8")
    assert "regex_v1+adapter_guard_batch" in src, "找不到批量隔离行的账本标记（修复点不在）"
    seg = src[src.index("regex_v1+adapter_guard_batch") - 1600:]
    seg = seg[:1600]
    assert "from trinity.security.sensitive import redact_identifiers" in seg, (
        "批量补掩必须复用 G2 的 `redact_identifiers`（不得另造策略）")
    assert "re.compile" not in seg, "修复段里出现了自建正则 ⇒ 第二套策略"
