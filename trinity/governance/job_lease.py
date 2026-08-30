"""治理任务租约（Job Lease）—— Codex 借鉴落地 P0-1（2026-08-21）。

背景
----
维护链任务（decay / tiers / consolidate / mirror / compact / dedup / sync /
pool-sync / session-summarize 等）由 autostart 循环、计划任务、手动调用多方
拉起，历史上出现并发实例互相抢 SQLite 写锁 → 多步写入叠加超过 busy_timeout
→ worker 卡死/60s 超时（见 dsh-ops/EXECUTION.md 已知坑 9/17）。

本模块对齐 codex state 的 job claim 语义：
- 每个 (job_kind, job_key) 一行；owner 持有租约 lease_seconds（默认 3600s）。
- acquire：无行 → 建行认领；有行且租约未过期 → **SKIP**（返回未认领，
  调用方直接跳过，绝不排队等待）；有行且租约已过期 → 接管（steal，视为
  原 owner 已死/已超时）。
- release：任务完成后写 status/detail，租约立即失效（后续可再次认领）。
- 所有操作均为短事务（BEGIN IMMEDIATE ... COMMIT），忙等上限取
  TRINITY_SQLITE_BUSY_TIMEOUT_MS（默认 15000ms，与 adapters/sqlite 一致）；
  拿不到锁（database is locked）时返回 "locked" 未认领，绝不长阻塞。

表结构（运行时权威库 ~/.trinity/store/trinity_store.db）::

    CREATE TABLE IF NOT EXISTS governance_jobs (
        job_kind         TEXT NOT NULL,
        job_key          TEXT NOT NULL DEFAULT 'global',
        owner            TEXT NOT NULL,
        lease_expires_at REAL NOT NULL,
        status           TEXT NOT NULL DEFAULT 'running',
        started_at       REAL NOT NULL,
        finished_at      REAL,
        detail           TEXT DEFAULT '',
        PRIMARY KEY (job_kind, job_key)
    );

用法
----
    from trinity.governance.job_lease import acquire, release, list_jobs

    lease = acquire("decay")
    if not lease["acquired"]:
        print("SKIP:", lease["reason"])
        return
    try:
        ...  # 执行任务
        release("decay", status="completed", detail="...")
    except Exception:
        release("decay", status="failed", detail="...")
        raise

命令行封装见 scripts/with_lease.py。
"""

from __future__ import annotations

import os
import platform
import socket
import time
import uuid
from typing import Any, Dict, List, Optional

# 2026-09 (EXECUTION 120): 租约库与运行时一致——优先 TRINITY_STORE 环境变量
# （迁移 D 盘后 ~/.trinity/store 为 C 盘残留，lease 与权威库不一致导致 SKIP 残留）
def _default_db():
    _env = os.environ.get("TRINITY_STORE", "")
    if _env:
        return os.path.join(_env, "trinity_store.db")
    return os.path.expanduser("~/.trinity/store/trinity_store.db")

DEFAULT_DB = _default_db()
DEFAULT_LEASE_SECONDS = 3600  # 对齐 codex 1h 租约

_SCHEMA = """
CREATE TABLE IF NOT EXISTS governance_jobs (
    job_kind         TEXT NOT NULL,
    job_key          TEXT NOT NULL DEFAULT 'global',
    owner            TEXT NOT NULL,
    lease_expires_at REAL NOT NULL,
    status           TEXT NOT NULL DEFAULT 'running',
    started_at       REAL NOT NULL,
    finished_at      REAL,
    detail           TEXT DEFAULT '',
    PRIMARY KEY (job_kind, job_key)
)
"""


def _busy_timeout_ms() -> int:
    try:
        return int(os.environ.get("TRINITY_SQLITE_BUSY_TIMEOUT_MS", "15000"))
    except ValueError:
        return 15000


