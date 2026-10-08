# -*- coding: utf-8 -*-
"""图级在线 Hebbian（2026-09-09，借鉴 HeLa-Mem ACL 2026）

与既有 trinity/brain/hebbian.py 的分工：
  - hebbian.py        ：**embedding 级**——记忆向量向查询方向漂移（权重级用进废退）
  - 本模块 hebbian_links：**图级**——一次检索里共同命中的记忆两两强化 memory_links
                        （HeLa-Mem 的共激活联想），实时生效而非等日周期

增量：strength += eta * (1 - strength)（饱和式，收敛到 1.0）；缺边即时创建。
开关：TRINITY_HEBBIAN_ONLINE=on（默认 on）；限流：同结果集 60s 一次、每次≤6 对。
"""

from __future__ import annotations

import logging
import os
import time
from itertools import combinations
from typing import Any, Dict, List, Sequence, Tuple
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:
    def swallow(site: str, exc: Any = None, *, detail: str = "") -> None:
        # 2026-09-13（659.40）：本块可能位于模块级 sys.path 操纵**之前**，
        # 此时 from trinity._swallow import 会失败 → 埋点静默退化为空操作。
        # 改为**首次调用时惰性重导入**：异常真正发生时 sys.path 早已就绪。
        try:
            from trinity._swallow import swallow as _real
            globals()["swallow"] = _real
            return _real(site, exc, detail=detail)
        except Exception:
            return None

logger = logging.getLogger("trinity.brain.hebbian_links")

ETA = 0.05
CAP = 1.0
MAX_PAIRS = 6
THROTTLE_S = 60.0
LINK_TYPE = "hebbian"
_recent: Dict[Tuple[str, ...], float] = {}
# 2026-09-09 性能修复：热路径不得阻塞——后台单线程 + 复用连接
_EXECUTOR = None
_CONN = None


def _bg_submit(fn, *a, **kw) -> None:
    """把写入丢到后台单线程执行（fire-and-forget，失败静默）。"""
    global _EXECUTOR
    try:
        if _EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hebbian")
        _EXECUTOR.submit(fn, *a, **kw)
    except Exception:  # noqa: BLE001
        swallow(__name__, None)


def _shared_conn():
    """复用一条连接（单后台线程访问，autocommit）。"""
    global _CONN
    try:
        if _CONN is not None and getattr(_CONN, "closed", 0) == 0:
            return _CONN
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    _CONN = _open_conn()
    try:
        _CONN.autocommit = True
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    return _CONN


def _drop_shared_conn() -> None:
    """丢弃缓存的共享连接（出错后自愈：下次调用重建）。

    2026-09-20 §1002.4：原实现只在 ``_CONN.closed`` 上判活，而实测的失败签名是
    「connection pointer is NULL」「connection already closed」——底层句柄已经没了，
    而 Python 侧对象看着还活着。出错就丢掉缓存，是最省事也最可靠的自愈。
    """
    global _CONN
    try:
        if _CONN is not None:
            _CONN.close()
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    _CONN = None


def _env_flag(name: str, default: str = "on") -> bool:
    return os.environ.get(name, default).lower() in ("on", "1", "true", "yes")


def is_enabled() -> bool:
    return _env_flag("TRINITY_HEBBIAN_ONLINE", "on")


def _open_conn():
    import psycopg2
    return psycopg2.connect(
        host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", ""),
        connect_timeout=3)


# ── T14：方言感知（2026-10-06）───────────────────────────────────────────
#
# 原实现用 `hasattr(adapter, "_get_conn")` 做守卫 —— **方言盲**：
# `_get_conn()` 只存在於 `PostgreSQLAdapter`，`SQLiteAdapter` 只有 `_conn`/`_get_read_conn`
# ⇒ SQLite 上恒假 ⇒ 走 else 分支 `_shared_conn()`/`_open_conn()`（**都是硬编码 psycopg2**）
# ⇒ 语句被发往**另一个库**，而返回值还是正数（假成功）。
# 实测见 evidence/t14_repro_before.json：`_record_sync` 返回 3、`decay_links` 返回 1、
# `stats` 返回 {0,0.0,0.0}，三者的 SQLite 库全部零变化、SQL 全部发往 PG 连接。
#
# 现改为：先走 `trinity._tags._conn_ctx`（**方言感知**：PG `_get_conn()` /
# SQLite `nullcontext(_conn)`），再按方言选 SQL 文本与占位符。

