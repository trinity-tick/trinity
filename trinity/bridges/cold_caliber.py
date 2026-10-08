#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cold_caliber.py —— 冷集**口径选择**（T12，2026-10-06）。

## 缺陷（T12 复现，不是推测）

`trinity/engine_worker.py::_get_cold_set()` 直接查 `last_retrieved_at`（**PG-only 列**）。
在 SQLite 库上这条 SQL 的结果是 **0 行、不抛异常**（SQLite 适配器的读取路径把
`no such column` 吞成空结果，见 `adapters/sqlite/_connection.py:57` 的既有注释
"走错库会**静默表现为'没有结果'**"）⇒ 冷集 `ColdSet([])`，
`cold_load_status="empty"`、`cold_load_size=0`、**stderr 一行都没有**。

复现（`evidence/t12_repro_cold_sqlite.py`，真 worker 子进程，生产计数文件隔离）：

    sqlite 臂（TRINITY_STORE=~/.trinity/store/trinity_store.db，不设 backend）
        cold_load_status="empty"  cold_load_size=0  cold_sources=0  stderr 无冷集行
    pg 臂（TRINITY_STORAGE_BACKEND=postgresql，同一条 SQL 的对照组）
        cold_load_status="loaded" cold_load_size=22390 cold_sources=1

服务运行态就是 SQLite（`trinity.yaml` 第 9-12 行：本机未设 `TRINITY_STORAGE_BACKEND`
⇒ 走 SQLite 默认分支，库为 `~/.trinity/store/trinity_store.db`）
⇒ **「冷池投递」这个能力在服务配置下等于不存在，而且是静默的。**

## 本模块的处置：**要么算得对、要么响亮地失败**

| 情形 | status | 口径 | 是否响亮 |
|---|---|---|---|
| 有 `last_retrieved_at`（PG，**本机生产路径**） | `loaded` | `pg:last_retrieved_at IS NULL`（权威） | 静默通过（正常态） |
| 无该列（SQLite 等），默认 `loud` | `unavailable` | 不替代 | **计数置 `unavailable` + reason + stderr 行**（冷集仍 0 ⇒ 投递行为不变） |
| 无该列且显式 `TRINITY_COLD_CALIBER=fallback` | `loaded_fallback` | `access_count IS NULL OR =0`，**下界** | 计数带 `caliber`+`reason`（可核） |
| 列探测失败 / SQL 抛错 | `unavailable` | — | **reason + stderr 行** |
| `TRINITY_COLD_CALIBER=off` | `disabled` | — | **reason + stderr 行**（显式关闭，不是静默） |
| 口径列**确实存在**但结果为空 | `empty` | 同上 | 这是真·空（可区分于"列缺失"） |

**为什么默认是"响亮失败"而不是"自动用下界口径"**：下界口径是一处**会导致投递内容变化**的
行为选择（冷候选会真的被投出去），按"默认零行为变化 + 显式开关"的纪律应opt-in；
而"缺列却被记成 empty"是纯观测缺陷，必须默认修掉。

**为什么 `access_count==0` 可以当下界**（task 提示的可辩护信号）：
`access_count` 在写入时从 0 起算，检索/touch 路径会累加它 ⇒ `access_count=0`
意味着"自写入以来**任何 touch 路径都没碰过**"⇒ 必未被检索。
反向不成立（有些检索路径可能不累加该列 ⇒ 真实冷集**更大**）⇒ 标注为**下界**。
**不用 `last_accessed_at`**：t4 已实测它是**写入时打的**（DEFAULT NOW()），与"被读过"无关。

回滚杠杆：`TRINITY_COLD_CALIBER=strict`（只认 PG 口径，缺失即 `unavailable` 响亮失败）
或 `=off`（不加载冷集，但**响亮**记 `disabled`）。
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import logging

#: 口径门控：auto（默认，缺失即用下界口径）/ strict（只认 PG 口径）/ off（不加载）
CALIBER_GATE = "TRINITY_COLD_CALIBER"
#: PG 权威口径列
AUTHORITATIVE_COLUMN = "last_retrieved_at"
#: SQLite 下界口径列
FALLBACK_COLUMN = "access_count"

_AUTHORITATIVE_SQL = ("select memory_id from memories "
                      "where status='active' and last_retrieved_at is null")
_FALLBACK_SQL = ("select memory_id from memories "
                 "where status='active' and (access_count is null or access_count = 0)")
#: 列探测的两条方言查询（顺序由适配器类型决定，见 `detect_columns`）
_PRAGMA_COLUMNS = "select name from pragma_table_info('memories')"
_INFO_SCHEMA_COLUMNS = ("select column_name from information_schema.columns "
                        "where table_name='memories'")

_AUTHORITATIVE_CALIBER = "pg:last_retrieved_at IS NULL（权威口径）"
_FALLBACK_CALIBER = ("sqlite:access_count IS NULL OR access_count=0（**下界**："
                     "未 touch ⇒ 必未被检索；反向不成立 ⇒ 真实冷集更大）")


