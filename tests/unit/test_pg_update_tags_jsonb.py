"""S1 反向测试：PG update_memory 的 tags 必须按 **jsonb** 绑定（2026-09-19 体检 839）。

## 实测缺陷

维护链 decay 任务的归档路径**每条必失败**，且失败被吞、统计谎报：

    trinity.daemon.memory_compressor: Failed to archive memory mem_9a28...:
      column "tags" is of type jsonb but expression is of type text[]
      LINE 1: ... tags = ARRAY['arc...        <-- psycopg2 把 list 自适应成 text[]
    trinity.daemon.memory_compressor: Archived 0 original memories   <-- 真实归档 0
    decay_compress: SUCCESS — summary_id=5b438624, archived=1 ...    <-- 谎报 1

调用链：dsh-ops/trinity-dsh-maintenance.ps1(decay) -> scripts/run_decay_compress.py:499
        -> trinity/daemon/memory_compressor.py:310/540 -> trinity/adapters/postgresql.py:1177

## 根因

`postgresql.py:1177-1179` 把 Python list 直接 append 进 params；psycopg2 对**非空** list
自适应为 `ARRAY[...]`（text[]），PG 无 text[]→jsonb 隐式转换 ⇒ DatatypeMismatch(42804)。
同仓 `store_memory:743` / `ingest_batch:870` / `sqlite/_crud.py:595` 都已
`json.dumps(normalize_tags(...))` —— 只有 PG 的 `update_memory` 漏了这一处。

**空 list 的静默分支**：空 list 渲染成无类型字面量 `'{}'`，被 PG 当成 jsonb **对象**吸收
（`jsonb_typeof('{}'::jsonb)='object'`）—— 不报错，但把"空数组"写成了"空对象"。

## 本文件判据（全部走**真实 adapter + 真实 PG**，事务内验证 + ROLLBACK，生产行零变更）

  ① `update_memory(tags=[...])` 不得抛异常（修复前必抛 DatatypeMismatch）；
  ② 事务内读回：`jsonb_typeof(tags)='array'` 且内容等于传入的标签（不是对象）；
  ③ 空 list 也必须落成 **array**（防"静默写成对象"分支复发）；
  ④ 断言生产行在 rollback 后**逐字段未变**（证明本测试自身无副作用）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CREDS = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")


def _creds() -> dict:
    """PG 连接参数：走**仓内统一入口** `scripts/_pg_std.py`（merge 凭证文件的 `refs`）。

    2026-10-06（t26）：原实现取**顶层键**（+ 一个并不存在的嵌套 `TRINITY_PG`），而凭证文件
    自 2026-09-18 起是版本化结构（键缩进在 `refs` 下）⇒ 口令空串 ⇒ 本文件两条 PG 判据
    从未真正跑过，skip 理由还与事实不符。详见 tests/unit/test_credentials_readers_20261006.py。
    """
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    _sd = os.path.join(ROOT, "scripts")
    if _sd not in sys.path:
        sys.path.insert(0, _sd)
    from _pg_std import pg_creds      # noqa: E402 —— 统一凭据入口（merge refs）

    return pg_creds()


#: 最近一次可用性探测的**真实原因**（成功则为 None）
_PG_ERROR = None


def _pg_available() -> bool:
    global _PG_ERROR
    if not os.path.exists(CREDS):
        _PG_ERROR = "凭证文件不存在：%s" % CREDS
        return False
    try:
        import psycopg2  # noqa: F401

        conn = psycopg2.connect(**_creds(), connect_timeout=3)
        conn.close()
        _PG_ERROR = None
        return True
    except Exception as exc:  # noqa: BLE001 —— 把真因记下来
        _PG_ERROR = "%s: %s" % (type(exc).__name__, str(exc)[:160])
        return False


pytestmark = pytest.mark.skipif(
    not _pg_available(),
    reason="PG 不可达（真实原因：%s）" % (_PG_ERROR or "未探测"))


class _NoCommit:
    """把 commit() 吞掉 —— 验证只落在事务里，最后统一 rollback。"""

    def __init__(self, conn):
        self._conn = conn

    def commit(self):
        return None

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _target_row():
    import psycopg2

    conn = psycopg2.connect(**_creds(), connect_timeout=5)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(
        "SELECT memory_id, tags::text, status, updated_at::text FROM memories "
        "WHERE status='active' ORDER BY created_at ASC LIMIT 1"
    )
    row = cur.fetchone()
    conn.close()
    return row


def test_pg_update_memory_binds_tags_as_jsonb():
    """判据①②③④：真实 adapter + 真实 PG，事务内验证，ROLLBACK 后生产行未变。"""
    import psycopg2

    before = _target_row()
    assert before, "PG 里没有 active 行可用作验证目标"
    mem_id, tags_before, status_before, updated_before = before

    cfg = _creds()
    os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")
    sys.path.insert(0, ROOT)
    import trinity.adapters.postgresql as pgmod

    raw = psycopg2.connect(**cfg)
    raw.autocommit = False
    adapter = pgmod.PostgreSQLAdapter(**cfg, auto_connect=False)

    @contextmanager
    def _fake_conn():
        yield _NoCommit(raw)

    adapter._get_conn = _fake_conn  # type: ignore[assignment]
    try:
        # ① 非空 tags：修复前这里抛 DatatypeMismatch
        try:
            adapter.update_memory(memory_id=mem_id, tags=["archived", "compressed"])
        except Exception as exc:  # pragma: no cover - 修复前走这里
            pytest.fail(
                "update_memory(tags=[...]) 抛异常 —— tags 仍按 text[] 绑定：%s: %s"
                % (type(exc).__name__, str(exc).splitlines()[0])
            )

        cur = raw.cursor()
        cur.execute("SELECT jsonb_typeof(tags), tags::text FROM memories WHERE memory_id=%s", (mem_id,))
        typeof, tags_text = cur.fetchone()
        # ② 必须是数组且内容正确
        assert typeof == "array", "tags 落成了 %s（空/非数组写法会被 PG 吸收成 jsonb 对象）" % typeof
        assert "archived" in tags_text and "compressed" in tags_text, tags_text

        # ③ 空 list 也必须是 array（治"静默写成对象"分支）
        adapter.update_memory(memory_id=mem_id, tags=[])
        cur.execute("SELECT jsonb_typeof(tags) FROM memories WHERE memory_id=%s", (mem_id,))
        assert cur.fetchone()[0] == "array", "空 tags 被写成了 jsonb 对象（应为空数组）"
    finally:
        raw.rollback()
        raw.close()

    # ④ 生产行零变更（本测试自身无副作用）
    after = _target_row()
    assert after == before, "rollback 后生产行发生变化：before=%r after=%r" % (before, after)


def test_reverse_lock_on_text_array_binding():
    """反向锁：把旧绑定渲染出来，必须确实是 text[]（证明判据针对的是真实机理，不是装饰）。"""
    import psycopg2
    from psycopg2.extensions import adapt

    rendered = adapt(["archived", "compressed"]).getquoted().decode()
    assert rendered.startswith("ARRAY["), "psycopg2 对 list 的适配方式变了：%s" % rendered
    # jsonb 列不接受 text[] 赋值：让 PG 自己做类型检查（WHERE false ⇒ 零行受影响，不改数据）
    conn = psycopg2.connect(**_creds(), connect_timeout=5)
    conn.autocommit = True
    cur = conn.cursor()
    with pytest.raises(psycopg2.errors.DatatypeMismatch):
        cur.execute("UPDATE memories SET tags = ARRAY['archived']::text[] WHERE false")
    conn.close()