def _conn_ctx_local(adapter: Any):
    """方言感知的连接上下文（复用仓内既有 helper；不可用时抛**明确**异常）。"""
    from trinity._tags import _conn_ctx as _impl
    ctx = _impl(adapter)
    if ctx is None:
        raise RuntimeError(
            "hebbian-links: adapter %s 既无 _get_conn 也无 _conn —— 拒绝回落到硬编码 PG 连接"
            % type(adapter).__name__)
    return ctx


def _dialect_of(adapter: Any) -> str:
    from trinity._tags import _dialect as _impl
    return _impl(adapter)


#: 方言化 SQL 片段
_SQL = {
    "postgres": {
        "ph": "%s",
        "least": "LEAST(%s, strength + %s * (1 - strength))",
        "new_id": "md5(random()::text || clock_timestamp()::text)",
        "now": "NOW()",
    },
    "sqlite": {
        "ph": "?",
        "least": "MIN(?, strength + ? * (1 - strength))",
        "new_id": "lower(hex(randomblob(16)))",
        "now": "datetime('now')",
    },
}


def _throttled(ids: Sequence[str]) -> bool:
    key = tuple(sorted(ids))
    now = time.time()
    last = _recent.get(key)
    if last is not None and now - last < THROTTLE_S:
        return True
    _recent[key] = now
    if len(_recent) > 500:
        cutoff = now - THROTTLE_S * 4
        for k in [k for k, v in _recent.items() if v < cutoff]:
            _recent.pop(k, None)
    return False


def record_coactivation(ids: Sequence[str], adapter: Any = None,
                        eta: float = ETA, max_pairs: int = MAX_PAIRS,
                        temporal: bool = True, background: bool = True) -> int:
    """一次检索结果的共激活强化；返回强化的对数。异常一律吞掉。

    background=True（默认）：立即返回，写入丢到后台线程——**不阻塞检索热路径**。
    """
    if not is_enabled():
        return 0
    clean = [str(i) for i in ids if i][:5]
    if len(clean) < 2 or _throttled(clean):
        return 0
    if background:
        _bg_submit(_record_sync, clean, adapter, eta, max_pairs, temporal)
        return len(list(combinations(clean, 2))[:max_pairs])
    return _record_sync(clean, adapter, eta, max_pairs, temporal)


def _record_sync(clean: Sequence[str], adapter: Any = None,
                 eta: float = ETA, max_pairs: int = MAX_PAIRS,
                 temporal: bool = True) -> int:
    pairs = list(combinations(clean, 2))[:max_pairs]
    conn = None
    own = False
    cm = None
    try:
        if adapter is not None and hasattr(adapter, "_get_conn"):
            # 2026-09-20 §1002.4：**必须配对 __exit__**。原写法把上下文管理器写成临时对象
            # （取 CM 后直接调它的 __enter__、不留引用）⇒ CM 立刻被引用计数回收 ⇒ __exit__ 当场
            # putconn ⇒ **连接在本次调用还在用的时候就回到池子**，下一次 getconn 拿到的
            # 是同一条连接（实测：借用后空闲表立刻回到 1、两次借用 is 同一对象）
            # ⇒ 两个调用方并发共用一条 PG 连接，正是日志里的
            # cursor already closed / connection already closed / connection pointer is NULL。
            cm = adapter._get_conn()
            conn = cm.__enter__()
        elif adapter is not None:
            # T14：适配器存在但没有 `_get_conn`（典型：SQLiteAdapter）⇒ 走**方言感知** helper，
            # 用**适配器自己的库**，不再回落到硬编码 PG。
            cm = _conn_ctx_local(adapter)
            conn = cm.__enter__()
        else:
            conn = _shared_conn()   # 无适配器（纯 PG 老路径）：保持原样
            own = False
        dia = _dialect_of(adapter) if adapter is not None else "postgres"
        s = _SQL[dia]
        cur = conn.cursor()
        updated = 0
        # 2026-09-09 时序 Hebbian（STDP 式）：检索排名靠前者→靠后者权重更高，
        # 反向连接按 0.5 折扣（"谁先谁后"编码进图结构）。
        for a, b in pairs:
            for src, dst in ((a, b), (b, a)):
                # 前→后（检索排名靠前指向靠后）全权重；反向 0.5 折扣
                w = eta if (not temporal or (src, dst) == (a, b)) else eta * 0.5
                cur.execute(
                    "UPDATE memory_links SET strength = " + s["least"] +
                    " WHERE source_id = " + s["ph"] + " AND target_id = " + s["ph"],
                    (CAP, w, src, dst))
                if cur.rowcount == 0:
                    cur.execute(
                        "INSERT INTO memory_links (id, source_id, target_id, link_type, strength, created_at) "
                        "VALUES (" + s["new_id"] + ", " + s["ph"] + ", " + s["ph"] + ", " +
                        s["ph"] + ", " + s["ph"] + ", " + s["now"] + ")",
                        (src, dst, LINK_TYPE, eta))
            updated += 1
        # 2026-09-09 修复：借用 adapter 连接时也必须提交（否则热路径更新被回滚）
        conn.commit()
        return updated
    except Exception as e:  # noqa: BLE001
        logger.warning("hebbian-links update failed: %s", str(e)[:160])
        try:
            if conn is not None:
                conn.rollback()
        except Exception as _e:
            swallow(__name__, None)
        if cm is None:
            _drop_shared_conn()   # §1002.4：坏掉的共享连接不许留在缓存里
        return 0
    finally:
        try:
            if cm is not None:
                cm.__exit__(None, None, None)   # §1002.4：归还池连接（与 __enter__ 配对）
            elif conn is not None and own:
                conn.close()
        except Exception as _e:
            swallow(__name__, _e)


