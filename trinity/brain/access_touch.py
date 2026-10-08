# -*- coding: utf-8 -*-
"""检索命中即触达（2026-09-09，访问率提升核心）

问题：search 命中不累加 access_count → ① 指标低估 ② FSRS/decay/priority_replay
读 access_count 做决策时数据不全。
本模块：检索返回后**异步批量 touch** top-k（限流 + 批量单条 SQL，不阻塞热路径）。
"""
from __future__ import annotations
import logging, os, time
from typing import Any, Dict, List, Optional, Sequence
try:
    from trinity._swallow import swallow  # L1 静默失败治理（2026-09-13）
except Exception:  # 兜底：退回原静默行为
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

logger = logging.getLogger("trinity.brain.access_touch")
_EXECUTOR = None
_CONN = None
_recent: Dict[tuple, float] = {}
THROTTLE_S = 60.0

#: T14：被**响亮拒绝**的适配器类型 → 累计批次数（可被诊断/判据读取；
#: 不允许"看起来守卫了、实际什么都没做"的中间态）。
_UNSUPPORTED: Dict[str, int] = {}


def _bg(fn, *a, **kw) -> None:
    global _EXECUTOR
    try:
        if _EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor
            _EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="access-touch")
        _EXECUTOR.submit(fn, *a, **kw)
    except Exception:  # noqa: BLE001
        swallow(__name__, None)