def gate() -> str:
    """读门控（默认 loud）。任何异常按 loud 处理（配置读不出来也不许静默空集）。"""
    try:
        v = str(os.environ.get(CALIBER_GATE, "loud")).strip().lower()
    except Exception:  # noqa: BLE001
        return "loud"
    return v if v in ("loud", "fallback", "off") else "loud"


def resolve(columns: Optional[Sequence[str]], gate_mode: Optional[str] = None,
            backend: str = "", probe_error: str = "") -> Dict[str, Any]:
    """**纯函数**：由"表里有哪些列"决定口径。返回 dict(status, sql, caliber, reason, backend)。

    三种模式（`TRINITY_COLD_CALIBER`）：
      · `loud`（**默认**）：只认权威口径列。缺列 ⇒ `unavailable` + reason
        （**响亮**、且冷集仍为 0 ⇒ **不改变任何投递行为**；下界口径要显式开）。
      · `fallback`：缺权威列时改用 `access_count` 下界口径（`loaded_fallback`，标注为下界）。
      · `off`：不加载冷集，但状态记 `disabled` + reason（显式关闭 ≠ 静默空集）。

    `columns=None` 表示**列探测失败**（不是"表里没列"）——两者必须区分：
    前者不可判定 ⇒ `unavailable`；后者按可用的列挑口径。
    """
    mode = (gate_mode or gate())
    backend = str(backend or "")
    base = {"backend": backend, "caliber": "", "reason": "", "sql": ""}
    if mode == "off":
        return {**base, "status": "disabled",
                "reason": f"{CALIBER_GATE}=off（显式关闭冷集加载；不是静默空集）"}
    cols: Optional[Set[str]] = None
    if columns is not None:
        try:
            cols = {str(c).strip().lower() for c in columns}
        except Exception:  # noqa: BLE001
            cols = None
    if cols is None:
        return {**base, "status": "unavailable",
                "reason": ("列探测失败，无法区分『库真的没有冷条目』与『口径列不存在』"
                           + (f"（probe_error={probe_error}）" if probe_error else ""))}
    if AUTHORITATIVE_COLUMN in cols:
        return {**base, "status": "loaded", "sql": _AUTHORITATIVE_SQL,
                "caliber": _AUTHORITATIVE_CALIBER}
    if FALLBACK_COLUMN in cols:
        if mode == "fallback":
            return {**base, "status": "loaded_fallback", "sql": _FALLBACK_SQL,
                    "caliber": _FALLBACK_CALIBER,
                    "reason": (f"库无 `{AUTHORITATIVE_COLUMN}` 列（backend={backend or 'unknown'}）"
                               f"⇒ 按 {CALIBER_GATE}=fallback 用下界口径 "
                               f"`{FALLBACK_COLUMN}`（冷集**不小于**真实冷集）")}
        return {**base, "status": "unavailable",
                "reason": (f"库无 `{AUTHORITATIVE_COLUMN}` 列（backend={backend or 'unknown'}）；"
                           f"{CALIBER_GATE} 默认 loud ⇒ **响亮失败**：冷集置 0 并记 unavailable，"
                           "不以『空集』冒充（要下界口径请设 "
                           f"{CALIBER_GATE}=fallback）")}
    return {**base, "status": "unavailable",
            "reason": (f"既无 `{AUTHORITATIVE_COLUMN}` 也无 `{FALLBACK_COLUMN}` 列"
                       f"（backend={backend or 'unknown'}）⇒ 无可用冷集口径；"
                       "拒绝以『空集』冒充")}


def _conn_ctx(adapter: Any):
    """方言感知的连接上下文：**复用仓内既有 helper** `trinity._tags._conn_ctx`。

    为什么不能自己写 `hasattr(adapter, "_get_conn")`（T12 高危发现，见模块头）：
    `_get_conn()` **只存在于 `PostgreSQLAdapter`（adapters/postgresql.py:269）**；
    `SQLiteAdapter` 只有 `_get_read_conn` / 裸 `_conn`。于是方言盲的 hasattr 守卫在
    SQLite 上**恒假** ⇒ 代码块被跳过 ⇒ 调用方走"空结果/空集"的正常路径
    —— **不是异常被吞，是分支根本没进**。`_tags._conn_ctx` 正是为此写的
    （其 docstring：『SQLite 分支必须真的可用，而不是静默返回空』）。
    """
    try:
        from trinity._tags import _conn_ctx as _impl
        return _impl(adapter)
    except Exception as e:  # noqa: BLE001
        return None if adapter is None else _NullCtx(adapter, e)


class _NullCtx:
    """兜底的"不可用"上下文：`with` 时抛**明确异常**，绝不静默产出空结果。"""

    def __init__(self, adapter: Any, err: Exception):
        self._adapter = adapter
        self._err = err

    def __enter__(self):
        raise RuntimeError("adapter connection unavailable: %r" % (self._err,))

    def __exit__(self, *exc) -> bool:
        return False