def decay_links(factor: float = 0.995, limit: int = 20000, adapter: Any = None) -> int:
    """全局衰减（日任务）：strength *= factor，防权重饱和成噪声。"""
    conn = None
    own = False
    cm = None
    try:
        if adapter is not None and hasattr(adapter, "_get_conn"):
            cm = adapter._get_conn()   # §1002.4：配对借还（原写法借出即归还）
            conn = cm.__enter__()
        elif adapter is not None:
            cm = _conn_ctx_local(adapter)     # T14：方言感知（SQLite 走裸 _conn）
            conn = cm.__enter__()
        else:
            conn = _open_conn()
            own = True
        s = _SQL[_dialect_of(adapter) if adapter is not None else "postgres"]
        cur = conn.cursor()
        cur.execute("UPDATE memory_links SET strength = strength * " + s["ph"] +
                    " WHERE id IN (SELECT id FROM memory_links ORDER BY created_at DESC LIMIT " +
                    s["ph"] + ")",
                    (float(factor), int(limit)))
        n = cur.rowcount
        # §1002.4：**借用 adapter 连接时也必须提交**。原实现是 if own: conn.commit() ——
        # 借用路径 own=False ⇒ 不提交 ⇒ 连接归还池时 putconn 回滚 ⇒ **每日衰减静默无效**
        # （与 2026-09-09 在 _record_sync 修过的是同一个坑，当时只修了那一处）。
        conn.commit()
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("hebbian-links decay failed: %s", str(e)[:160])
        return 0
    finally:
        try:
            if cm is not None:
                cm.__exit__(None, None, None)
            elif conn is not None and own:
                conn.close()
        except Exception as _e:
            swallow(__name__, _e)


def stats(adapter: Any = None) -> Dict[str, Any]:
    conn = None
    own = False
    cm = None
    try:
        if adapter is not None and hasattr(adapter, "_get_conn"):
            cm = adapter._get_conn()   # §1002.4：配对借还
            conn = cm.__enter__()
        elif adapter is not None:
            cm = _conn_ctx_local(adapter)     # T14：方言感知（SQLite 走裸 _conn）
            conn = cm.__enter__()
        else:
            conn = _open_conn()
            own = True
        s = _SQL[_dialect_of(adapter) if adapter is not None else "postgres"]
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*), COALESCE(AVG(strength),0), COALESCE(MAX(strength),0) "
                    "FROM memory_links WHERE link_type = " + s["ph"], (LINK_TYPE,))
        n, avg, mx = cur.fetchone()
        return {"hebbian_links": int(n), "avg_strength": round(float(avg), 4),
                "max_strength": round(float(mx), 4)}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}
    finally:
        try:
            if cm is not None:
                cm.__exit__(None, None, None)
            elif conn is not None and own:
                conn.close()
        except Exception as _e:
            swallow(__name__, _e)