def _shared_conn():
    global _CONN
    try:
        if _CONN is not None and getattr(_CONN, "closed", 0) == 0:
            return _CONN
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    import psycopg2
    _CONN = psycopg2.connect(host=os.environ.get("TRINITY_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("TRINITY_PG_PORT", "5432")),
        dbname=os.environ.get("TRINITY_PG_DB", "trinity"),
        user=os.environ.get("TRINITY_PG_USER", "trinity"),
        password=os.environ.get("TRINITY_PG_PASSWORD", ""), connect_timeout=3)
    try:
        _CONN.autocommit = True
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    return _CONN


def _drop_shared_conn() -> None:
    """丢弃缓存的共享连接（出错后自愈：下次调用重建）。

    2026-09-20 §1002.4：实测失败签名是「connection pointer is NULL」——底层句柄已经没了，
    而 Python 侧对象看着还活着；出错就丢掉缓存是最省事也最可靠的自愈。
    """
    global _CONN
    try:
        if _CONN is not None:
            _CONN.close()
    except Exception:  # noqa: BLE001
        swallow(__name__, None)
    _CONN = None


def is_enabled() -> bool:
    return os.environ.get("TRINITY_ACCESS_TOUCH", "on").lower() in ("on", "1", "true", "yes")


def _throttle(ids: Sequence[str]) -> bool:
    key = tuple(sorted(ids))
    now = time.time()
    if _recent.get(key) and now - _recent[key] < THROTTLE_S:
        return True
    _recent[key] = now
    if len(_recent) > 500:
        for k in [k for k, v in _recent.items() if now - v > THROTTLE_S * 4]:
            _recent.pop(k, None)
    return False


def _dialect_native_touch(adapter: Any, ids: List[str]) -> Optional[int]:
    """用适配器**自己的** touch API 写入（方言原生）；不可用返回 None。

    ## 为什么（T14，2026-10-06）

    原实现的守卫是 `hasattr(adapter, "_get_conn")` —— 这是**方言盲**的：
    `_get_conn()` 只存在於 `PostgreSQLAdapter`，`SQLiteAdapter` 只有 `_conn` /
    `_get_read_conn` ⇒ 在 SQLite 上恒假 ⇒ 走 else 分支 `_shared_conn()`
    （**硬编码 psycopg2**）⇒ 写入被送到**另一个库**（且 `return` 的数还是正的）。

    实测（临时 SQLite 库 + 记录型假 psycopg2，见 evidence/t14_repro_before.json）：
    `_sync_touch(["mem_1"], sqlite_adapter)` 返回 **1**、SQLite 库**零变化**、
    UPDATE 语句被发往硬编码 PG 连接 ⇒ **假成功 + 写错库**。

    而两个适配器**都已经实现了方言原生的 touch**：
    `StorageAdapter.touch_memory`（base.py:142）/ `SQLiteAdapter.touch_memory`
    （_crud.py:875）/ `PostgreSQLAdapter.touch_memory`（postgresql.py:1205）
    ⇒ 能力存在，缺的只是调用方按后端名硬编码的判断。
    """
    fn = getattr(adapter, "touch_memory", None)
    if not callable(fn):
        return None
    n = 0
    for mid in ids:
        try:
            if fn(mid):
                n += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("access touch (native) failed for %s: %s", mid, str(e)[:120])
    return n


def _sync_touch(ids: List[str], adapter: Any = None) -> int:
    n = 0
    conn = None
    cm = None
    try:
        if adapter is None:
            # 无适配器（纯 PG 部署的老路径）—— 保持原样
            conn = _shared_conn()
        elif hasattr(adapter, "_get_conn"):
            # PG 适配器：保持原路径不变（T14 刻意不改活体 PG 行为）
            # 2026-09-20 §1002.4：**必须配对 __exit__**。原写法把上下文管理器写成临时对象
            # （取 CM 后直接调它的 __enter__、不留引用）⇒ CM 立刻被引用计数回收 ⇒ __exit__
            # 当场 putconn ⇒ **连接在本次调用还在用的时候就回到池子**，下一次 getconn 拿到的
            # 是同一条连接（实测：借用后空闲表立刻回到 1、两次借用 is 同一对象）
            # ⇒ 并发共用一条 PG 连接，正是日志里的 cursor already closed /
            # connection already closed / connection pointer is NULL。
            cm = adapter._get_conn()
            conn = cm.__enter__()
        else:
            # T14：既没有 `_get_conn` 也不是"无适配器" ⇒ 优先用**方言原生** API；
            # 拿不到就**响亮失败**，绝不回落到硬编码 PG 连接（那会把写入送到另一个库）。
            native = _dialect_native_touch(adapter, ids)
            if native is not None:
                return native
            _UNSUPPORTED[type(adapter).__name__] = _UNSUPPORTED.get(type(adapter).__name__, 0) + 1
            logger.error(
                "access touch UNSUPPORTED adapter %s: 既无 touch_memory 也无 _get_conn；"
                "拒绝回落到硬编码 PG 连接（会把 touch 写到另一个库）—— 本批 %d 条未写入",
                type(adapter).__name__, len(ids))
            return 0
        cur = conn.cursor()
        for mid in ids:
            cur.execute("UPDATE memories SET access_count = access_count + 1, "
                        "last_accessed_at = NOW() WHERE memory_id = %s", (mid,))
            n += cur.rowcount
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("access touch failed: %s", str(e)[:120])
        try:
            if conn is not None:
                conn.rollback()
        except Exception as _e:
            swallow(__name__, None)
        if cm is None:
            _drop_shared_conn()   # §1002.4：坏掉的共享连接不许留在缓存里
    finally:
        try:
            if cm is not None:
                cm.__exit__(None, None, None)   # §1002.4：归还池连接（与 __enter__ 配对）
        except Exception as _e:
            swallow(__name__, _e)
    return n


def touch_results(ids: Sequence[str], adapter: Any = None, background: bool = True) -> int:
    """检索命中触达：top-k 去重、限流、异步批量。"""
    if not is_enabled():
        return 0
    clean = []
    seen = set()
    for i in ids:
        s = str(i) if i is not None else ""
        if s and s not in seen:
            seen.add(s)
            clean.append(s)
        if len(clean) >= 5:
            break
    if not clean or _throttle(clean):
        return 0
    if background:
        _bg(_sync_touch, list(clean), adapter)
        return len(clean)
    return _sync_touch(clean, adapter)
