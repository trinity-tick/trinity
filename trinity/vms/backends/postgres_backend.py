"""
Trinity VMS — PostgreSQL Backend.

PostgreSQL-based MemoryStore using psycopg2 (or asyncpg).
Supports pgvector extension for vector similarity search.

Configuration via environment variable DATABASE_URL or constructor arg.

Schema::

    CREATE TABLE memories (
        memory_id   TEXT PRIMARY KEY,
        content     TEXT NOT NULL,
        agent_id    TEXT DEFAULT 'default',
        persona_id  TEXT DEFAULT 'default',
        session_id  TEXT,
        tenant_id   TEXT DEFAULT 'default',
        role        TEXT DEFAULT 'user',
        importance  REAL DEFAULT 0.5,
        tags        JSONB DEFAULT '[]',
        category    TEXT DEFAULT 'general',
        embedding   VECTOR(384),     -- pgvector extension (optional)
        created_at  TIMESTAMPTZ DEFAULT NOW(),
        updated_at  TIMESTAMPTZ DEFAULT NOW(),
        is_deleted  BOOLEAN DEFAULT FALSE
    );
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional
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

logger = logging.getLogger(__name__)


class PostgresBackend:
    """PostgreSQL memory backend with optional pgvector support.

    Parameters
    ----------
    connection_string : str
        PostgreSQL connection string, e.g.
        ``postgresql://user:pass@localhost:5432/trinity``.
    pool_min : int
        Minimum connection pool size.
    pool_max : int
        Maximum connection pool size.
    """

    def __init__(
        self,
        connection_string: str = "",
        pool_min: int = 2,
        pool_max: int = 10,
    ):
        self._conn_str = connection_string or os.environ.get(
            "DATABASE_URL",
            "postgresql://postgres:postgres@localhost:5432/trinity",
        )
        self._pool_min = pool_min
        self._pool_max = pool_max
        # 2026-09-29（Round 31）：原为无注解的 `self._conn = None` ⇒ mypy 推断为 `None`
        # ⇒ 下面 6 处 `self._conn.autocommit/cursor(...)` 全报
        # `"None" has no attribute ...`。它实际持有 psycopg2 连接（或 None）
        # ⇒ 注解 `Any` 是**说真话**，不是放宽类型。
        self._conn: Any = None
        self._has_pgvector = False

    def connect(self) -> None:
        """Establish connection and create schema."""
        try:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(self._conn_str)
            self._conn.autocommit = True
            self._create_schema()
            self._detect_pgvector()
            logger.info("PostgresBackend connected to %s", self._conn_str.split("@")[-1])
        except ImportError:
            logger.warning("psycopg2 not installed — PostgresBackend unavailable")
        except Exception as exc:
            logger.warning("PostgresBackend connection failed: %s", exc)

    def disconnect(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception as _e:
                swallow(__name__, _e)
            self._conn = None

    # ── Schema ────────────────────────────────────────────────────────

    def _create_schema(self):
        ddl = """
        CREATE TABLE IF NOT EXISTS memories (
            memory_id   TEXT PRIMARY KEY,
            content     TEXT NOT NULL,
            agent_id    TEXT DEFAULT 'default',
            persona_id  TEXT DEFAULT 'default',
            session_id  TEXT,
            tenant_id   TEXT DEFAULT 'default',
            role        TEXT DEFAULT 'user',
            importance  REAL DEFAULT 0.5,
            tags        JSONB DEFAULT '[]'::jsonb,
            category    TEXT DEFAULT 'general',
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW(),
            is_deleted  BOOLEAN DEFAULT FALSE
        );
        CREATE INDEX IF NOT EXISTS idx_memories_agent
            ON memories(agent_id, tenant_id);
        CREATE INDEX IF NOT EXISTS idx_memories_category
            ON memories(category);
        """
        with self._conn.cursor() as cur:
            for stmt in ddl.split(";"):
                stmt = stmt.strip()
                if stmt:
                    cur.execute(stmt)

    def _detect_pgvector(self):
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
                self._has_pgvector = cur.fetchone() is not None
        except Exception:
            self._has_pgvector = False

    # ── MemoryStore Protocol ──────────────────────────────────────────

    def add(
        self,
        content: str,
        agent_id: str = "default",
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
    ) -> Dict[str, Any]:
        import uuid
        from datetime import datetime, timezone

        memory_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        tags_json = json.dumps(tags or [])

        # t50/G9：与**同目录 sqlite backend**（走适配器 ⇒ t48 已掩码）行为一致：
        # 这里裸 SQL，必须显式过同一守卫；high ⇒ 拒存（与单条写入路径同语义）。
        from trinity.adapters._pii_guard import adapter_pii_guard
        content, _vms_md, _vms_info = adapter_pii_guard(content, None)
        if _vms_info.get("refuse"):
            return {"memory_id": "", "error": "sensitive-high refused (adapter PII guard)",
                    "agent_id": agent_id, "category": category}

        sql = """
        INSERT INTO memories
            (memory_id, content, agent_id, persona_id, session_id,
             tenant_id, role, importance, tags, category, created_at, updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING memory_id, created_at
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                memory_id, content, agent_id, persona_id, session_id,
                tenant_id, role, importance, tags_json, category, now, now,
            ))
            row = cur.fetchone()

        return {
            "memory_id": memory_id,
            "created_at": now,
            "agent_id": agent_id,
            "category": category,
        }

    def get(self, memory_id: str) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM memories WHERE memory_id = %s AND is_deleted = FALSE"
        with self._conn.cursor() as cur:
            cur.execute(sql, (memory_id,))
            row = cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    def search(
        self,
        query: str,
        top_k: int = 10,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        conditions = ["is_deleted = FALSE"]
        params: List[Any] = []

        if agent_id:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if persona_id:
            conditions.append("persona_id = %s")
            params.append(persona_id)
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)

        # Full-text search on content (fallback to ILIKE if query is simple)
        conditions.append(
            "to_tsvector('english', content) @@ plainto_tsquery('english', %s)"
        )
        params.append(query)
        conditions.append("content ILIKE %s")
        params.append(f"%{query}%")

        where = "WHERE " + " AND ".join(conditions)
        sql = f"""
        SELECT * FROM memories
        {where}
        ORDER BY
            ts_rank(to_tsvector('english', content),
                    plainto_tsquery('english', %s)) DESC,
            importance DESC
        LIMIT %s
        """
        params.insert(-2, query)
        params.append(top_k)

        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in rows]

    def delete(self, memory_id: str, soft: bool = True) -> bool:
        if soft:
            sql = "UPDATE memories SET is_deleted = TRUE WHERE memory_id = %s"
        else:
            sql = "DELETE FROM memories WHERE memory_id = %s"
        with self._conn.cursor() as cur:
            cur.execute(sql, (memory_id,))
            return cur.rowcount > 0

    def count(
        self,
        agent_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
    ) -> int:
        conditions = ["is_deleted = FALSE"]
        params: List[Any] = []
        if agent_id:
            conditions.append("agent_id = %s")
            params.append(agent_id)
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)
        where = "WHERE " + " AND ".join(conditions)
        with self._conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM memories {where}", params)
            return cur.fetchone()[0]

    @property
    def has_pgvector(self) -> bool:
        return self._has_pgvector
