"""S1 行为测试：PG 适配器的 **edge bi-temporal 时点查询** 与 **GDPR 导出/遗忘**（2026-09-23 §1274）。

配套 `tests/unit/test_pg_adapter_parity.py`（结构对等）。本文件验证**行为**：

  ① `query_relations_at` 在 PG 上按 `valid_from <= at < valid_to` 过滤（含 open-ended）；
  ② **`valid_from IS NULL` 的历史边按 `created_at` 参与判定** —— 实测 PG 的 `relations`
     表里 **17,997 / 81,749 = 22%** 的行 `valid_from` 为 NULL（该列是
     `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 补的，老行留空）。
     不 COALESCE 就会静默少返回两成边 —— 这是"实现存在但语义不等价"的形态。
  ③ `export_user_data` 返回的 content 必须是**解密后的明文**（§13.1：直读 `content` 列
     会拿到 `enc:v1:`，而"看起来一切正常"）；
  ④ `forget_user` 必须真匿名化 memories + memory_versions 并写审计。

## 本文件自身**零生产副作用**（照 `test_pg_update_tags_jsonb.py` 的房规）

真实 adapter + 真实 PG，但所有写都发生在**显式事务内**，收尾统一 `rollback()`；
`test_no_side_effect_on_production_counts` 断言前后全局计数逐字段未变。
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from contextlib import contextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
CREDS = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")


def _creds() -> dict:
    """PG 连接参数：走**仓内统一入口** `scripts/_pg_std.py`（它 merge 凭证文件的 `refs`）。

    2026-10-06（t26）：原实现 `yaml.safe_load(...)` 后取**顶层键**（并额外找一个并不存在的
    嵌套 `TRINITY_PG` 字典）—— 而 `~/.dsh/.credentials.yaml` 自 2026-09-18 起是版本化结构
    （真键**缩进在 `refs` 下**）⇒ 口令取成空串 ⇒ `_pg_available()` 恒 False ⇒
    **本文件的 PG 判据从未真正跑过**，且 skip 理由（"PG 不可用（离线/无凭证）"）与事实不符
    （本机 PG 可达、凭证就在 `refs` 下）。`_pg_std.pg_creds()` 做 `{**refs, **raw}` 且 env 优先。
    """
    _sd = os.path.join(ROOT, "scripts")
    if _sd not in sys.path:
        sys.path.insert(0, _sd)
    from _pg_std import pg_creds      # noqa: E402 —— 统一凭据入口（merge refs）

    return pg_creds()


#: 最近一次可用性探测的**真实原因**（成功则为 None）—— 供 skip 理由如实引用
_PG_ERROR = None


def _pg_available() -> bool:
    global _PG_ERROR
    if not os.path.exists(CREDS):
        _PG_ERROR = "凭证文件不存在：%s" % CREDS
        return False
    try:
        import psycopg2

        conn = psycopg2.connect(**_creds(), connect_timeout=3)
        conn.close()
        _PG_ERROR = None
        return True
    except Exception as exc:  # noqa: BLE001 —— 把真因记下来，skip 理由不许笼统
        _PG_ERROR = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return False


# 2026-10-06（t26）：理由从"PG 不可用（离线/无凭证）"改成**带上真实异常** ——
# 那条笼统理由此前掩盖了"凭证读法不对导致口令空串"这个真因。
pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="PG 不可达（真实原因：%s）" % (_PG_ERROR or "未探测"))


class _NoCommit:
    """吞掉 commit()：验证只落在事务里，最后统一 rollback。"""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


@contextmanager
def _pg_session():
    import psycopg2
    import trinity.adapters.postgresql as pgmod

    raw = psycopg2.connect(**_creds(), connect_timeout=5)
    raw.autocommit = False
    adapter = pgmod.PostgreSQLAdapter(**_creds(), auto_connect=False)
    adapter._connected = True  # 方法内的 `if not self._connected` 守卫需要它

    @contextmanager
    def _fake_conn():
        yield _NoCommit(raw)

    adapter._get_conn = _fake_conn  # type: ignore[assignment]
    try:
        yield adapter, raw
    finally:
        raw.rollback()
        raw.close()


MARK = "parity1274-" + uuid.uuid4().hex[:8]


def _counts(raw) -> tuple:
    cur = raw.cursor()
    cur.execute("SELECT count(*), count(*) FILTER (WHERE status='active') FROM memories")
    mem = cur.fetchone()
    cur.execute("SELECT count(*) FROM relations")
    rel = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM memory_versions")
    ver = cur.fetchone()[0]
    return (mem[0], mem[1], rel, ver)


def test_no_side_effect_on_production_counts():
    """本文件的判据自身无副作用：跑一圈后全局计数逐字段未变。"""
    import psycopg2

    raw = psycopg2.connect(**_creds(), connect_timeout=5)
    raw.autocommit = True
    before = _counts(raw)

    with _pg_session() as (ad, sraw):
        e1 = ad.upsert_entity(MARK + "-e1", "test", {})["id"]
        e2 = ad.upsert_entity(MARK + "-e2", "test", {})["id"]
        ad.create_relation(e1, "works_with", e2, valid_from="2026-01-01T00:00:00+00:00")

    after = _counts(raw)
    raw.close()
    assert after == before, "本测试在生产库留下了痕迹：before=%r after=%r" % (before, after)


def test_query_relations_at_filters_by_time_on_pg():
    with _pg_session() as (ad, _raw):
        e1 = ad.upsert_entity(MARK + "-a1", "test", {})["id"]
        e2 = ad.upsert_entity(MARK + "-a2", "test", {})["id"]
        e3 = ad.upsert_entity(MARK + "-a3", "test", {})["id"]
        ad.create_relation(e1, "test_pred", e2,
                           valid_from="2026-01-01T00:00:00+00:00",
                           valid_to="2026-03-01T00:00:00+00:00")
        ad.create_relation(e1, "test_pred", e3,
                           valid_from="2026-02-01T00:00:00+00:00")  # open-ended

        at_jan = [{r["object_id"]} for r in ad.query_relations_at(
            "2026-01-15T00:00:00+00:00", subject_id=e1, predicate="test_pred")]
        assert at_jan and at_jan[0] == {e2}, "2026-01-15 应只看到已闭合的第一条边"

        at_apr = [{r["object_id"]} for r in ad.query_relations_at(
            "2026-04-01T00:00:00+00:00", subject_id=e1, predicate="test_pred")]
        assert at_apr and at_apr[0] == {e3}, "2026-04-01 应只看到 open-ended 的第二条边"

        rows = ad.query_relations_at("2026-01-15T00:00:00+00:00",
                                     subject_id=e1, predicate="test_pred", limit=1)
        assert len(rows) == 1, "limit 未生效"


def test_query_relations_at_counts_legacy_null_valid_from():
    """22% 的历史边 valid_from IS NULL ⇒ 必须按 created_at 参与判定（否则静默少两成）。"""
    with _pg_session() as (ad, raw):
        e1 = ad.upsert_entity(MARK + "-n1", "test", {})["id"]
        e2 = ad.upsert_entity(MARK + "-n2", "test", {})["id"]
        cur = raw.cursor()
        cur.execute(
            "INSERT INTO relations (id, subject_id, predicate, object_id, properties,"
            " created_at, valid_from, valid_to) "
            "VALUES (%s, %s, %s, %s, '{}'::jsonb, %s::timestamptz, NULL, NULL)",
            (MARK + "-legacy", e1, "legacy_pred", e2, "2026-01-10T00:00:00+00:00"),
        )
        hit_late = ad.query_relations_at("2026-02-01T00:00:00+00:00",
                                         predicate="legacy_pred")
        assert any(r["id"] == MARK + "-legacy" for r in hit_late), (
            "valid_from IS NULL 的历史边被漏掉了 ⇒ 时点查询比 SQLite 少返回约 22% 的边"
        )
        hit_early = ad.query_relations_at("2026-01-05T00:00:00+00:00",
                                          predicate="legacy_pred")
        assert not any(r["id"] == MARK + "-legacy" for r in hit_early), (
            "created_at 之前的时点不该看到这条边（COALESCE 方向反了）"
        )


def _insert_memory(raw, persona: str, content: str) -> str:
    mid = str(uuid.uuid4())
    cur = raw.cursor()
    cur.execute(
        "INSERT INTO memories (memory_id, session_id, persona_id, agent_id, content,"
        " role, importance, category, sha256_hash, status, embedding) "
        "VALUES (%s, %s, %s, 'parity-test', %s, 'user', 0.5, 'test', %s, 'active', %s::vector)",
        (mid, str(uuid.uuid4()), persona, content, "0" * 64,
         "[" + ",".join(["0"] * 1024) + "]"),
    )
    return mid


def test_export_user_data_returns_plaintext_on_pg():
    """导出必须是明文（§13.1：直读 content 列拿到的是 enc:v1: 密文）。"""
    from trinity.security.crypto import encrypt_content

    persona = MARK + "-persona"
    secret = "PARITY1274-SECRET-" + uuid.uuid4().hex[:6]
    stored = encrypt_content(secret)
    encrypted = stored != secret  # 加密开关的状态随环境而变，两种都要覆盖

    with _pg_session() as (ad, raw):
        mid = _insert_memory(raw, persona, stored)
        out = ad.export_user_data(persona)
        assert out, "export_user_data 返回空"
        data = json.loads(out)
        assert data["persona"]["persona_id"] == persona
        assert data["memories_count"] == 1, data["memories_count"]
        got = data["memories"][0]["content"]
        assert got == secret, (
            "导出内容不是明文（拿到 %r）⇒ 密文直接外泄/失真（encrypted=%s）"
            % (str(got)[:24], encrypted)
        )
        assert not str(got).startswith("enc:v1:"), "导出内容以 enc:v1: 开头 ⇒ 未解密"

        cur = raw.cursor()
        cur.execute("SELECT count(*) FROM audit_log WHERE persona_id=%s AND action='EXPORT_USER_DATA'",
                    (persona,))
        assert cur.fetchone()[0] >= 1, "导出未留审计（GDPR 要求可追溯）"
        assert mid  # 记下 id 便于日志


def test_forget_user_anonymizes_memories_and_versions_on_pg():
    persona = MARK + "-forget"
    with _pg_session() as (ad, raw):
        mid = _insert_memory(raw, persona, "PARITY1274-FORGET-" + uuid.uuid4().hex[:6])
        cur = raw.cursor()
        # 实测（2026-09-23）：**活库** memory_versions.version_id 无默认值
        # （`_pg_schema.py:171` 声明了 DEFAULT uuid_generate_v4()，但活表是更早的
        # CREATE TABLE 建的 ⇒ IF NOT EXISTS 不会补），故这里必须显式给 id。
        cur.execute(
            "INSERT INTO memory_versions (version_id, memory_id, content, sha256_hash, operation) "
            "VALUES (%s, %s, %s, %s, 'CREATE')",
            (str(uuid.uuid4()), mid, "PARITY1274-VER-" + uuid.uuid4().hex[:6], "0" * 64),
        )

        res = ad.forget_user(persona)
        assert res.get("memories_erased") == 1, res
        assert res.get("versions_erased") == 1, res
        assert res.get("status") == "GDPR forgotten", res

        cur.execute("SELECT content, sha256_hash, status, embedding FROM memories"
                    " WHERE memory_id=%s", (mid,))
        content, sha, status, emb = cur.fetchone()
        assert content == "[GDPR erased]", content
        assert status == "gdpr_deleted", status
        # PG 侧 sha256_hash 是 NOT NULL（SQLite 侧置 NULL）⇒ 按 purge_memory 的哨兵哈希法
        assert sha == ad._compute_sha256("[GDPR erased]"), sha
        assert emb is None, "被删文本的向量仍在 HNSW 索引里 ⇒ 语义签名未真正擦除"

        cur.execute("SELECT content, sha256_hash FROM memory_versions WHERE memory_id=%s", (mid,))
        vcontent, vsha = cur.fetchone()
        assert vcontent == "[GDPR erased]", vcontent
        assert vsha == ad._compute_sha256("[GDPR erased]"), vsha

        cur.execute("SELECT count(*) FROM audit_log WHERE persona_id=%s AND action='FORGET_USER'",
                    (persona,))
        assert cur.fetchone()[0] >= 1, "遗忘未留审计"


def test_forget_user_missing_persona_is_not_an_error():
    """不存在的 persona：SQLite 侧是 0 计数 + 审计，不许抛异常（幂等语义）。"""
    with _pg_session() as (ad, _raw):
        res = ad.forget_user(MARK + "-nobody")
        assert res.get("memories_erased") == 0, res
