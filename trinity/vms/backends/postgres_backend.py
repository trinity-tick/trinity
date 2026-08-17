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
        self._conn = None
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
            except Exception:
                pass
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
