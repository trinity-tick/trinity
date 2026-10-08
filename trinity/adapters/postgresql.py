"""PostgreSQL storage adapter — production multi-tenant backend with connection pooling."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time  # 658.60：SAGE 快照节流需要（原缺失）
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .base import PROTECTED_ARCHIVE_CATEGORIES, StorageAdapter
from ._pg_audit import _PgAuditMixin
from ._pg_graph import _PgGraphMixin  # 2026-09-11 结构梳理：图谱一族迁出（防回胀）
from ._pg_search import _PgSearchMixin  # 2026-09-16 结构梳理：检索一族迁出（行数预算）
from ._pg_write_guard import (adapter_guard, maybe_encrypt_content,  # noqa: F401
                              zh_tsv_text)  # 727/925：守卫·写时 FTS·写端加密一族迁出（行数预算）
from ._pg_schema import INIT_SQL  # noqa: F401  (§930 DDL 迁出)
from ._pg_touch import _PgTouchMixin  # $795 写入经济学：检索命中访问统计的异步合并队列
from ._pg_gdpr import _PgGdprMixin  # §1274：GDPR 导出/遗忘（SQLite 侧早有、PG 侧曾缺）
from ._pg_conflicts import _PgConflictsMixin  # G27：冲突检测一族迁出（行数预算）
from .._tags import normalize_tags  # EXECUTION 771：tags 归一（治双重编码，写边界唯一入口）
from ..memory.l0_summary import attach as attach_l0  # $797：L0 摘要 sidecar（写路径默认开，随 INSERT 落库、零额外 WAL）
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

logger = logging.getLogger(__name__)

# ── 连接级失败的判定与有界重试（2026-09-19 遗留处理 L6；§881 按结构预算迁出）──────
# 逻辑在 trinity/adapters/_pg_conn_retry.py（本文件 2700 行预算：迁出，不上调数字）。
# 本机 PG 的 idle_session_timeout 回收池中空闲连接 ⇒ 首次 execute 抛 OperationalError:
# server closed the connection unexpectedly（重试即成功，实测 1 次）。
# 判据：tests/unit/test_pg_conn_retry_once.py（含语句级错误不得重试的反向锁）。
from ._pg_conn_retry import is_conn_error as _is_conn_error  # noqa: E402
from ._pg_conn_retry import retry_conn_once as _retry_conn_once  # noqa: E402

# 2026-09-16（EXECUTION 773）连接期 DDL 快路径开关（见 _create_tables 上方长注释）
#   TRINITY_PG_SCHEMA_FASTPATH=off  -> 恢复旧行为（每次都跑整段 DDL）
#   TRINITY_PG_DDL_LOCK_TIMEOUT     -> 真需要 DDL 时的取锁上限（默认 3s，拿不到就快速失败不排队）
SCHEMA_FASTPATH = os.environ.get("TRINITY_PG_SCHEMA_FASTPATH", "on").strip().lower() not in (
    "off", "0", "false", "no")
DDL_LOCK_TIMEOUT = os.environ.get("TRINITY_PG_DDL_LOCK_TIMEOUT", "3s")

# ── Configuration from environment ───────────────────────────────────

def _env_config() -> Dict[str, Any]:
    """Load PostgreSQL config from environment variables.

    Priority:
        1. DATABASE_URL (full DSN)
        2. Individual PG* environment variables
        3. Default values
    """
    db_url = os.environ.get("DATABASE_URL", "")
    if db_url:
        return {"url": db_url}

    cfg = {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "dbname": os.environ.get("PGDATABASE", os.environ.get("PGDBNAME", "trinity")),
        "user": os.environ.get("PGUSER", "trinity"),
        "password": os.environ.get("PGPASSWORD", "trinity"),
        "min_conn": int(os.environ.get("PG_MIN_CONN", "1")),
        "max_conn": int(os.environ.get("PG_MAX_CONN", "10")),
    }
    # 2026-09-02: 凭证自举——TRINITY_PG_* env 与 ~/.dsh/.credentials.yaml 覆盖
    # （优先级：PGHOST 等 libpq env > TRINITY_PG_* env > yaml > 默认值）
    try:
        from trinity.security.credentials import _load_yaml as _ly
        _y = _ly()
        if _y:
            cfg.update(_y)
    except Exception as _e:
        swallow(__name__, _e)
    for _k, _envk in (("host", "TRINITY_PG_HOST"), ("port", "TRINITY_PG_PORT"),
                      ("dbname", "TRINITY_PG_DB"), ("user", "TRINITY_PG_USER"),
                      ("password", "TRINITY_PG_PASSWORD")):
        _v = os.environ.get(_envk)
        if _v:
            cfg[_k] = int(_v) if _k == "port" else _v
    return cfg


# G27（t170）：_conflict_tokens 已迁到 _pg_conflicts，此处**re-export** 供既有引用方使用（勿删）
from ._pg_conflicts import _conflict_tokens as _conflict_tokens  # noqa: F401


class PostgreSQLAdapter(_PgSearchMixin, _PgGraphMixin, _PgAuditMixin, _PgGdprMixin, _PgTouchMixin, _PgConflictsMixin, StorageAdapter):
    """PostgreSQL-based storage adapter with connection pooling.

    Production backend with:
      - Connection pool (psycopg2.pool.SimpleConnectionPool)
      - Multi-tenant isolation (tenant_id)
      - Multi-persona support (persona_id)
      - Session scoping (session_id)
      - Full-text search via pg_trgm
      - Version chain for audit/provenance
      - Auto-config from environment variables

    Usage:
        # Auto-detect from environment
        adapter = PostgreSQLAdapter()

        # Manual configuration
        adapter = PostgreSQLAdapter(
            host="pg.example.com",
            port=5432,
            dbname="trinity_prod",
            user="app_user",
            password="secret",
            min_conn=5,
            max_conn=20,
        )

        adapter.connect()
        result = adapter.store_memory("Hello world")
        results = adapter.search_memories("hello")
        adapter.disconnect()
    """

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        dbname: str | None = None,
        user: str | None = None,
        password: str | None = None,
        url: str | None = None,
        min_conn: int = 1,
        max_conn: int = 10,
        auto_connect: bool = False,
    ):
        """Initialize PostgreSQL adapter.

        Args:
            host/dbname/user/password: Database connection parameters.
            url: Full DSN (overrides individual params).
            min_conn: Minimum connections in pool.
            max_conn: Maximum connections in pool.
            auto_connect: Immediately attempt connection in __init__.
        """
        env = _env_config()

        if url:
            self._url = url
            self._host = None
            self._port = None
            self._dbname = None
            self._user = None
            self._password = None
        else:
            self._url = None
            self._host = host or env["host"]
            self._port = port or env["port"]
            self._dbname = dbname or env["dbname"]
            self._user = user or env["user"]
            self._password = password or env["password"]

        self._min_conn = max(1, min_conn)
        self._max_conn = max(self._min_conn, max_conn)
        # 2026-09-11（体检 660 连接耗尽事故）：进程级连接上限的环境变量覆盖。
        # 背景：一个进程内会创建多个 PostgreSQLAdapter（engine / aggregator /
        # second_brain 等各持一个池，上限默认 10），实测评测进程 e2e_qa_eval.py
        # 单进程持有 **99 条** PG 连接，两个并发评测把 max_connections=200 打到
        # 199/200，生产 API 与维护链拿不到连接（FATAL: sorry, too many clients）。
        # 批处理/评测类进程可用 TRINITY_PG_POOL_MAX=2 把"适配器数 × 池上限"压下来；
        # 未设置时行为完全不变（默认仍是构造参数）。
        _env_pmax = os.environ.get("TRINITY_PG_POOL_MAX")
        if _env_pmax:
            try:
                self._max_conn = max(self._min_conn, int(_env_pmax))
            except (TypeError, ValueError) as _e:
                swallow(__name__, _e)
        _env_pmin = os.environ.get("TRINITY_PG_POOL_MIN")
        if _env_pmin:
            try:
                self._min_conn = max(1, min(int(_env_pmin), self._max_conn))
                self._max_conn = max(self._min_conn, self._max_conn)
            except (TypeError, ValueError) as _e:
                swallow(__name__, _e)
        self._pool = None
        self._connected = False
        self._pool_lock = threading.Lock()

        # 2026-09 WS-B T1: TRINITY_PG_POOL=off → 回退"逐调用独立连接"旧路径
        # （迁移对照/排障/回滚用；每操作开/关一次连接，无空闲连接驻留）。
        # 默认 on：走 psycopg2 SimpleConnectionPool（连接复用，写频 10x+ 收益）。
        _pool_env = os.environ.get("TRINITY_PG_POOL", "on").strip().lower()
        self._use_pool = _pool_env not in ("0", "off", "false", "no")

        # $795 写入经济学：检索命中的访问统计走异步合并队列（回滚见 _pg_touch 模块头）
        self._init_touch_queue()

        if auto_connect:
            self.connect()

    # ── Connection Management ──────────────────────────────────────

    def connect(self) -> None:
        """Initialize connection pool and create tables."""
        with self._pool_lock:
            if self._connected:
                return

            try:
                import psycopg2
                from psycopg2 import pool as pg_pool
                import psycopg2.extras
            except ImportError:
                raise ImportError(
                    "psycopg2 required for PostgreSQL adapter. "
                    "Install: pip install psycopg2-binary"
                )

            if self._use_pool:
                if self._url:
                    self._pool = pg_pool.SimpleConnectionPool(
                        self._min_conn, self._max_conn,
                        dsn=self._url,
                    )
                    logger.info(
                        "Connected to PostgreSQL via DSN (pool: %d-%d)",
                        self._min_conn, self._max_conn,
                    )
                else:
                    self._pool = pg_pool.SimpleConnectionPool(
                        self._min_conn, self._max_conn,
                        host=self._host,
                        port=self._port,
                        dbname=self._dbname,
                        user=self._user,
                        password=self._password,
                    )
                    logger.info(
                        "Connected to PostgreSQL at %s:%s/%s (pool: %d-%d)",
                        self._host, self._port, self._dbname,
                        self._min_conn, self._max_conn,
                    )
            else:
                # 2026-09 WS-B T1: TRINITY_PG_POOL=off —— 不建池，
                # _get_conn 每调用开/关独立连接（历史行为，可回滚对照）。
                logger.info(
                    "PostgreSQL pool DISABLED (TRINITY_PG_POOL=off) at "
                    "%s:%s/%s — per-call dedicated connections",
                    self._host, self._port, self._dbname,
                )

            # 2026-09 修复：先置 _connected 再建表（否则 _get_conn 在
            # _create_tables 内抛 "not connected"，schema 创建被 except 吞掉
            # —— 新库永远建不出表。旧库表为早年 SQL 迁移所建，未暴露。）
            self._connected = True
            self._create_tables()
            # $795：连上后启动 touch 合并 flush 线程（TRINITY_PG_TOUCH_SYNC=1 时不启）
            self._start_touch_flush_thread()

    @contextmanager
    def _get_conn(self) -> Iterator[Any]:
        """Get a connection (context manager).

        - TRINITY_PG_POOL=on (default): 取 SimpleConnectionPool 连接，用毕归还
          （连接复用 → 写频路径无每次新建/授权开销）。
        - TRINITY_PG_POOL=off: 每调用开/关独立连接（历史逐调行为，回滚用）。
        """
        if not self._connected:
            raise RuntimeError(
                "PostgreSQL adapter not connected. Call connect() first."
            )

        if self._use_pool:
            if not self._pool:
                raise RuntimeError(
                    "PostgreSQL adapter not connected. Call connect() first."
                )
            conn = None
            # 2026-09-10（体检 659 P2-2）：池取连接真实耗时埋点。
            # 口径：psycopg2 SimpleConnectionPool 无 wait 统计（_getconn 直接
            # pop 空闲连接或新建，无排队），故这里量的是 API 层实测的墙钟
            # "取连接"耗时——它天然涵盖池耗尽后新建连接的代价，正是
            # /metrics 的 trinity_pg_pool_wait_seconds 数据源。
            import time as _time
            import sys as _sys
            _t0 = _time.perf_counter()
            _err = False
            try:
                conn = self._pool.getconn()
            except Exception:
                _err = True
                raise
            finally:
                # 2026-09-15（P0 冷启动）：**只在本进程已加载 API 观测层时才记录**。
                # 该指标写入 `trinity.api.middleware.get_metrics()` 的**进程内**
                # 注册表，只有 API 服务端的 /metrics 读得到（_routers_health.py）；
                # 而 `from trinity.api.server._observability import note_pool_wait`
                # 会连带执行 `trinity.api.server` 包初始化 —— FastAPI + Strawberry
                # GraphQL 全栈。实测（Python314，-X importtime）：`trinity.api.server`
                # 自 1.28s / **累计 14.68s**，且发生在"取连接"这条**每连接热路径**上，
                # 于是任何用 PG 适配器的客户端/worker/脚本进程都要白付这 14.68s。
                # 客户端进程不在 sys.modules 里 ⇒ 直接跳过：本就无人可读该进程内
                # 指标，零语义损失。API 进程在包初始化时即已装载该模块（REST 路由
                # 引用 pool_wait_histogram），故 /metrics 口径**完全不变**。
                try:
                    _obs = _sys.modules.get("trinity.api.server._observability")
                    if _obs is not None:
                        _obs.note_pool_wait(_time.perf_counter() - _t0, error=_err)
                except Exception as _e:
                    swallow(__name__, _e)  # 观测路径绝不影响取连接
            bad = False
            try:
                yield conn
            except Exception as exc:  # noqa: BLE001 —— 只为判定"该不该弃掉这条连接"
                # 2026-09-19（遗留 L6）：**连接级**失败的连接不再归还池中。
                # 原实现无条件 putconn ⇒ 被 PG 回收的死连接（idle_session_timeout）放回池子，
                # 下一次 getconn() 又交给调用方 ⇒ 连环失败；现在直接 close 掉，重试拿到新连接。
                # 语句级错误（约束/类型/语法）**照旧归还**：putconn 会 rollback，连接可复用 ——
                # 这也是 tests/unit/test_pg_store_pool.py 锁定的既有契约。
                bad = _is_conn_error(exc)
                raise
            finally:
                if conn and self._pool:
                    try:
                        if bad:
                            self._pool.putconn(conn, close=True)
                        else:
                            self._pool.putconn(conn)
                    except Exception:  # noqa: BLE001 —— 归还失败不影响调用方
                        pass
            return

        # ── TRINITY_PG_POOL=off：逐调用独立连接 ────────────────────
        import psycopg2
        if self._url:
            conn = psycopg2.connect(self._url)
        else:
            conn = psycopg2.connect(
                host=self._host, port=self._port, dbname=self._dbname,
                user=self._user, password=self._password,
            )
        try:
            yield conn
        finally:
            try:
                conn.close()
            except Exception as _e:
                swallow(__name__, _e)

    def disconnect(self) -> None:
        """Close all connections in the pool."""
        # $795：关连接前 flush 残留 touch 队列（否则这批访问计数随进程消失）
        self._stop_touch_queue()
        with self._pool_lock:
            if self._pool:
                self._pool.closeall()
                self._pool = None
            self._connected = False
            logger.info("Disconnected from PostgreSQL")

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Schema Management ──────────────────────────────────────────
    #
    # 2026-09-16（EXECUTION 773）**连接期 DDL 快路径**——治锁雪崩：
    # _create_tables() 原本每次 connect() 都把 40+ 条 CREATE TABLE/INDEX IF NOT EXISTS
    # 整段跑一遍。关键点：**这些语句即使对象已存在也要拿锁**——
    #   CREATE INDEX IF NOT EXISTS  ->  SHARE 锁（与写入的 ROW EXCLUSIVE **冲突**）
    #   ALTER TABLE ... ADD COLUMN  ->  ACCESS EXCLUSIVE（连读都挡）
    # 而 PG 的锁队列是 FIFO：一旦有 DDL 在排队，**后续所有写入都会排在它后面**。
    # 有长事务/高并发写入时就会雪崩（2026-09-16 实测：CREATE INDEX 等了 531s、
    # 多个会话排队等 advisory lock 72744721、last_accessed_at 的 UPDATE 被卡 300s+）。
    #
    # 快路径：先用**只读目录查询**（AccessShareLock，与读写全兼容）确认对象齐全，
    # 齐全就整段跳过——不取 advisory 锁、不发任何 DDL、连接耗时回到毫秒级；
    # 真缺对象时才进原 DDL 路径，并加 lock_timeout 让它在拿不到锁时**快速失败**
    # 而不是排队（缺对象的场景是新建库/迁移，此时短暂让路是合理的）。
    #
    # 回滚：TRINITY_PG_SCHEMA_FASTPATH=off 恢复旧行为（逐字节等价）。
    _RE_TABLE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
    _RE_INDEX = re.compile(r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
    _RE_ADDCOL = re.compile(
        r"ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)",
        re.I)

    @classmethod
    def schema_object_names(cls, init_sql: str) -> "tuple[list, list]":
        """从建表 SQL 里抽出必须存在的对象：关系(表+索引) 与 新增列。

        用正则从 DDL 文本**现抽**（而不是另维护一份清单），避免清单与 DDL 漂移。
        """
        rels = [m.lower() for m in cls._RE_TABLE.findall(init_sql)]
        rels += [m.lower() for m in cls._RE_INDEX.findall(init_sql)]
        cols = [(t.lower(), c.lower()) for t, c in cls._RE_ADDCOL.findall(init_sql)]
        return sorted(set(rels)), cols

    @classmethod
    def schema_is_ready(cls, cur, init_sql: str) -> bool:
        """只读目录检查（AccessShareLock）：init_sql 声明的对象是否都已存在。"""
        rels, cols = cls.schema_object_names(init_sql)
        if rels:
            cur.execute(
                "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = ANY (current_schemas(false)) AND lower(c.relname) = ANY (%s)",
                (rels,))
            if int(cur.fetchone()[0]) < len(rels):
                return False
        for tbl, col in cols:
            cur.execute(
                "SELECT 1 FROM pg_attribute a "
                "JOIN pg_class c ON c.oid = a.attrelid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = ANY (current_schemas(false)) "
                "AND lower(c.relname) = %s AND lower(a.attname) = %s "
                "AND a.attnum > 0 AND NOT a.attisdropped",
                (tbl, col))
            if cur.fetchone() is None:
                return False
        return True

    def _create_tables(self) -> None:
        """Create database schema if not exists."""
        import psycopg2.extras

        init_sql = INIT_SQL  # 2026-09-20 §930：DDL 迁到 _pg_schema.INIT_SQL（行数预算）

        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # EXECUTION 773 快路径：对象齐全 -> 整段 DDL 跳过（无 advisory 锁、无 DDL、无 SHARE 锁）
                    if SCHEMA_FASTPATH:
                        try:
                            if self.schema_is_ready(cur, init_sql):
                                conn.commit()   # 结束只读事务，尽快释放快照
                                logger.debug("PostgreSQL schema already current — DDL skipped (fastpath)")
                                return
                        except Exception as _probe_err:
                            # 只读探测失败（权限/老版本 PG）不能挡住建表，退回原路径并留痕
                            conn.rollback()
                            logger.warning("schema fastpath probe failed, falling back to DDL: %s", _probe_err)
                    # 2026-09-01 (P4 修复 v2): xact 级 advisory 锁——事务结束(commit/rollback)
                    # 自动释放，杜绝"aborted 事务阻塞 unlock → 会话持锁 → 同进程自锁死"
                    # 缺陷（2026-09-01 16:1x 实测：两池连接一持锁一排队，API 启动卡死）。
                    # 多进程首次建表串行化目标不变；异常路径显式 rollback 清事务。
                    # EXECUTION 773：取锁加超时——拿不到就失败退出（对象大概率已被别的进程建好），
                    # 绝不把自己排进锁队列去堵住后续写入。
                    if str(DDL_LOCK_TIMEOUT).strip().lower() not in ("0", "off", "none", ""):
                        cur.execute("SET LOCAL lock_timeout = %s", (DDL_LOCK_TIMEOUT,))
                    cur.execute("SELECT pg_advisory_xact_lock(72744721)")
                    try:
                        # 双重检查：等锁期间别的进程可能已经把 schema 建好了
                        if SCHEMA_FASTPATH and self.schema_is_ready(cur, init_sql):
                            conn.commit()
                            logger.info("PostgreSQL schema already current (built concurrently) — DDL skipped")
                            return
                        # Split and execute each statement
                        for statement in init_sql.split(";"):
                            stmt = statement.strip()
                            if stmt:
                                cur.execute(stmt)
                        conn.commit()
                    except Exception:
                        conn.rollback()  # 清掉 aborted 事务，xact 锁随回滚自动释放
                        raise
            logger.info("PostgreSQL schema created/verified")
        except Exception as e:
            logger.warning("Schema creation issue (may already exist): %s", e)

    # ── Hashing ───────────────────────────────────────────────��────

    @staticmethod
    def _compute_sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ── CRUD Operations ────────────────────────────────────────────

    @_retry_conn_once
    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        agent_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        ttl_seconds: Optional[int] = None,
        modality: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        """写入一条记忆。

        Args:
            status: 2026 优化轮 B6 —— 调用方请求的落库状态（与 `ingest_batch` 的
                `rec["status"]` 同语义）。传 `"archived"` 时该行以
                `status='archived'` 与记忆行**同一条 INSERT** 落库。只允许下调。
        """
        import psycopg2.extras

        # 2026-10-06（复评 G4）：`agent_id` 空值归一化 —— 与 SQLite 侧同口径。
        # 空串/纯空白 ≡ 未提供（空串不携带归属信息）⇒ 'default'，与省略实参一致。
        # 动机：库里 `agent_id=''` 的行会让读侧统计产出**不可消费的空键**
        # （PowerShell 5.1 的 ConvertFrom-Json 报「参数 name 值无效」）。
        if isinstance(agent_id, str) and not agent_id.strip():
            logger.warning(
                "PG AGENT-ID-EMPTY-NORMALISED: 收到空/纯空白的 agent_id，"
                "已按「未提供」归一化为 'default'")
            agent_id = "default"

        # ── t48/G7：适配器写入边界的 **PII 守卫**（与 SQLite 侧同款，复用 G2 的策略与开关）──
        # 必须在 `_compute_sha256(content)` **之前**：否则哈希算的是**原文**、而落库的是掩码后
        # 的正文 ⇒ 行自相矛盾（SQLite 侧本来就是先掩码再算哈希，这里对齐）。
        # 也必须在下面的注入守卫**之前**：注入判定看文本语义，PII 掩码只动号码/卡号。
        _pii_iso = False
        try:
            from ._pii_guard import adapter_pii_guard

            content, metadata, _pii_g = adapter_pii_guard(content, metadata)
            if _pii_g.get("refuse"):
                return {"memory_id": "", "error": "sensitive-high refused (adapter PII guard)",
                        "severity": _pii_g.get("severity"), "policy": _pii_g.get("policy")}
            _pii_iso = bool(_pii_g.get("isolate"))
        except Exception as _e:  # noqa: BLE001 — 守卫尽力而为，绝不阻断写入
            swallow(__name__, _e)

        memory_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        if not session_id:
            session_id = str(uuid.uuid4())
        sha256_hash = self._compute_sha256(content)
        now = datetime.now(timezone.utc).isoformat()

        # H1-8 适配器层注入守卫 + 719 写时中文 FTS 文本（727 抽到 `_pg_write_guard`，见 EXECUTION 718-719）
        _status, metadata = adapter_guard(content, agent_id=agent_id, category=category,
                                          tags=tags, metadata=metadata)
        # $797 L0 摘要 sidecar：随本条 INSERT 一起落 metadata（**不是写后 UPDATE**，
        # 因此零额外 WAL——刚把写放大压下来，不能再引入新的）。默认开，
        # TRINITY_L0_SUMMARY=off 关；只对 >700 字符的非密文内容生成。
        metadata = attach_l0(content, metadata)
        _guard_isolated = _status == "archived"   # 注入守卫**自身**判定隔离
        if _pii_iso:
            # t48：PII 守卫判定的隔离 —— **单独归因**（否则审计会记成 INJECTION_ISOLATED，
            # 污染审计语义；SQLite 侧同一处注释有完整说明）
            _status = "archived"
        if str(status or "").strip().lower() == "archived":
            # 2026 优化轮 B6：调用方请求的隔离，与记忆行同一条 INSERT 落库
            # （此前 client 层"先写 active、再 archive"是两次独立 commit）
            _status = "archived"
        _tsv_zh_txt = zh_tsv_text(content)
        # 2026-09-20（§925）：**写端加密（按类目白名单，默认关）** —— 与 SQLite 适配器对称。
        # 为什么默认关：PG 关键词通道依赖明文（_pg_search.py 文件头有完整推导）。本轮同时补上了
        # "密文行客户端关键词通道"（窗口内可见）与三处 LIKE 消费方的加密兼容，因此**可以**按类目
        # 逐步放开：TRINITY_PG_ENCRYPT_CATEGORIES=perception。
        _store_content, _tsv_zh_txt = maybe_encrypt_content(content, category, _tsv_zh_txt)

        with self._get_conn() as conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO memories
                        (memory_id, session_id, persona_id, tenant_id, agent_id, content, role,
                         importance, tags, category, sha256_hash, status, version,
                         ttl_seconds, last_accessed_at, access_count, importance_score,
                         content_hash, conflict_group_id, is_resolved,
                         modality, metadata, source_uri, content_tsv_zh,
                         created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1,
                                %s, %s::timestamptz, 0, 0.0, %s, NULL, FALSE,
                                %s, %s::jsonb, %s, to_tsvector('simple', %s),
                                %s::timestamptz, %s::timestamptz)
                    """, (memory_id, session_id, persona_id, tenant_id, agent_id, _store_content, role,
                          importance, json.dumps(normalize_tags(tags), ensure_ascii=False), category, sha256_hash, _status, ttl_seconds, now,
                          sha256_hash, modality, json.dumps(metadata or {}, ensure_ascii=False),
                          source_uri, _tsv_zh_txt, now, now))

                    cur.execute("""
                        INSERT INTO memory_versions
                        (version_id, memory_id, content, sha256_hash, operation, created_at)
                        VALUES (%s, %s, %s, %s, 'CREATE', %s::timestamptz)
                    """, (version_id, memory_id, _store_content, sha256_hash, now))
                    #: G6/t109 + ⭐G10R2/t134：**在同一事务内**分配冲突组（与 SQLite 同语义）——
                    #: 位置在这一次 `conn.commit()` **之前** ⇒ 全程 **1 次提交、1 次池往返**；失败不影响写入。
                    try:
                        self._assign_conflicts(memory_id, content, conn=conn)
                    except Exception as _e_g6:  # noqa: BLE001 — 冲突检测失败**不得**影响写入
                        swallow(__name__, _e_g6)

                    conn.commit()
            except BaseException as _e:
                # 2026-09-01教训：池连接禁止残留 aborted 事务——异常先 rollback
                # 清事务（xact advisory 锁随 rollback 释放），归还池连接才是干净的。
                try:
                    conn.rollback()
                except BaseException as _e:
                    swallow(__name__, _e)
                raise

        #: G6/t109：写入成功后分配冲突组（与 SQLite 同语义；失败不影响写入）

        if _guard_isolated:
            # 被隔离的投毒写入必须留痕（审计链），否则"静默归档"不可回溯
            #
            # 2026 优化轮 B6 修复：此处原为 `_inj.get("severity")` / `_inj.get("patterns")`，
            # 而 `_inj` **从未在本文件定义**（它是 `_pg_write_guard.adapter_guard` 内
            # 的局部名，EXECUTION 727 把该段抽出到 `_pg_write_guard.py` 时把局部量
            # 一起带走了，只留下这个引用）⇒ 运行时 `NameError` ⇒ 被下面的
            # `except Exception` 静默吞掉 ⇒ **PG 的 INJECTION_ISOLATED 审计从不落库**，
            # 与该注释的承诺完全相反。判据由函数内常量证明（dis：`_inj` 为
            # LOAD_GLOBAL，且不在 `__globals__` 也不在 builtins）。
            # 现改用守卫已回写到 metadata 的扫描结果（同一来源、无需新耦合）。
            #
            # 2026-10-06（t64/I4）：**"从不落库"这一前提已过期**（B6 修好了调用与参数：
            # 假连接实测隔离路径确实执行 `INSERT INTO audit_log`，见
            # `tests/unit/test_audit_landing_and_switch_ledger_20261006.py` 的 T1）。
            # 但**"审计没落库时无人知道"这一形态仍在**，故本次只补**可观测性**（不改判定）：
            #   · 调用前 ⇒ 一条可 grep 的 `PG-INJECTION-ISOLATED-AUDIT-ATTEMPT`（**只是"尝试"**，
            #     不是成功断言 —— `_pg_audit` 内部吞异常且恒返回 None，见下方注释）；
            #   · 抛异常 ⇒ `PG-INJECTION-ISOLATED-AUDIT-FAILED`（**审计专属**告警）；
            #   · 未连接 ⇒ `PG-INJECTION-ISOLATED-AUDIT-SKIPPED`
            #     （`_pg_audit.write_audit_log` 在 `not self._connected` 时**静默 return**，
            #      这一层就是不可见的 ⇒ 在调用点显式留痕）。
            # 判定与落库行为**逐字未改**（隔离仍落 status='archived'、仍调用同一个 write_audit_log）。
            try:
                _scan = (metadata or {}).get("injection_scan") or {}
                if not getattr(self, "_connected", False):
                    logger.warning(
                        "PG-INJECTION-ISOLATED-AUDIT-SKIPPED: 适配器未连接 ⇒ 审计无法落库"
                        "（memory_id=%s severity=%s）", memory_id, _scan.get("severity"))
                else:
                    # ⚠️ 只能说 **ATTEMPT**，不能说 "AUDITED"：
                    # `_pg_audit.write_audit_log` **内部自己吞异常**（只打
                    # `WARNING trinity.adapters.pg_audit Failed to write audit log: …`）
                    # 并恒返回 None ⇒ **调用点无法得知是否真的落库**。
                    # 本轮已抓过一次"字段说假话"（G3 的 `auto_redacted`），此处不重犯：
                    # 成功/失败**都不由调用点断言**，落地事实由判据用假连接看"是否发出
                    # `INSERT INTO audit_log`"来证明（T1），失败可见性由 `_pg_audit` 的
                    # WARNING 与下面的 FAILED 告警共同保证（T2）。
                    logger.info(
                        "PG-INJECTION-ISOLATED-AUDIT-ATTEMPT: memory_id=%s severity=%s patterns=%d",
                        memory_id, _scan.get("severity"), len(_scan.get("patterns", []) or []))
                    self.write_audit_log(
                        memory_id=memory_id, action="INJECTION_ISOLATED",
                        agent_id=agent_id, persona_id=persona_id,
                        details={"severity": _scan.get("severity"),
                                 "patterns": _scan.get("patterns", []),
                                 "layer": "adapter"})
            except Exception as _e:
                # 审计专属告警（可 grep）—— 先留痕，再沿用既有的 swallow 记账；
                # **不阻断**写入（与既有降级语义一致，改的只是"可见性"）。
                logger.warning(
                    "PG-INJECTION-ISOLATED-AUDIT-FAILED: %r（审计未落库；memory_id=%s）",
                    _e, memory_id)
                swallow(__name__, _e)

        return {
            "memory_id": memory_id,
            "version_id": version_id,
            "sha256_hash": sha256_hash,
            "timestamp": now,
            "persona_id": persona_id,
            "session_id": session_id,
            "injection_isolated": _status == "archived",
        }

    @_retry_conn_once
    def ingest_batch(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批量写入记忆（高写频通道，WS-B T1，2026-09）。

        与 SQLite _BatchMixin.ingest_batch 同签名的 PG 实装：
          - 单条池连接 + 单事务 executemany + 一次性 commit（相对 N×store_memory
            省 N-1 次池往返与 N-1 次 commit ⇒ 高写频路径 10x+ 期望）。
          - 语义与 store_memory 逐条一致：memories + memory_versions 双 INSERT、
            sha256、content_tsv 由生成列/DEFAULT 计算、返回 memory_id/version_id/…。
          - 原子性：任一条失败整体 rollback 并上抛（调用方决定是否分片重试）。
        幂等/审计提示：本项目去重由 memories.content_hash 唯一索引兜底；逐条审计
        记录（write_audit_log）不属于 adapter 层，仍由调用方按既有 ingest 语义各自
        逐条落账——此处只负责两表的批量数据落盘。

        Args:
            records: store_memory 参数的 dict 列表（content/persona_id/session_id/
                tenant_id/agent_id/role/importance/tags/category/modality/metadata/
                ttl_seconds/source_uri）。
        Returns:
            List[result_dict]（顺序与 records 一致）。
        """
        if not records:
            return []

        import psycopg2.extras

        def _row(rec: Dict[str, Any]) -> Dict[str, Any]:
            content = str(rec.get("content", ""))
            # ── t48/G7：**批量通道同样下沉 PII 守卫**（与单条通道同一策略同一开关）──
            # 批量通道保留 1:1 契约（records ↔ rows），所以 high 档用**隔离**而非**拒存**：
            # 单条通道 `store_memory` 是拒存（返回 error、不落行），这里落 `status='archived'`。
            # 差别与理由写在 REDACT-ADAPTER-GUARD.md §3。
            _pii_iso = False
            try:
                from ._pii_guard import adapter_pii_guard

                _md0 = rec.get("metadata")
                content, _md1, _pii_g = adapter_pii_guard(content, _md0)
                if _pii_g.get("refuse") or _pii_g.get("isolate"):
                    # ── t55/G14：**归档 ≠ 安全**（队长 PG 冒烟实测到的真泄漏）────────────────
                    # 守卫对 refuse 档返回**未掩码**正文：单条通道靠"**根本不落库**"兜住，
                    # 而批量通道为保 1:1（records ↔ rows）要**保留那一行** ⇒ 必须**自己再掩一次**，
                    # 否则 archived 那行的 content 是**原始明文**，且可被 SQL 直接读到。
                    # 复用的是 **G2 的同一掩码器**（不另造策略、不新增正则）。
                    from trinity.security.sensitive import redact_identifiers as _redact

                    _m2, _l2 = _redact(content, cause="pii")
                    if _l2:
                        content = _m2
                        _md1 = dict(_md1 or {})
                        _md1["pii_redaction"] = {
                            "policy": "all_pii",
                            "kinds": [str(x) for x in _l2],
                            "count": len(_l2),
                            "scanner": "regex_v1+adapter_guard_batch",
                            "layer": "adapter",
                            "ts": datetime.now(timezone.utc).isoformat(),
                        }
                    _pii_iso = True
                if _md1 is not _md0:
                    rec = dict(rec, metadata=_md1)
            except Exception as _e:  # noqa: BLE001
                swallow(__name__, _e)
            now = datetime.now(timezone.utc).isoformat()
            memory_id = str(uuid.uuid4())
            version_id = str(uuid.uuid4())
            session_id = rec.get("session_id") or str(uuid.uuid4())
            sha256_hash = self._compute_sha256(content)
            # 2026-09-13（H1-8）：批量通道同样过适配器守卫（active_collector 走这里）
            _status = str(rec.get("status") or "active")
            if _pii_iso:
                _status = "archived"
            try:
                from trinity.security.injection import adapter_write_guard
                _g = adapter_write_guard(content, agent_id=rec.get("agent_id", "default"),
                                         category=rec.get("category", "general"),
                                         tags=rec.get("tags"), metadata=rec.get("metadata"))
                if _g.get("flagged"):
                    _md = dict(rec.get("metadata") or {})
                    _md["injection_scan"] = {"severity": _g.get("severity"),
                                             "patterns": _g.get("patterns", []),
                                             "layer": "adapter"}
                    rec = dict(rec, metadata=_md)
                if _g.get("isolate"):
                    _status = "archived"
            except Exception as _e:
                swallow(__name__, _e)
            return {
                "rec": rec,
                "content": content,
                "status": _status,
                "now": now,
                "memory_id": memory_id,
                "version_id": version_id,
                "session_id": session_id,
                "sha256_hash": sha256_hash,
                "persona_id": rec.get("persona_id", "default"),
                "agent_id": rec.get("agent_id", "default"),
            }

        rows = [_row(r) for r in records]

        # 2026-09-14（719）批量通道同样写中文 FTS 向量（与 store_memory 一致；开关同一枚）。
        # 719：批量通道同样写 content_tsv_zh（实测批量写入的行该列亦为 NULL）
        _zh_text = zh_tsv_text

        mem_values = []
        ver_values = []
        for r in rows:
            rec = r["rec"]
            mem_values.append((
                r["memory_id"], r["session_id"], r["persona_id"],
                rec.get("tenant_id", "default"), r["agent_id"], r["content"],
                rec.get("role", "user"), rec.get("importance", 0.5),
                json.dumps(normalize_tags(rec.get("tags")), ensure_ascii=False),
                rec.get("category", "general"), r["sha256_hash"], r["status"],
                rec.get("ttl_seconds"), r["now"], r["sha256_hash"],
                rec.get("modality", "text"),
                # $797：批写同样带 L0 摘要（与 store_memory 同一开关与口径）
                json.dumps(attach_l0(r["content"], rec.get("metadata")), ensure_ascii=False),
                rec.get("source_uri"), _zh_text(r["content"]), r["now"], r["now"],
            ))
            ver_values.append((
                r["version_id"], r["memory_id"], r["content"],
                r["sha256_hash"], r["now"],
            ))

        with self._get_conn() as conn:
            # t60/H4：**前后各数一次行数** ⇒ `rows_added` 是**实测真值**（与 t59 的 SQLite 口径一致）
            def _count_rows() -> int:
                try:
                    with conn.cursor() as _c:
                        _c.execute("SELECT count(*) FROM memories")
                        return int(_c.fetchone()[0])
                except Exception as _e:  # noqa: BLE001 — 数不出来不能让批量写失败
                    swallow(__name__, _e)
                    return -1

            _rows_before = _count_rows()
            _present = set()
            _probe_ok = False
            try:
                with conn.cursor() as cur:
                    cur.executemany("""
                        INSERT INTO memories
                        (memory_id, session_id, persona_id, tenant_id, agent_id, content, role,
                         importance, tags, category, sha256_hash, status, version,
                         ttl_seconds, last_accessed_at, access_count, importance_score,
                         content_hash, conflict_group_id, is_resolved,
                         modality, metadata, source_uri, content_tsv_zh,
                         created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1,
                                %s, %s::timestamptz, 0, 0.0, %s, NULL, FALSE,
                                %s, %s::jsonb, %s, to_tsvector('simple', %s),
                                %s::timestamptz, %s::timestamptz)
                    """, mem_values)
                    cur.executemany("""
                        INSERT INTO memory_versions
                        (version_id, memory_id, content, sha256_hash, operation, created_at)
                        VALUES (%s, %s, %s, %s, 'CREATE', %s::timestamptz)
                    """, ver_values)
                    # t60/H4：**逐条回查**这些 memory_id 是否真的落了行 —— 让 `inserted`/`deduped`
                    # 成为**实测结果**而非常量假设（本表 `content_hash` 目前**无唯一索引**、
                    # 这条 INSERT **无 ON CONFLICT** ⇒ 实测 20 送 20 行；若将来加了去重/忽略语义，
                    # 这里会**立刻**如实报成 `deduped=True`）。
                    try:
                        cur.execute("SELECT memory_id FROM memories WHERE memory_id = ANY(%s)",
                                    ([r["memory_id"] for r in rows],))
                        _present = {str(x[0]) for x in cur.fetchall()}
                        _probe_ok = True
                    except Exception as _e2:  # noqa: BLE001 — 回查失败 ⇒ 标记"未知"，**不猜**
                        swallow(__name__, _e2)
                    #: G6/t109 + ⭐G10R2/t134：批量通道**在同一事务内**逐条补冲突检测
                    #: （复用本次批量连接的 conn ⇒ **不新增池往返/提交**）
                    for _r_g6 in rows:
                        try:
                            self._assign_conflicts(_r_g6.get("memory_id"),
                                                   _r_g6.get("content") or "", conn=conn)
                        except Exception as _e_g6:  # noqa: BLE001
                            swallow(__name__, _e_g6)

                    conn.commit()
            except Exception as _e:
                try:
                    conn.rollback()
                except Exception as _e:
                    swallow(__name__, _e)
                raise
            _rows_after = _count_rows()

            #: G6/t109：批量通道同样补冲突检测（逐条；与 SQLite 同语义）



        # 2026-09-13（H1-8）：批量通道中被隔离的行必须留审计痕（否则静默归档不可回溯）
        for r in rows:
            if r.get("status") == "archived":
                try:
                    self.write_audit_log(
                        memory_id=r["memory_id"], action="INJECTION_ISOLATED",
                        agent_id=r["agent_id"], persona_id=r["persona_id"],
                        details={"layer": "adapter.batch"})
                except Exception as _e:
                    swallow(__name__, _e)

        # ── t60/H4：与 **SQLite 侧（t59）同一套字段与口径** ────────────────────────
        # 逐条：`inserted` / `deduped`（互斥；**由上面的回查实测得出**，不是常量）；
        # 表级：`sent / rows_added（实测 count 差）/ inserted_count / deduped_count /
        #        failed_count / silent_drop`，并复用 t59 的 `BatchResults`（`list` 子类 ⇒ 向后兼容）。
        _out = []
        _inserted = _deduped = _unknown = 0
        for r in rows:
            if _probe_ok:
                _ins = str(r["memory_id"]) in _present
                _ded = not _ins
            else:
                _ins = _ded = None          # 回查失败 ⇒ **诚实标"未知"**，不猜、不硬编码
            if _ins is True:
                _inserted += 1
            elif _ded is True:
                _deduped += 1
            else:
                _unknown += 1
            _out.append({
                "memory_id": r["memory_id"],
                "version_id": r["version_id"],
                "sha256_hash": r["sha256_hash"],
                "timestamp": r["now"],
                "persona_id": r["persona_id"],
                "session_id": r["session_id"],
                "injection_isolated": r.get("status") == "archived",
                "inserted": _ins,
                "deduped": _ded,
            })
        try:
            from .sqlite._batch import BatchResults as _BatchResults   # t59 的同一实现（单一来源）
        except Exception as _e:  # noqa: BLE001 — 兜底：**记警告**而不是静默降级
            logger.warning("PG batch: 无法复用 BatchResults（%r）⇒ 本次返回裸 list（无表级计数）", _e)
            return _out
        _rows_added = (_rows_after - _rows_before) if (_rows_before >= 0 and _rows_after >= 0) else -1
        return _BatchResults(
            _out,
            sent=len(rows),
            rows_added=_rows_added,
            inserted_count=_inserted,
            deduped_count=_deduped,
            failed_count=_unknown,
        )


    # 2026-09-02: 读取路径统一解密（与 SQLiteAdapter 对齐）。PG 混存 SQLite 同步来的
    # enc:v1 密文行与本地明文行；fail-open：非密文/无密钥/解密失败原样返回。
    @staticmethod
    def _decrypt_content(content: Any) -> Any:
        from trinity.security.crypto import decrypt_content
        return decrypt_content(content)

    # 2026-09-20（§925）：写端加密的实现在 _pg_write_guard.maybe_encrypt_content
    # （与 727 抽 adapter_guard 同理：postgresql.py 有 2700 行预算，实测一加就 2715 超限）。


    def get_embedding(self, memory_id: str):
        """读取单条记忆的 embedding（Hebbian 强化用）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT embedding FROM memories WHERE memory_id = %s", (memory_id,))
                    row = cur.fetchone()
                    if row and row[0] is not None:
                        _raw = row[0]
                        if isinstance(_raw, str):
                            import ast as _ast
                            _raw = _ast.literal_eval(_raw)
                        return [float(x) for x in _raw]
            return None
        except Exception:
            return None

    def set_embedding(self, memory_id: str, query_vec: Any) -> bool:
        """写入单条记忆的 pgvector embedding（回填/增量用）。"""
        import psycopg2.extras
        try:
            import numpy as _np
            vec = _np.asarray(query_vec, dtype=_np.float32).reshape(-1)
        except Exception:
            vec = list(query_vec)
        vec_str = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET embedding = %s::vector, updated_at = NOW() WHERE memory_id = %s",
                    (vec_str, memory_id),
                )
                conn.commit()
                return cur.rowcount > 0

    def refresh_content_indexes(self, memory_id: str, query_vec: Any = None,
                                tsv_zh: str = "") -> bool:
        """内容更新后刷新检索索引（EXECUTION 596: update_memory 此前不改
        embedding/content_tsv_zh → 更新后检索陈旧）。embedding 与中文分词向量
        一次提交；不提供则保留原值。"""
        sets = []
        params: list = []
        if query_vec is not None:
            try:
                import numpy as _np
                vec = _np.asarray(query_vec, dtype=_np.float32).reshape(-1)
            except Exception:
                vec = list(query_vec)
            vec_str = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"
            sets.append("embedding = %s::vector")
            params.append(vec_str)
        if tsv_zh:
            sets.append("content_tsv_zh = to_tsvector('simple', %s)")
            params.append(tsv_zh)
        if not sets:
            return False
        sets.append("updated_at = NOW()")
        params.append(memory_id)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET %s WHERE memory_id = %%s" % ", ".join(sets),
                    params)
                conn.commit()
                return cur.rowcount > 0

    def count_embeddings(self) -> int:
        """已回填向量条数（回填进度监控用）。"""
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM memories WHERE embedding IS NOT NULL")
                return int(cur.fetchone()[0])

    def get_memories_missing_embedding(self, limit: int = 500) -> List[Dict[str, Any]]:
        """分批取未回填向量记忆（回填脚本用）。"""
        import psycopg2.extras
        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT memory_id, content FROM memories WHERE embedding IS NULL ORDER BY created_at LIMIT %s",
                    (limit,),
                )
                return [{"memory_id": str(r["memory_id"]), "content": self._decrypt_content(r["content"])} for r in cur.fetchall()]

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM memories WHERE memory_id = %s", (memory_id,))
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                if d.get("content"):
                    d["content"] = self._decrypt_content(d["content"])
                return d

    def get_memory_owners(self, memory_ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """批量查询记忆的归属与状态（hybrid 检索隔离后过滤用；与 SQLiteAdapter 同接口）。"""
        if not memory_ids:
            return {}
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT memory_id, status, agent_id, persona_id, tenant_id "
                    "FROM memories WHERE memory_id::text = ANY(%s)",
                    ([str(m) for m in memory_ids],),
                )
                return {
                    str(r["memory_id"]): {
                        "status": r["status"],
                        "agent_id": r["agent_id"],
                        "persona_id": r["persona_id"],
                        "tenant_id": r["tenant_id"],
                    }
                    for r in cur.fetchall()
                }

    def get_persona_memories(self, persona_id: str, agent_id: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                if agent_id:
                    cur.execute("""
                        SELECT * FROM memories
                        WHERE persona_id = %s AND agent_id = %s AND status = 'active'
                        ORDER BY created_at DESC LIMIT %s
                    """, (persona_id, agent_id, limit))
                else:
                    cur.execute("""
                        SELECT * FROM memories
                        WHERE persona_id = %s AND status = 'active'
                        ORDER BY created_at DESC LIMIT %s
                    """, (persona_id, limit))
                return [dict(row) for row in cur.fetchall()]

    def delete_memory(self, memory_id: str) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET status = 'deleted', updated_at = NOW() WHERE memory_id = %s",
                    (memory_id,)
                )
                conn.commit()
                return cur.rowcount > 0

    def purge_memory(self, memory_id: str, reason: str = "") -> Dict[str, Any]:
        """GDPR 硬擦除（2026-09-02, Fable 对照审计 P2-⑤⑦）。覆写销毁+行保留。"""
        # 2026 优化轮 B6：本方法此前**必抛 NameError** —— `psycopg2` 未在本作用域
        # 绑定（本文件按方法惰性导入，本方法漏了），且此处无 try 保护，故 GDPR
        # 硬擦除整体不可用。由 tests/unit/test_b6_commit_boundary.py::
        # TestUndefinedGlobalGuard 静态 + 功能双重复验。
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "SELECT memory_id, persona_id, sha256_hash, status"
                    " FROM memories WHERE memory_id::text = %s",
                    (str(memory_id),),
                )
                row = cur.fetchone()
                if not row:
                    return {"memory_id": memory_id, "purged": False, "error": "not_found"}
                prior_hash = row["sha256_hash"]
                persona_id = row["persona_id"]
                now = datetime.now(timezone.utc).isoformat()
                sentinel = "[HARD_PURGED %s] %s" % (now, memory_id)
                meta = json.dumps({"hard_purged": True, "purged_at": now,
                                   "reason": (reason or "")[:200]}, ensure_ascii=False)
                sent_hash = self._compute_sha256(sentinel)
                cur.execute(
                    "UPDATE memories SET content = %s, status = 'gdpr_deleted',"
                    " sha256_hash = %s, embedding = NULL, importance = 0,"
                    " metadata = metadata || %s::jsonb, updated_at = NOW()"
                    " WHERE memory_id::text = %s",
                    (sentinel, sent_hash, meta, str(memory_id)),
                )
                cur.execute(
                    "UPDATE memory_versions SET content = %s WHERE memory_id::text = %s",
                    (sentinel, str(memory_id)),
                )
                try:
                    cur.execute(
                        "DELETE FROM memory_links WHERE memory_id::text = %s"
                        " OR from_memory_id::text = %s OR to_memory_id::text = %s",
                        (str(memory_id), str(memory_id), str(memory_id)),
                    )
                except Exception as _e:
                    swallow(__name__, _e)
                conn.commit()
            return {"memory_id": str(memory_id), "purged": True,
                    "prior_sha256": prior_hash, "status": "gdpr_deleted",
                    "persona_id": persona_id}

    @_retry_conn_once
    def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Update an existing memory with version tracking."""
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                # Get current memory
                cur.execute("SELECT * FROM memories WHERE memory_id = %s", (memory_id,))
                current = cur.fetchone()
                if not current:
                    return None

                now = datetime.now(timezone.utc).isoformat()
                version_id = str(uuid.uuid4())

                # Build updates
                updates = ["updated_at = %s::timestamptz"]
                params = [now]

                if content is not None:
                    updates.append("content = %s")
                    params.append(content)
                    updates.append("sha256_hash = %s")
                    params.append(self._compute_sha256(content))
                    updates.append("version = version + 1")

                if importance is not None:
                    updates.append("importance = %s")
                    params.append(importance)

                if tags is not None:
                    # 2026-09-19（体检 839）：**必须按 jsonb 绑定**。原写法把 Python list 直接
                    # 交给 psycopg2 ⇒ 非空 list 自适应为 ARRAY[...](text[]) ⇒ PG 无 text[]→jsonb
                    # 隐式转换 ⇒ DatatypeMismatch(42804)：decay 归档路径**每条必失败**且被吞，
                    # 原文永远 active、统计谎报 archived=N。空 list 更隐蔽——渲染成无类型字面量
                    # '{}' 被当 jsonb **对象**吸收（静默错写成对象而非空数组）。
                    # 同 store_memory(:743) / ingest_batch(:870) / sqlite/_crud.py(:595) 一致写法。
                    updates.append("tags = %s::jsonb")
                    params.append(json.dumps(normalize_tags(tags), ensure_ascii=False))

                if category is not None:
                    updates.append("category = %s")
                    params.append(category)

                params.append(memory_id)

                update_sql = f"UPDATE memories SET {', '.join(updates)} WHERE memory_id = %s"
                cur.execute(update_sql, params)

                # Version trail
                if content is not None:
                    new_content = content
                    cur.execute("""
                        INSERT INTO memory_versions
                        (version_id, memory_id, content, sha256_hash, operation, created_at)
                        VALUES (%s, %s, %s, %s, 'UPDATE', %s::timestamptz)
                    """, (version_id, memory_id, new_content, self._compute_sha256(new_content), now))
                    # EXECUTION 638: 内容更新=旧有效区间闭合（valid_to=本次更新时刻; 幂等仅闭未闭区间）
                    cur.execute("UPDATE memories SET valid_to = %s::timestamptz "
                                "WHERE memory_id = %s AND valid_to IS NULL", (now, memory_id))

                conn.commit()

                # Return updated memory
                cur.execute("SELECT * FROM memories WHERE memory_id = %s", (memory_id,))
                row = cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                if d.get("content"):
                    d["content"] = self._decrypt_content(d["content"])
                return d

    def update_importance(
        self,
        memory_id: str,
        importance: float,
        importance_score: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """轻量价值回写（EXECUTION 612，H2 修复）：importance/importance_score/
        metadata(jsonb merge) 更新，不动 content/version 链。经当前 adapter 执行，
        替代 _deep_value 硬编码直连段（源码凭据移除，隔离后端不再隐性连库）。

        Returns:
            True 若行存在并更新；False 未找到。
        """
        if not memory_id:
            return False
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        score = (float(importance_score)
                 if importance_score is not None else float(importance))
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE memories
                    SET importance = %s,
                        importance_score = %s,
                        metadata = CASE
                            WHEN jsonb_typeof(metadata) = 'object' THEN metadata || %s::jsonb
                            ELSE '{}'::jsonb || %s::jsonb
                        END,
                        updated_at = NOW()
                    WHERE memory_id = %s
                """, (float(importance), score, meta_json, meta_json, memory_id))
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    def set_content_tsv_zh(self, memory_id: str, zh_words_text: str) -> bool:
        """中文 FTS tsvector 回填（EXECUTION 131/612 H2）：经当前 adapter 执行，
        替代硬编码直连段（源码凭据移除）。
        """
        if not memory_id:
            return False
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE memories SET content_tsv_zh = to_tsvector('simple', %s) "
                    "WHERE memory_id = %s",
                    (zh_words_text, memory_id))
                updated = cur.rowcount > 0
            conn.commit()
        return updated

    # ── Version Chain ─────────────────────────────────────────────

    @_retry_conn_once
    def archive_memories(self, memory_ids: List[str]) -> int:
        """批量将记忆标记为 archived（衰减压缩回写；与 SQLiteAdapter 同接口）。

        镜像 memory_compressor._archive_originals 的历史裸 SQL 行为。

        2026-09-29（判据接线 · C1）：**豁免 PROTECTED_ARCHIVE_CATEGORIES**。
        本方法是全仓归档的收口（decay / tiers / forgetting / compressor 都走它），
        故豁免加在这里一处即可覆盖全部调用方。被豁免的行 rowcount=0 ⇒ 上层
        HALF-ARCHIVE-GUARD 会正确地"跳过 tags 写入"，不会留下半归档行。
        """
        if not memory_ids:
            return 0
        count = 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for mid in memory_ids:
                    cur.execute(
                        "UPDATE memories SET status = 'archived', "
                        "updated_at = NOW() WHERE memory_id::text = %s "
                        "AND COALESCE(category,'') <> ALL(%s)",
                        (str(mid), list(PROTECTED_ARCHIVE_CATEGORIES)),
                    )
                    count += cur.rowcount
            conn.commit()
        return count

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT * FROM memory_versions
                    WHERE memory_id = %s ORDER BY created_at ASC
                """, (memory_id,))
                out = []
                for row in cur.fetchall():
                    d = dict(row)
                    if d.get("content"):
                        d["content"] = self._decrypt_content(d["content"])
                    out.append(d)
                return out

    def get_all_memories(self, agent_id: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                if agent_id:
                    cur.execute("""
                        SELECT * FROM memories
                        WHERE status = 'active' AND agent_id = %s
                        ORDER BY created_at DESC LIMIT %s
                    """, (agent_id, limit))
                else:
                    cur.execute("""
                        SELECT * FROM memories
                        WHERE status = 'active'
                        ORDER BY created_at DESC LIMIT %s
                    """, (limit,))
                out = []
                for row in cur.fetchall():
                    d = dict(row)
                    if d.get("content"):
                        d["content"] = self._decrypt_content(d["content"])
                    out.append(d)
                return out

    #: 默认上限 = **安全护栏**，不是截断政策（§809，2026-09-18）。
    #: 原默认 10000 曾配合 ORDER BY created_at DESC **按 recency 截断**：
    #: active 24,965 时只索引最新 40.1%，**14,965 条对 BM25 通道不存在**，
    #: 且被丢掉的那批 avg_importance 更高（0.597 vs 0.546）、累计访问是保留集的 2.4 倍
    #: ⇒ 等于把耐用知识（decision / knowledge / wms_knowledge）换成 5,384 条 perception。
    #: 实测把上限放到覆盖全量只多花 **fetch +0.27s / build +0.9s**，且构建发生在
    #: **启动期后台线程**（不在查询路径）⇒ 原上限买到的东西≈0。
    def get_index_documents(self, limit: int = 200000) -> List[Tuple[str, str]]:
        """检索索引专用**精简取数**：只取 (memory_id, content) 两列并解密。

        2026-09-15（R41-P23，冷启动 P0）：BM25 倒排索引构建只需要这两列，而
        `get_all_memories` 走的是 `SELECT *`（本表实测 **41 列**）+ DictCursor
        逐行建字典（1 万行 ≈ 42 万次 `DictRow.__setitem__`）⇒ 实测 **3.35s**，
        其中绝大部分花在取回并构造**用不到**的列上。

        本方法用普通 tuple 游标只取两列：实测 **0.34s（10.0x）**；且两种口径
        取到的 `(memory_id, content)` 经 1 万行**逐字比对完全一致**
        （temp/_prof10_lean_fetch.py）⇒ 对索引内容是**语义等价**替换。

        口径一致性：排序与 LIMIT 与 `get_all_memories` 保持相同
        （`status='active' ORDER BY created_at DESC`），确保取到**同一批**文档。
        仅供索引构建等只读消费方使用；需要完整字段请勿用本方法。
        """
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT memory_id, content FROM memories "
                    "WHERE status = 'active' ORDER BY created_at DESC LIMIT %s",
                    (limit,))
                return [(str(_mid), self._decrypt_content(_txt) if _txt else "")
                        for _mid, _txt in cur.fetchall()]

    # ── TTL & 自动老化 ────────────────────────────────────────────

    def touch_memory(self, memory_id: str) -> bool:
        """更新指定记忆的 last_accessed_at 和 access_count。"""
        import psycopg2.extras
        if not self._connected:
            return False

        now = datetime.now(timezone.utc).isoformat()
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE memories
                        SET last_accessed_at = %s::timestamptz,
                            last_retrieved_at = %s::timestamptz,
                            access_count = access_count + 1,
                            updated_at = %s::timestamptz
                        WHERE memory_id = %s
                    """, (now, now, now, memory_id))
                    conn.commit()
                    return cur.rowcount > 0
        except Exception:
            return False

    def age_memories(self) -> Dict[str, Any]:
        """手动触发老化扫描，清理 TTL 过期的记忆（软删除）。

        Returns:
            Dict with aged_count and details.
        """
        import psycopg2.extras
        if not self._connected:
            return {"aged_count": 0, "error": "Not connected"}

        now = datetime.now(timezone.utc)
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("""
                        SELECT memory_id FROM memories
                        WHERE status = 'active'
                          AND ttl_seconds IS NOT NULL
                          AND created_at IS NOT NULL
                          AND created_at + (ttl_seconds || ' seconds')::INTERVAL < %s
                    """, (now,))
                    expired_ids = [row["memory_id"] for row in cur.fetchall()]

                    if not expired_ids:
                        return {"aged_count": 0, "timestamp": now.isoformat()}

                    cur.execute("""
                        UPDATE memories
                        SET status = 'expired', updated_at = %s
                        WHERE memory_id = ANY(%s)
                    """, (now, expired_ids))
                    conn.commit()

                return {
                    "aged_count": len(expired_ids),
                    "timestamp": now.isoformat(),
                    "expired_ids": [str(mid) for mid in expired_ids],
                }
        except Exception as e:
            return {"aged_count": 0, "error": str(e)}

    def get_memory_stats(self) -> Dict[str, Any]:
        """返回记忆统计信息（总数、过期数、Agent 分布、平均访问频率等）。"""
        import psycopg2.extras
        if not self._connected:
            return {"error": "Not connected"}

        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'active'")
                    active = cur.fetchone()["c"]

                    cur.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'expired'")
                    expired = cur.fetchone()["c"]

                    cur.execute("SELECT COUNT(*) as c FROM memories")
                    total = cur.fetchone()["c"]

                    now = datetime.now(timezone.utc)
                    cur.execute("""
                        SELECT COUNT(*) as c FROM memories
                        WHERE status = 'active'
                          AND ttl_seconds IS NOT NULL
                          AND created_at + (ttl_seconds || ' seconds')::INTERVAL < %s
                    """, (now,))
                    due_expired = cur.fetchone()["c"]

                    # 2026-10-06：与 SQLite 适配器同口径的分组键归一化（详见
                    # trinity/adapters/sqlite/_stats.py 同处注释）：空串/ NULL 的
                    # agent_id 会让 JSON 出现 `""` / `"null"` 键，PowerShell 5.1
                    # 的 Invoke-RestMethod / ConvertFrom-Json 无法消费该端点。
                    # 非空键不动；空串（实测存在）与 NULL 合并为 '(unassigned)' 保留计数。
                    cur.execute("""
                        SELECT COALESCE(NULLIF(agent_id, ''), '(unassigned)') AS agent_id,
                               COUNT(*) as cnt FROM memories
                        WHERE status = 'active'
                        GROUP BY COALESCE(NULLIF(agent_id, ''), '(unassigned)')
                        ORDER BY cnt DESC
                    """)
                    agent_distribution = {row["agent_id"]: row["cnt"] for row in cur.fetchall()}

                    cur.execute("""
                        SELECT AVG(access_count) as avg_access FROM memories WHERE status = 'active'
                    """)
                    avg_access = cur.fetchone()["avg_access"] or 0

                return {
                    "total_memories": total,
                    "active_memories": active,
                    "expired_memories": expired,
                    "due_expired": due_expired,
                    "agent_distribution": agent_distribution,
                    "avg_access_count": round(float(avg_access), 2),
                }
        except Exception as e:
            return {"error": str(e)}

    def get_modality_stats(self) -> Dict[str, Any]:
        """返回各模态记忆数量、存储占比统计。"""
        try:
            import psycopg2.extras
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT COUNT(*) as c FROM memories WHERE status = 'active'")
                    total = cur.fetchone()["c"]

                    # 2026-10-06（复评 G3）：与 SQLite 侧同口径 —— 空串/NULL 键会让
                    # PowerShell 5.1 的 ConvertFrom-Json 报「参数 name 值无效」。
                    # 改法与 agent_distribution 一致（GROUP BY 用完整表达式而非别名）。
                    cur.execute("""
                        SELECT COALESCE(NULLIF(modality, ''), '(unassigned)') AS modality,
                               COUNT(*) as cnt
                        FROM memories
                        WHERE status = 'active'
                        GROUP BY COALESCE(NULLIF(modality, ''), '(unassigned)')
                        ORDER BY cnt DESC
                    """)
                    distribution = {row["modality"]: row["cnt"] for row in cur.fetchall()}

                return {
                    "total_active": total,
                    "modalities": distribution,
                    "percentages": {
                        m: round(c / total * 100, 2) if total > 0 else 0.0
                        for m, c in distribution.items()
                    },
                }
        except Exception as e:
            return {"error": str(e)}

    # ── 去重与冲突解决 ─────────────────────────────────────────────

    def check_content_hash_collision(
        self, persona_id: str, agent_id: str, content_hash: str
    ) -> Optional[Dict[str, Any]]:
        """检查同一 persona+agent 下是否已存在相同 content_hash 的记忆。"""
        # 2026 优化轮 B6：此前 `psycopg2` 未绑定 ⇒ NameError ⇒ 被下方
        # `except Exception: return None` 吞掉 ⇒ 本方法**恒返回"无冲突"**
        # （探测永远失效且无人知晓，属 GUARDS.md G3「被吞异常」同型）。
        import psycopg2.extras

        if not self._connected:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("""
                        SELECT memory_id, content, conflict_group_id, is_resolved,
                               created_at, status
                        FROM memories
                        WHERE persona_id = %s AND agent_id = %s
                          AND content_hash = %s AND status = 'active'
                        LIMIT 1
                    """, (persona_id, agent_id, content_hash))
                    row = cur.fetchone()
                    return dict(row) if row else None
        except Exception:
            return None

    def get_conflicts(self, memory_id: str) -> Dict[str, Any]:
        """查看指定记忆的冲突链（同一 conflict_group_id 的所有版本）。"""
        if not self._connected:
            return {"memory_id": memory_id, "conflicts": [], "error": "Not connected"}
        import psycopg2.extras
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("""
                        SELECT conflict_group_id FROM memories WHERE memory_id = %s
                    """, (memory_id,))
                    row = cur.fetchone()
                    if not row or not row["conflict_group_id"]:
                        return {"memory_id": memory_id, "conflicts": [], "conflict_group_id": None}

                    cgid = row["conflict_group_id"]
                    cur.execute("""
                        SELECT memory_id, content, content_hash, is_resolved,
                               created_at, updated_at, status
                        FROM memories
                        WHERE conflict_group_id = %s
                        ORDER BY created_at ASC
                    """, (str(cgid),))
                    conflicts = [dict(r) for r in cur.fetchall()]

                return {
                    "memory_id": memory_id,
                    "conflict_group_id": str(cgid),
                    "conflicts": conflicts,
                }
        except Exception as e:
            return {"memory_id": memory_id, "conflicts": [], "error": str(e)}

    def resolve_conflict(
        self, conflict_group_id: str, keep_memory_id: str
    ) -> Dict[str, Any]:
        """解决冲突：保留选定版本，软删除同一冲突组的其他版本。"""
        if not self._connected:
            return {"error": "Not connected", "resolved_count": 0}
        import psycopg2.extras
        now = datetime.now(timezone.utc)
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("""
                        UPDATE memories SET is_resolved = TRUE, updated_at = %s
                        WHERE memory_id = %s AND conflict_group_id = %s
                    """, (now, keep_memory_id, conflict_group_id))

                    cur.execute("""
                        SELECT memory_id FROM memories
                        WHERE conflict_group_id = %s
                          AND memory_id != %s
                          AND status = 'active'
                    """, (conflict_group_id, keep_memory_id))
                    discard_ids = [r["memory_id"] for r in cur.fetchall()]

                    if discard_ids:
                        cur.execute("""
                            UPDATE memories SET status = 'expired', is_resolved = TRUE, updated_at = %s
                            WHERE memory_id::text = ANY(%s::text[])
                        """, (now, discard_ids))

                    conn.commit()

                return {
                    "conflict_group_id": conflict_group_id,
                    "kept_memory_id": keep_memory_id,
                    "discarded_ids": [str(d) for d in discard_ids],
                    "resolved_count": len(discard_ids),
                }
        except Exception as e:
            return {"error": str(e), "resolved_count": 0}

    def dedup_stats(self) -> Dict[str, Any]:
        """返回去重统计信息。"""
        if not self._connected:
            return {"error": "Not connected"}
        import psycopg2.extras
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute("SELECT COUNT(*) as c FROM memories WHERE conflict_group_id IS NOT NULL")
                    total_in_conflicts = cur.fetchone()["c"]

                    cur.execute("SELECT COUNT(DISTINCT conflict_group_id) as c FROM memories WHERE conflict_group_id IS NOT NULL")
                    conflict_groups = cur.fetchone()["c"]

                    cur.execute("SELECT COUNT(*) as c FROM memories WHERE conflict_group_id IS NOT NULL AND LOWER(BTRIM(is_resolved)) = 'true'")
                    resolved = cur.fetchone()["c"]

                    cur.execute("SELECT COUNT(DISTINCT content_hash) as c FROM memories WHERE content_hash IS NOT NULL AND status = 'active'")
                    unique_hashes = cur.fetchone()["c"]

                return {
                    "total_in_conflict_groups": total_in_conflicts,
                    "conflict_groups": conflict_groups,
                    "resolved_conflicts": resolved,
                    "unique_content_hashes": unique_hashes,
                }
        except Exception as e:
            return {"error": str(e)}

    # ── Migration: SQLite → PostgreSQL ─────────────────────────────

    def migrate_from_sqlite(self, sqlite_path: str) -> Dict[str, Any]:
        """Migrate all data from a SQLite database to PostgreSQL.

        Args:
            sqlite_path: Path to existing SQLite database file.

        Returns:
            Migration statistics.
        """
        import sqlite3

        if not self._connected:
            self.connect()

        sqlite_conn = sqlite3.connect(sqlite_path)
        sqlite_conn.row_factory = sqlite3.Row

        stats = {
            "memories_migrated": 0,
            "versions_migrated": 0,
            "errors": 0,
            "error_details": [],
        }

        try:
            # Migrate memories
            sqlite_cur = sqlite_conn.cursor()
            sqlite_cur.execute("SELECT * FROM memories ORDER BY created_at ASC")

            with self._get_conn() as pg_conn:
                with pg_conn.cursor() as pg_cur:
                    for row in sqlite_cur.fetchall():
                        try:
                            row_dict = dict(row)
                            pg_cur.execute("""
                                INSERT INTO memories
                                (memory_id, session_id, persona_id, tenant_id, agent_id,
                                 content, role, importance, tags, category, sha256_hash,
                                 status, version, created_at, updated_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                        %s::timestamptz, %s::timestamptz)
                                ON CONFLICT (memory_id) DO NOTHING
                            """, (
                                row_dict.get("memory_id", str(uuid.uuid4())),
                                row_dict.get("session_id", str(uuid.uuid4())),
                                row_dict.get("persona_id", "default"),
                                row_dict.get("tenant_id", "default"),
                                # G66/t209：**本迁移此前省略 `agent_id` 列** ⇒ 迁移过来的行
                                # `agent_id IS NULL`（实测 11,808 行、且仍在增长；与主写入路径
                                # （`:578-590`/`:828-838`，26 列且含 `agent_id`）不一致）。
                                # 取值口径与 SQLite 侧 `_crud.py::_normalise_agent_id` 对齐：
                                # 空/NULL ⇒ `"default"`（NULL 是异常态，不作为缺省）。
                                row_dict.get("agent_id") or "default",
                                row_dict.get("content", ""),
                                row_dict.get("role", "user"),
                                row_dict.get("importance", 0.5),
                                json.loads(row_dict.get("tags", "[]")) if isinstance(row_dict.get("tags"), str) else row_dict.get("tags", []),
                                row_dict.get("category", "general"),
                                row_dict.get("sha256_hash", ""),
                                row_dict.get("status", "active"),
                                row_dict.get("version", 1),
                                row_dict.get("created_at", datetime.now(timezone.utc).isoformat()),
                                row_dict.get("updated_at", datetime.now(timezone.utc).isoformat()),
                            ))
                            stats["memories_migrated"] += 1
                        except Exception as e:
                            stats["errors"] += 1
                            stats["error_details"].append(str(e)[:200])

                    pg_conn.commit()

            # Migrate versions
            try:
                sqlite_cur.execute("SELECT * FROM memory_versions ORDER BY created_at ASC")
                with self._get_conn() as pg_conn:
                    with pg_conn.cursor() as pg_cur:
                        for row in sqlite_cur.fetchall():
                            try:
                                row_dict = dict(row)
                                pg_cur.execute("""
                                    INSERT INTO memory_versions
                                    (version_id, memory_id, content, sha256_hash, operation, created_at)
                                    VALUES (%s, %s, %s, %s, %s, %s::timestamptz)
                                    ON CONFLICT (version_id) DO NOTHING
                                """, (
                                    row_dict.get("version_id", str(uuid.uuid4())),
                                    row_dict.get("memory_id", ""),
                                    row_dict.get("content", ""),
                                    row_dict.get("sha256_hash", ""),
                                    row_dict.get("operation", "MIGRATE"),
                                    row_dict.get("created_at", datetime.now(timezone.utc).isoformat()),
                                ))
                                stats["versions_migrated"] += 1
                            except Exception as e:
                                stats["errors"] += 1
                        pg_conn.commit()
            except Exception as _e:
                swallow(__name__, _e)  # versions table may not exist in older SQLite

        finally:
            sqlite_conn.close()

        logger.info(
            "Migration complete: %d memories, %d versions, %d errors",
            stats["memories_migrated"], stats["versions_migrated"], stats["errors"],
        )
        return stats

    # ── Agent 权重管理 ─────────────────────────────────────────────

    def set_agent_weight(self, agent_id: str, weight: float) -> Dict[str, Any]:
        """设置 Agent 的检索权重。"""
        if not self._connected:
            return {"error": "Not connected"}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO agent_weights (agent_id, weight, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (agent_id) DO UPDATE
                    SET weight = EXCLUDED.weight, updated_at = NOW()
                """, (agent_id, weight))
                conn.commit()
        return {"agent_id": agent_id, "weight": weight}

    def get_agent_weights(self) -> Dict[str, float]:
        """获取所有 Agent 权重配置。"""
        if not self._connected:
            return {}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT agent_id, weight FROM agent_weights")
                return {row[0]: row[1] for row in cur.fetchall()}

    def delete_agent_weight(self, agent_id: str) -> bool:
        """删除 Agent 权重配置。"""
        if not self._connected:
            return False
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM agent_weights WHERE agent_id = %s", (agent_id,))
                conn.commit()
                return cur.rowcount > 0

    # ── 记忆关联（memory_links）───────────────────────────────────

    def create_memory_link(self, source_id: str, target_id: str,
                           link_type: str = "semantic",
                           strength: float = 0.5) -> Dict[str, Any]:
        """创建记忆关联链接。"""
        if not self._connected:
            return {"error": "Not connected"}
        if source_id == target_id:
            return {"error": "Cannot link memory with itself"}
        link_id = hashlib.sha256(
            f"{source_id}:{target_id}:{link_type}".encode()
        ).hexdigest()[:32]
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO memory_links (id, source_id, target_id, link_type, strength, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (source_id, target_id, link_type) DO NOTHING
                """, (link_id, source_id, target_id, link_type, strength))
                conn.commit()
        return {
            "id": link_id, "source_id": source_id, "target_id": target_id,
            "link_type": link_type, "strength": strength,
        }

    def get_linked_memories(self, memory_id: str,
                            min_strength: float = 0.0) -> List[Dict[str, Any]]:
        """获取与指定记忆关联的所有链接（按强度降序）。"""
        if not self._connected:
            return []
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT ml.*, m.content AS target_content
                    FROM memory_links ml
                    LEFT JOIN memories m ON m.memory_id = ml.target_id
                    WHERE ml.source_id = %s
                      AND ml.strength >= %s
                    ORDER BY ml.strength DESC
                """, (memory_id, min_strength))
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def strengthen_link(self, link_id: str,
                        increment: float = 0.1) -> Dict[str, Any]:
        """增强链接强度（上限 1.0）。"""
        if not self._connected:
            return {"error": "Not connected"}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE memory_links
                    SET strength = LEAST(strength + %s, 1.0)
                    WHERE id = %s
                """, (increment, link_id))
                conn.commit()
                cur.execute("SELECT * FROM memory_links WHERE id = %s", (link_id,))
                row = cur.fetchone()
                if row:
                    columns = [desc[0] for desc in cur.description]
                    return dict(zip(columns, row))
        return {"error": "Link not found"}

    def weaken_link(self, link_id: str,
                    decrement: float = 0.1) -> Dict[str, Any]:
        """削弱链接强度（下限 0.0）。"""
        if not self._connected:
            return {"error": "Not connected"}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE memory_links
                    SET strength = GREATEST(strength - %s, 0.0)
                    WHERE id = %s
                """, (decrement, link_id))
                conn.commit()
                cur.execute("SELECT * FROM memory_links WHERE id = %s", (link_id,))
                row = cur.fetchone()
                if row:
                    columns = [desc[0] for desc in cur.description]
                    return dict(zip(columns, row))
        return {"error": "Link not found"}

    def delete_memory_link(self, link_id: str) -> bool:
        """删除指定链接。"""
        if not self._connected:
            return False
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM memory_links WHERE id = %s", (link_id,))
                conn.commit()
                return cur.rowcount > 0

    def get_all_links(self, memory_id: str) -> Dict[str, Any]:
        """获取某记忆的所有关联链接和反向链接。"""
        if not self._connected:
            return {"outgoing": [], "incoming": []}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM memory_links WHERE source_id = %s", (memory_id,)
                )
                columns = [desc[0] for desc in cur.description]
                outgoing = [dict(zip(columns, row)) for row in cur.fetchall()]
                cur.execute(
                    "SELECT * FROM memory_links WHERE target_id = %s", (memory_id,)
                )
                incoming = [dict(zip(columns, row)) for row in cur.fetchall()]
        return {"outgoing": outgoing, "incoming": incoming}

    # ── 记忆图谱（entities + relations）───────────────────────────




    # ── Audit Log Methods ──────────────────────────────────────────
    def replay_agent_session(self, agent_id: str,
                              start_time: Optional[str] = None,
                              end_time: Optional[str] = None) -> List[Dict[str, Any]]:
        """回放某 Agent 在时间段内的所有操作。"""
        if not self._connected:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    query = """
                        SELECT id, memory_id, action, agent_id, persona_id,
                               timestamp, details, checksum
                        FROM audit_log
                        WHERE agent_id = %s
                    """
                    params: list = [agent_id]
                    # 2026-09-15（R41-P23）：**范围过滤与排序都必须按"时间"而非 TEXT 字典序**。
                    # `audit_log.timestamp` 是 TEXT 列，且历史上出现过异构格式
                    # （实测 226 行 `2026-09-03 15:10:37+08` 空格+本地偏移，来自早期
                    #  ops-bot 的 SUMMARY_WRITE/RETRO_BOOST；源头已修，行仍留库）。
                    # 字典序比较在 `' '</0x20` < `'T'`/0x54 处断裂 ⇒ **静默漏行**：
                    # 实测窗口 2026-09-03..09-08，按时间应命中 4445 条，按 TEXT 只命中 4291
                    # —— **漏掉 154 条**（且不报错）。故显式 cast 为 timestamptz。
                    if start_time:
                        query += " AND timestamp::timestamptz >= %s::timestamptz"
                        params.append(start_time)
                    if end_time:
                        query += " AND timestamp::timestamptz <= %s::timestamptz"
                        params.append(end_time)
                    query += " ORDER BY timestamp::timestamptz ASC, id ASC"
                    cur.execute(query, params)
                    cols = [desc[0] for desc in cur.description]
                    results = []
                    for row in cur.fetchall():
                        d = dict(zip(cols, row))
                        d["details"] = d.get("details", {}) or {}
                        if hasattr(d["timestamp"], "isoformat"):
                            d["timestamp"] = d["timestamp"].isoformat()
                        if d.get("memory_id"):
                            d["memory_id"] = str(d["memory_id"])
                        results.append(d)
                    return results
        except Exception as e:
            logger.warning("replay_agent_session failed: %s", e)
            return []

    # ── 2026-09 (EXECUTION 117): DCPM System1 信念持久化 ─────────────
    # ── 2026-09 (EXECUTION 125): SAGE 图记忆持久化 ─────────────
    def sage_save_snapshot(self, snapshot: dict) -> bool:
        """保存 SAGE 图快照（JSONB 单行，幂等 upsert）。

        658.60（连接耗尽事故修复）：sage_graph 是**整图单行快照**——实测单行 626MB，
        每次 upsert 需重写超大行（~100 秒），并发写入互相阻塞 → 连接堆积，
        最终 PostgreSQL 报 too many clients already（200 上限被打满，评测任务全挂）。
        加固两道：
          ① **时间节流**：同一进程内最快每 TRINITY_SAGE_SAVE_MIN_INTERVAL 秒写一次
             （默认 300s）；
          ② **大小护栏**：序列化后超过 TRINITY_SAGE_MAX_BYTES（默认 64MB）则跳过并告警，
             避免把连接与 WAL 拖死。
        """
        try:
            now = time.time()
            last = getattr(self, "_sage_last_save", 0.0)
            if now - last < float(os.environ.get("TRINITY_SAGE_SAVE_MIN_INTERVAL", "300")):
                return False
            payload = json.dumps(snapshot, ensure_ascii=False, default=str)
            if len(payload) > int(os.environ.get("TRINITY_SAGE_MAX_BYTES", str(64 * 1024 * 1024))):
                logger.warning("sage snapshot too large (%d bytes) — skipped", len(payload))
                return False
            # ── $795 写入经济学（2026-09-17）：**内容没变就不写** ──────────────
            # 实测证据：本 upsert 被调用 3,893 次 = **24GB WAL**（表本体仅 71MB，
            # 单个 snapshot 41MB JSON）——sage_graph 是"整图单行快照"，
            # 每次 upsert 都要重写这一超大行（TOAST 全量重写 + FPI）。
            # 而 self._sage_last_save 的 300s 节流只对**同一进程**有效：调用方
            # （sage_graph_memory_engine._persist）每次新建 adapter，
            # 维护链/评测脚本又各起新进程 ⇒ 节流跨进程失效，图没变也照写。
            # 因此这里加**内容寻址**：payload 的 sha256 与库中一致就整条跳过
            # （不写、不产生 WAL）。列 snapshot_hash 懒迁移（ADD COLUMN IF NOT
            # EXISTS，仅首次；回滚 = DROP COLUMN snapshot_hash）。
            digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    if not getattr(self, "_sage_hash_col", False):
                        try:
                            cur.execute(
                                "ALTER TABLE sage_graph "
                                "ADD COLUMN IF NOT EXISTS snapshot_hash TEXT")
                            conn.commit()
                        except Exception as _e:  # noqa: BLE001
                            conn.rollback()
                            logger.debug("sage snapshot_hash column ensure skipped: %s", _e)
                        self._sage_hash_col = True
                    cur.execute("""
                        INSERT INTO sage_graph (id, snapshot, snapshot_hash)
                        VALUES ('graph', %s, %s)
                        ON CONFLICT (id) DO UPDATE
                        SET snapshot = EXCLUDED.snapshot,
                            snapshot_hash = EXCLUDED.snapshot_hash,
                            updated_at = NOW()
                        WHERE sage_graph.snapshot_hash IS DISTINCT FROM EXCLUDED.snapshot_hash
                    """, (payload, digest))
                    changed = cur.rowcount > 0
                conn.commit()
            self._sage_last_save = now
            if not changed:
                # 图未变化：本次是**零 WAL** 的 no-op（旧实现在这里重写整行）
                self._sage_last_skip = getattr(self, "_sage_last_skip", 0) + 1
            return bool(changed)
        except Exception:
            return False

    def sage_load_snapshot(self):
        """读取 SAGE 图快照（无则 None）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT snapshot FROM sage_graph WHERE id = 'graph'")
                    row = cur.fetchone()
                    if row and row[0]:
                        return row[0] if isinstance(row[0], dict) else json.loads(row[0])
            return None
        except Exception:
            return None

    # ── 2026-09 (EXECUTION 141): 持久会话状态 ─────────────
    def context_save(self, last_query: str, percepts: list, affect=None,
                     session_id: str = "default", wm: Optional[list] = None) -> bool:
        """持久化最近上下文（跨进程/重启保留；按会话隔离）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    _aff = json.dumps(affect, ensure_ascii=False) if affect else None
                    _ctx_id = "ctx:" + str(session_id or "default")[:40]
                    _wm = json.dumps(wm, ensure_ascii=False) if wm else None
                    cur.execute("""
                        INSERT INTO session_context (id, last_query, percepts, affect, wm)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            last_query = EXCLUDED.last_query,
                            percepts = EXCLUDED.percepts,
                            affect = EXCLUDED.affect,
                            wm = EXCLUDED.wm,
                            updated_at = NOW()
                    """, (_ctx_id, last_query, json.dumps(percepts, ensure_ascii=False), _aff, _wm))
                conn.commit()
            return True
        except Exception:
            return False

    def context_load(self, session_id: str = "default"):
        """读取持久化上下文（无则 None；按会话隔离）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    _ctx_id = "ctx:" + str(session_id or "default")[:40]
                    cur.execute("SELECT last_query, percepts, affect, wm FROM session_context WHERE id = %s", (_ctx_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    _p = row[1]
                    if isinstance(_p, str):
                        import json as _j
                        _p = _j.loads(_p)
                    _a = row[2]
                    if isinstance(_a, str):
                        import json as _j2
                        _a = _j2.loads(_a)
                    _wm2 = row[3]
                    if isinstance(_wm2, str):
                        import json as _j3
                        _wm2 = _j3.loads(_wm2)
                    return {"last_query": row[0] or "", "percepts": _p or [],
                            "affect": _a or None, "wm": _wm2 or []}
            return None
        except Exception:
            return None

    # ── 2026-09 (EXECUTION 170): 记忆擦除（memory_unlearning 激活）──
    # ── 2026-09 (EXECUTION 183): 主动遗忘（大脑遗忘机制）──
    def forget_candidates(self, limit: int = 50, min_age_days: float = 14) -> list:
        """遗忘候选：低价值 + 长时间未访问 + 访问少。

        大脑对应：突触修剪——不被使用的弱连接被清除。
        评分 = importance 低(0-0.3) + created_at 久(>min_age_days) + access_count 少(<3)。
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT memory_id, content, importance, access_count, created_at
                        FROM memories
                        WHERE status = 'active'
                          AND (importance IS NULL OR importance < 0.3)
                          AND (created_at::timestamp < NOW() - make_interval(days => %s)
                               OR created_at IS NULL)
                          AND (access_count IS NULL OR access_count < 3)
                          AND category NOT IN ('perception', 'self-reflection', 'self-identity',
                                               'self-observation', 'action-experience', 'dcpm-core')
                        ORDER BY created_at
                        LIMIT %s
                    """, (min_age_days, limit))
                    return [dict(zip(["memory_id", "content", "importance", "access_count", "created_at"], r))
                            for r in cur.fetchall()]
        except Exception:
            return []

    def apply_forgetting(self, candidates: list, dry_run: bool = True) -> dict:
        """应用遗忘：标记 status='forgotten'（保留审计，不物理删除）。"""
        n = 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    for c in candidates:
                        mid = c.get("memory_id")
                        if not mid:
                            continue
                        if dry_run:
                            n += 1
                            continue
                        cur.execute("UPDATE memories SET status='forgotten' WHERE memory_id=%s AND status='active'",
                                   (mid,))
                        if cur.rowcount:
                            n += 1
                            self.write_audit_log(
                                memory_id=None, action="memory_forgotten",
                                agent_id="forgetting",
                                details={"memory_id": mid, "reason": "low_value_unused"},
                            )
                conn.commit()
        except Exception as _e:
            swallow(__name__, _e)
        return {"forgotten": n, "dry_run": dry_run}

    def erase_memory(self, memory_id: str, reason: str = "manual") -> dict:
        """可验证擦除：删除记忆 + 审计记录 + 返回擦除证明。

        GDPR Article 17 被遗忘权——擦除后外部可验证（审计链记录）。
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT content FROM memories WHERE memory_id = %s", (memory_id,))
                    row = cur.fetchone()
                    if not row:
                        return {"erased": False, "error": "not found"}
                    content = row[0]
                    cur.execute("DELETE FROM memories WHERE memory_id = %s", (memory_id,))
                    # 审计记录
                    _proof = {
                        "memory_id": memory_id,
                        "reason": reason,
                        "fingerprint": hashlib.sha256(str(content).encode()).hexdigest()[:16],
                    }
                    self.write_audit_log(
                        memory_id=None, action="memory_erased",
                        agent_id="memory-unlearning",
                        details=_proof,
                    )
                conn.commit()
            return {"erased": True, "proof": _proof}
        except Exception:
            return {"erased": False, "error": "erase failed"}

    def dcpm_store_belief(self, belief_id, subject, predicate, obj, superseded_by=None):
        """持久化 System1 信念（跨进程可见，供夜间整合读取）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO dcpm_beliefs (belief_id, subject, predicate, object, superseded_by)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (belief_id) DO NOTHING
                    """, (belief_id, subject, predicate, obj, superseded_by))
                conn.commit()
            return True
        except Exception:
            return False

    def dcpm_get_beliefs(self, limit=500):
        """读取全部持久化信念（夜间整合输入）。"""
        import psycopg2.extras
        try:
            with self._get_conn() as conn:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                    cur.execute(
                        "SELECT belief_id, subject, predicate, object, superseded_by, created_at "
                        "FROM dcpm_beliefs ORDER BY created_at DESC LIMIT %s", (limit,))
                    return [dict(r) for r in cur.fetchall()]
        except Exception:
            return []

    def dcpm_count(self):
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT count(*) FROM dcpm_beliefs")
                    return int(cur.fetchone()[0])
        except Exception:
            return 0



    # ── 身份锚点 CRUD ───────────────────────────────────────────

    def upsert_anchor(self, agent_id: str, anchor_type: str,
                      content: str, version: int = 1) -> Dict[str, Any]:
        """注册或更新身份锚点（幂等：按 agent_id + anchor_type 去重）。"""
        if not self._connected:
            return {"error": "Not connected"}
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    checksum = self._compute_sha256(content)

                    cur.execute(
                        "SELECT id, version FROM identity_anchors WHERE agent_id = %s AND anchor_type = %s",
                        (agent_id, anchor_type),
                    )
                    existing = cur.fetchone()

                    if existing:
                        anchor_id = existing[0]
                        new_version = existing[1] + 1
                        cur.execute("""
                            UPDATE identity_anchors
                            SET content = %s, version = %s, checksum = %s, updated_at = NOW()
                            WHERE id = %s
                        """, (content, new_version, checksum, anchor_id))
                    else:
                        anchor_id = f"anchor_{uuid.uuid4().hex[:12]}"
                        cur.execute("""
                            INSERT INTO identity_anchors (id, agent_id, anchor_type, content, version, checksum)
                            VALUES (%s, %s, %s, %s, %s, %s)
                        """, (anchor_id, agent_id, anchor_type, content, version, checksum))

                    conn.commit()

            return {
                "id": anchor_id,
                "agent_id": agent_id,
                "anchor_type": anchor_type,
                "version": existing[1] + 1 if existing else version,
                "checksum": checksum,
            }
        except Exception as e:
            logger.warning("upsert_anchor failed: %s", e)
            return {"error": str(e)}

    def get_anchors(self, agent_id: str,
                    anchor_type: Optional[str] = None) -> List[Dict[str, Any]]:
        """获取指定 Agent 的锚点列表。"""
        if not self._connected:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    if anchor_type:
                        cur.execute(
                            "SELECT * FROM identity_anchors WHERE agent_id = %s AND anchor_type = %s ORDER BY anchor_type, version DESC",
                            (agent_id, anchor_type),
                        )
                    else:
                        cur.execute(
                            "SELECT * FROM identity_anchors WHERE agent_id = %s ORDER BY anchor_type, version DESC",
                            (agent_id,),
                        )
                    cols = [desc[0] for desc in cur.description]
                    return [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception as e:
            logger.warning("get_anchors failed: %s", e)
            return []

    def get_all_anchors(self, agent_id: str) -> Dict[str, List[Dict[str, Any]]]:
        """获取指定 Agent 按类型分组的所有锚点。"""
        anchors = self.get_anchors(agent_id)
        grouped: Dict[str, list] = {
            "identity_files": [],
            "procedural_patterns": [],
            "episodic_keys": [],
            "value_specifications": [],
        }
        for a in anchors:
            atype = a.get("anchor_type", "")
            if atype in grouped:
                grouped[atype].append(a)
        return grouped

    def get_latest_anchor_version(self, agent_id: str,
                                   anchor_type: str) -> Optional[Dict[str, Any]]:
        """获取指定 Agent 指定类型的最高版本锚点。"""
        if not self._connected:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM identity_anchors WHERE agent_id = %s AND anchor_type = %s "
                        "ORDER BY version DESC LIMIT 1",
                        (agent_id, anchor_type),
                    )
                    row = cur.fetchone()
                    if row:
                        cols = [desc[0] for desc in cur.description]
                        return dict(zip(cols, row))
            return None
        except Exception as e:
            logger.warning("get_latest_anchor_version failed: %s", e)
            return None

    # ── DCSA-EJP 审计 CRUD ──────────────────────────────────────────


    def log_constitutional_violation(self, run_id: str, invariant: str,
                                      severity: str, context: str = "{}") -> bool:
        import uuid as _uuid, json as _json
        if not self._connected:
            return False
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO constitutional_violations "
                        "(violation_id, run_id, invariant, severity, context) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (f"cv_{_uuid.uuid4().hex[:12]}", run_id, invariant, severity,
                         _json.dumps(context) if not isinstance(context, str) else context),
                    )
            return True
        except Exception:
            return False



    def get_violation_trends(self, agent_id: Optional[str] = None,
                              limit: int = 100) -> List[Dict[str, Any]]:
        if not self._connected:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    if agent_id:
                        cur.execute(
                            "SELECT cv.*, ar.agent_id FROM constitutional_violations cv "
                            "JOIN audit_runs ar ON cv.run_id = ar.run_id "
                            "WHERE ar.agent_id = %s ORDER BY cv.timestamp DESC LIMIT %s",
                            (agent_id, limit),
                        )
                    else:
                        cur.execute(
                            "SELECT cv.*, ar.agent_id FROM constitutional_violations cv "
                            "JOIN audit_runs ar ON cv.run_id = ar.run_id "
                            "ORDER BY cv.timestamp DESC LIMIT %s",
                            (limit,),
                        )
                    cols = [desc[0] for desc in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            return []

    # ── Diagnostics ────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        if not self._connected:
            return {
                "adapter": "postgresql",
                "connected": False,
                "pool": None,
            }

        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM memories")
                    total = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'")
                    active = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(DISTINCT persona_id) FROM memories")
                    personas = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(DISTINCT agent_id) FROM memories")
                    agents = cur.fetchone()[0]

                    cur.execute("SELECT COUNT(DISTINCT tenant_id) FROM memories")
                    tenants_count = cur.fetchone()[0]

                    # TTL 统计
                    cur.execute("SELECT COUNT(*) FROM memories WHERE status = 'expired'")
                    expired = cur.fetchone()[0]

                    cur.execute("SELECT AVG(access_count) FROM memories WHERE status = 'active'")
                    avg_access = cur.fetchone()[0] or 0

                    cur.execute("SELECT COUNT(*) FROM audit_log")
                    audit_log_count = cur.fetchone()[0]

            return {
                "adapter": "postgresql",
                "connected": True,
                "host": self._host,
                "port": self._port,
                "dbname": self._dbname,
                "pool_min": self._min_conn,
                "pool_max": self._max_conn,
                "pool_active": self._pool._used if self._pool else 0,
                "total_memories": total,
                "active_memories": active,
                "expired_memories": expired,
                "total_personas": personas,
                "total_agents": agents,
                "total_tenants": tenants_count,
                "avg_access_count": round(float(avg_access), 2),
                "agent_weights_configured": len(self.get_agent_weights()),
                "memory_links_count": self._get_memory_links_count(),
                "entity_count": self._get_entity_count(),
                "relation_count": self._get_relation_count(),
                "audit_log_count": audit_log_count,
                "identity_anchor_count": self._get_identity_anchor_count(),
                "audit_run_count": self._get_audit_run_count(),
                "violation_count": self._get_violation_count(),
                "a2a_task_count": self._get_a2a_task_count(),
                "agent_registry_count": self._get_agent_registry_count(),
            }
        except Exception as e:
            return {
                "adapter": "postgresql",
                "connected": True,
                "error": str(e),
            }

    def _get_memory_links_count(self) -> int:
        """返回 memory_links 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM memory_links")
                    return cur.fetchone()[0]
        except Exception:
            return 0

    def _get_entity_count(self) -> int:
        """返回 entities 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM entities")
                    return cur.fetchone()[0]
        except Exception:
            return 0

    def _get_relation_count(self) -> int:
        """返回 relations 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM relations")
                    return cur.fetchone()[0]
        except Exception:
            return 0

    def _get_identity_anchor_count(self) -> int:
        """返回 identity_anchors 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM identity_anchors")
                    return cur.fetchone()[0]
        except Exception:
            return 0


    def _get_violation_count(self) -> int:
        """返回 constitutional_violations 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM constitutional_violations")
                    return cur.fetchone()[0]
        except Exception:
            return 0

    # ── A2A Protocol: Task Management ──────────────────────────────

    def register_agent_card(self, agent_id: str, card_json: str) -> bool:
        """注册或更新 Agent Card 到全局注册中心。"""
        if not self._connected:
            return False
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO agent_registry "
                        "(agent_id, card_json, last_heartbeat, status) "
                        "VALUES (%s, %s, NOW(), 'active') "
                        "ON CONFLICT (agent_id) DO UPDATE SET "
                        "card_json = EXCLUDED.card_json, "
                        "last_heartbeat = NOW(), status = 'active'",
                        (agent_id, card_json),
                    )
                    conn.commit()
            return True
        except Exception:
            return False

    def get_agent_card(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取 Agent 的注册卡片。"""
        if not self._connected:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM agent_registry WHERE agent_id = %s",
                        (agent_id,),
                    )
                    row = cur.fetchone()
                    if row:
                        import psycopg2.extras
                        return dict(row)
            return None
        except Exception:
            return None

    def create_a2a_task(self, task_id: str, from_agent: str, to_agent: str,
                         payload: str, status: str = "pending",
                         result: Optional[str] = None) -> bool:
        """创建跨 Agent 任务记录。"""
        if not self._connected:
            return False
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO a2a_tasks "
                        "(task_id, from_agent, to_agent, payload, status, result) "
                        "VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb) "
                        "ON CONFLICT (task_id) DO NOTHING",
                        (task_id, from_agent, to_agent, payload, status, result),
                    )
                    conn.commit()
            return True
        except Exception:
            return False

    def update_a2a_task(self, task_id: str, status: str,
                         result: Optional[str] = None) -> bool:
        """更新跨 Agent 任务状态。"""
        if not self._connected:
            return False
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE a2a_tasks SET status = %s, result = %s::jsonb, "
                        "updated_at = NOW() WHERE task_id = %s",
                        (status, result, task_id),
                    )
                    conn.commit()
            return True
        except Exception:
            return False

    def list_a2a_tasks(self, task_id: Optional[str] = None,
                        agent_id: Optional[str] = None,
                        status: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """列出跨 Agent 任务，支持按 agent_id / status / task_id 过滤。"""
        if not self._connected:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    if task_id:
                        cur.execute(
                            "SELECT * FROM a2a_tasks WHERE task_id = %s",
                            (task_id,),
                        )
                    else:
                        query = "SELECT * FROM a2a_tasks WHERE 1=1"
                        params: list = []
                        if agent_id:
                            query += " AND (from_agent = %s OR to_agent = %s)"
                            params.extend([agent_id, agent_id])
                        if status:
                            query += " AND status = %s"
                            params.append(status)
                        query += " ORDER BY created_at DESC LIMIT %s"
                        params.append(limit)
                        cur.execute(query, params)
                    rows = cur.fetchall()
                    import psycopg2.extras
                    return [dict(r) for r in rows]
        except Exception:
            return []

    def update_agent_heartbeat(self, agent_id: str) -> bool:
        """更新 Agent 注册中心的心跳时间戳。"""
        if not self._connected:
            return False
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE agent_registry SET last_heartbeat = NOW() "
                        "WHERE agent_id = %s",
                        (agent_id,),
                    )
                    conn.commit()
            return True
        except Exception:
            return False

    def _get_a2a_task_count(self) -> int:
        """返回 a2a_tasks 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM a2a_tasks")
                    return cur.fetchone()[0]
        except Exception:
            return 0

    def _get_agent_registry_count(self) -> int:
        """返回 agent_registry 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM agent_registry")
                    return cur.fetchone()[0]
        except Exception:
            return 0