def detect_columns(adapter: Any) -> Tuple[Optional[Set[str]], str, str]:
    """只读探测 memories 表的列名。返回 `(columns|None, backend, error)`。

    连接获取走 `trinity._tags._conn_ctx`（方言感知：PG `_get_conn()` / SQLite 裸 `_conn`）；
    · SQLite：`select name from pragma_table_info('memories')`；
    · PostgreSQL：`information_schema.columns`。
    · 两种方言都拿不到 ⇒ `columns=None` ⇒ 上层按 `unavailable` 响亮失败（**绝不**变成空集）。
    """
    if adapter is None:
        return None, "", "adapter 为 None（无库可读）"
    backend = ""
    try:
        backend = str(getattr(adapter, "backend", "") or getattr(adapter, "name", "")
                      or type(adapter).__name__)
    except Exception:  # noqa: BLE001
        backend = type(adapter).__name__
    ctx = _conn_ctx(adapter)
    if ctx is None:
        return None, backend, "无可用连接入口（PG `_get_conn` / SQLite `_conn` 都没有）"
    err = ""
    try:
        with ctx as conn:
            if conn is None:
                return None, backend, "连接上下文返回 None"
            #: ⚠️ **方言顺序很重要**（T12 实测踩到过）：在 PG 上先跑 SQLite 的
            #: `pragma_table_info` 会抛错并**把 PG 事务标记为 aborted** ⇒ 同一连接上的
            #: 下一条查询直接 `InFailedSqlTransaction` ⇒ 列探测整体失败、PG 冷集被误判
            #: 成 unavailable（把好路径打坏）。故：① 按适配器类型决定先试哪种方言；
            #: ② 每次失败后 `rollback()` + **重建游标**，再做下一次尝试。
            name = type(adapter).__name__.lower()
            prefers_sqlite = ("sqlite" in name) or ("sqlite" in str(backend).lower())
            probes = (_PRAGMA_COLUMNS, _INFO_SCHEMA_COLUMNS) if prefers_sqlite \
                else (_INFO_SCHEMA_COLUMNS, _PRAGMA_COLUMNS)
            cols: Optional[Set[str]] = None
            for sql in probes:
                try:
                    cur = conn.cursor()
                    cur.execute(sql)
                    got = {str(r[0]).strip().lower() for r in (cur.fetchall() or []) if r}
                    if got:
                        cols = got
                        break
                except Exception as _e:  # noqa: BLE001 —— 换下一种方言继续
                    err = f"{type(_e).__name__}: {_e}"
                    try:
                        conn.rollback()          # PG 必需：清掉 aborted 事务状态
                    except Exception:            # noqa: BLE001
                        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）trinity/bridges/cold_caliber.py::detect_columns")
                    continue
            if cols is None:
                return None, backend, err or "两种方言的列探测都返回空"
            return cols, backend, ""
    except Exception as e:  # noqa: BLE001
        return None, backend, f"{type(e).__name__}: {e}"


def load_cold_ids(adapter: Any, gate_mode: Optional[str] = None,
                  now: Optional[float] = None) -> Tuple[List[str], Dict[str, Any]]:
    """按口径取冷集 id。返回 `(ids, info)`；**任何形态的失败都体现在 `info.status`**。

    纪律：
      · 口径列缺失/探测失败 ⇒ `unavailable`（**不是** empty）；
      · 口径列存在但 0 行 ⇒ `empty`（真·空，与"列缺失"可区分）；
      · SQL 抛异常 ⇒ `unavailable` + reason（响亮），**绝不当成 0 条**。
    """
    cols, backend, probe_error = detect_columns(adapter)
    info = resolve(cols, gate_mode=gate_mode, backend=backend, probe_error=probe_error)
    ids: List[str] = []
    if info["status"] not in ("loaded", "loaded_fallback"):
        return ids, info
    ctx = _conn_ctx(adapter)
    if ctx is None:
        return [], {**info, "status": "unavailable", "sql": "",
                    "reason": "无可用连接入口（PG `_get_conn` / SQLite `_conn` 都没有）"}
    try:
        with ctx as conn:
            cur = conn.cursor()
            cur.execute(info["sql"])
            ids = [str(r[0]) for r in (cur.fetchall() or []) if r and r[0]]
    except Exception as e:  # noqa: BLE001
        return [], {**info, "status": "unavailable", "sql": "",
                    "reason": (f"冷集 SQL 执行失败（{info['status']} 口径）："
                               f"{type(e).__name__}: {e}")}
    if not ids:
        # 口径列**确实存在**（resolve 已确认）⇒ 这才是真·空集，与"列缺失"不是一件事。
        return [], {**info, "status": "empty",
                    "reason": f"{info['caliber']} 命中 0 行（口径列存在 ⇒ 真·空集）"}
    return ids, info


def format_log(info: Dict[str, Any], size: int, where: str = "opening") -> str:
    """要写的**日志行**（`unavailable`/`disabled` 必须可见；正常态给一行可核摘要）。"""
    status = str(info.get("status") or "")
    if status in ("unavailable", "disabled"):
        return (f"[{where}] cold-set {status} (LOUD, cold=0): "
                f"reason={info.get('reason') or '-'} backend={info.get('backend') or '-'}")
    return (f"[{where}] cold-set {status}: size={size} caliber={info.get('caliber') or '-'} "
            f"backend={info.get('backend') or '-'}")
