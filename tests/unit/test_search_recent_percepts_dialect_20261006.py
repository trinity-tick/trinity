# -*- coding: utf-8 -*-
"""`recent_percepts` 读库块的**方言守卫**修复闸门（2026-10-06, T3）

## 修的缺陷（`_search.py` 的 `_build_auto_situation` 内）

原实现：

    if self._adapter is not None and hasattr(self._adapter, "_get_conn"):
        import psycopg2 as _pg
        _conn = _pg.connect(host="127.0.0.1", port=5432, dbname="trinity",
                            user=..., password=...)

三件事叠在一起：

1. **守卫按 PG 的能力名硬编码**：`_get_conn` 全仓**只有 `PostgreSQLAdapter` 有**
   （`trinity/adapters/postgresql.py:269`；`SQLiteAdapter` 无此方法）⇒ SQLite 后端上
   该块**恒不执行**，"最近感知"这一能力在 SQLite 上**根本不存在**；
2. **绕开适配器直连 PG**：连接参数（host/port/dbname/user）写死在 client 代码里，
   非默认部署上必然失败（由外层 `except: swallow` 吞掉 ⇒ 静默）；
3. 因此它既不是"方言正确写法"，也不是"响亮失败"。

## 修法

复用本仓既有方言工具 `trinity._tags._conn_ctx`（`trinity/_tags.py:128`：
PG → `adapter._get_conn()`；SQLite → `contextlib.nullcontext(adapter._conn)`）⇒
**两个后端都经适配器取连接**，SQLite 上该能力由"不存在"变为"存在"（有行为差异，
故有本文件）。行数中性（`_search.py` 1400 行硬预算不放宽）。

## 前后对比（本文件即证据）

  修复前（SQLite）：`_recent_percepts` 恒为 `[]`（守卫挡住）
  修复后（SQLite）：`_recent_percepts` = 库里 `category='perception'` 的最新 2 条
"""
from __future__ import annotations

import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.environ.setdefault("TRINITY_MEMORY_ENABLED", "0")

PERSONA = "percept-dialect-persona"


def _mark_perception(store_dir: str, n: int) -> None:
    """把库内前 n 条改成 `category='perception'`。

    用**裸 sqlite3 + 显式 commit**：`sqlite3.Connection` 的上下文管理器只在异常时
    回滚、**正常路径不提交** ⇒ 用 `with sqlite3.connect(...)` 改完不 commit 会静默丢失
    （实测踩过：改完读到 0 行，误判成"修复没生效"）。
    """
    with sqlite3.connect(os.path.join(store_dir, "trinity_store.db")) as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT memory_id FROM memories WHERE status='active' LIMIT ?", (n,))]
        for mid in ids:
            conn.execute("UPDATE memories SET category='perception' WHERE memory_id=?", (mid,))
        conn.commit()


def _fresh(tmp_path, n_rows: int = 3):
    from trinity import Trinity
    store = str(tmp_path / "store")
    mem = Trinity(adapter="sqlite", store_path=store)
    for i in range(n_rows):
        mem.ingest("PERCEPT-%d: an observation captured by the perception channel" % i,
                   persona_id=PERSONA, session_id=str(i), category="general",
                   tags=["percept"])
    return mem, store


def test_sqlite_adapter_has_no_get_conn_so_the_old_guard_was_pg_only(tmp_path):
    """把**根因**钉住：`_get_conn` 是 PG 专有能力 ⇒ 按它做守卫 = PG-only 能力。

    这条不是风格断言：它解释了"为什么修复前 SQLite 上读不到"。
    """
    mem, _store = _fresh(tmp_path)
    assert not hasattr(mem._adapter, "_get_conn"), (
        "SQLiteAdapter 现在有 `_get_conn` 了 ⇒ 旧守卫的前提变了，请重新评估本文件。")


def test_recent_percepts_are_read_on_sqlite(tmp_path):
    """修复后：SQLite 上也能读到 `category='perception'` 的最新条目。

    修复前的行为：`mem._build_auto_situation()` 执行完后 `mem._recent_percepts == []`
    （守卫 `hasattr(self._adapter, "_get_conn")` 为假 ⇒ 整个块跳过）。
    """
    mem, store = _fresh(tmp_path)
    _mark_perception(store, 2)
    mem2 = mem.__class__(adapter="sqlite", store_path=store)
    sit = mem2._build_auto_situation()
    seen = getattr(mem2, "_recent_percepts", [])
    assert seen, (
        "SQLite 上 `_recent_percepts` 仍为空 —— 方言修复失效（守回 PG-only/psycopg2 直连）。")
    assert len(seen) <= 2, "缓存条数应受 [:2] 限制"
    assert any("PERCEPT-" in s for s in seen), "读到的不是刚标记的 perception 条目"
    assert any("PERCEPT-" in (sit or "") for s in [sit] if s), (
        "读过库了但没拼进 situation 字符串 ⇒ 下游仍看不到")


def test_conn_ctx_is_the_repo_canonical_helper(tmp_path):
    """修复用的是**本仓既有**工具而不是新写一套：`_conn_ctx` 对本适配器返回可用连接。"""
    from trinity._tags import _conn_ctx
    mem, _store = _fresh(tmp_path)
    got = _conn_ctx(mem._adapter)
    assert got is not None, "_conn_ctx 对本适配器返回 None（该分支会静默不读）"
    # 它返回的是**上下文管理器**（SQLite 分支是 `contextlib.nullcontext(裸连接)`；
    # PG 分支是 `_get_conn()`），只能经 `with` 取出真连接 —— 直接 `.cursor()` 必 AttributeError。
    with got as conn:
        assert conn is not None, "_conn_ctx 取出的连接是 None"
        cur = conn.cursor()
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