def _connect(db_path: str):
    import sqlite3

    conn = sqlite3.connect(db_path, timeout=max(_busy_timeout_ms() // 1000, 1))
    conn.execute(f"PRAGMA busy_timeout={_busy_timeout_ms()}")
    return conn


def default_owner() -> str:
    """默认 owner 标识：hostname + pid + 随机串（跨进程唯一）。"""
    host = socket.gethostname() or platform.node() or "unknown"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def _ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


def acquire(
    job_kind: str,
    job_key: str = "global",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    owner: Optional[str] = None,
    db_path: str = DEFAULT_DB,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """认领任务租约。

    返回 dict：
      acquired: bool
      reason: "claimed" | "stolen" | "skipped" | "locked" | "error"
      owner / held_by / expires_at / held_until / previous_status / detail
    调用方仅在 acquired=True 时执行任务；否则应直接跳过（SKIP），不要等待。
    """
    t0 = time.time()
    now = now if now is not None else t0
    owner = owner or default_owner()
    result: Dict[str, Any] = {
        "acquired": False,
        "reason": "skipped",
        "job_kind": job_kind,
        "job_key": job_key,
        "owner": owner,
        "held_by": None,
        "expires_at": None,
        "held_until": None,
        "previous_status": None,
        "detail": "",
    }
    import sqlite3

    conn = None
    try:
        conn = _connect(db_path)
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT owner, lease_expires_at, status, detail FROM governance_jobs "
            "WHERE job_kind=? AND job_key=?",
            (job_kind, job_key),
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO governance_jobs "
                "(job_kind, job_key, owner, lease_expires_at, status, started_at, detail) "
                "VALUES (?,?,?,?,?,?,?)",
                (job_kind, job_key, owner, now + lease_seconds, "running", now, ""),
            )
            conn.commit()
            result.update(acquired=True, reason="claimed", expires_at=now + lease_seconds)
            return result
        held_by, held_until, status, detail = row
        if held_until > now:
            conn.rollback()
            result.update(
                reason="skipped",
                held_by=held_by,
                held_until=held_until,
                expires_at=held_until,
                previous_status=status,
                detail=detail or "",
            )
            return result
        # 租约过期 → 接管（steal）
        conn.execute(
            "UPDATE governance_jobs SET owner=?, lease_expires_at=?, status='running', "
            "started_at=?, finished_at=NULL, detail=? WHERE job_kind=? AND job_key=?",
            (owner, now + lease_seconds, now, "", job_kind, job_key),
        )
        conn.commit()
        result.update(
            acquired=True,
            reason="stolen",
            expires_at=now + lease_seconds,
            held_by=held_by,
            held_until=held_until,
            previous_status=status,
            detail=detail or "",
        )
        return result
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            result.update(reason="locked", detail=str(exc)[:200])
        else:
            result.update(reason="error", detail=str(exc)[:200])
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return result
    except Exception as exc:  # noqa: BLE001 — 任何异常都不应让调用方崩溃
        result.update(reason="error", detail=str(exc)[:200])
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def release(
    job_kind: str,
    job_key: str = "global",
    status: str = "completed",
    detail: str = "",
    db_path: str = DEFAULT_DB,
    now: Optional[float] = None,
) -> bool:
    """释放租约：status 记 completed/failed，租约立即过期。"""
    now = now if now is not None else time.time()
    import sqlite3

    conn = None
    try:
        conn = _connect(db_path)
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE governance_jobs SET status=?, finished_at=?, lease_expires_at=?, detail=? "
            "WHERE job_kind=? AND job_key=?",
            (status, now, now, (detail or "")[:500], job_kind, job_key),
        )
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def list_jobs(db_path: str = DEFAULT_DB) -> List[Dict[str, Any]]:
    """诊断用：列出全部租约行。"""
    conn = None
    try:
        conn = _connect(db_path)
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT job_kind, job_key, owner, lease_expires_at, status, "
            "started_at, finished_at, detail FROM governance_jobs ORDER BY job_kind, job_key"
        ).fetchall()
        return [
            {
                "job_kind": r[0],
                "job_key": r[1],
                "owner": r[2],
                "lease_expires_at": r[3],
                "status": r[4],
                "started_at": r[5],
                "finished_at": r[6],
                "detail": r[7],
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
