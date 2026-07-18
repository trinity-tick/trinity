"""PostgreSQL storage adapter — production multi-tenant backend with connection pooling."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from .base import StorageAdapter

logger = logging.getLogger(__name__)

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

    return {
        "host": os.environ.get("PGHOST", "localhost"),
        "port": int(os.environ.get("PGPORT", "5432")),
        "dbname": os.environ.get("PGDATABASE", os.environ.get("PGDBNAME", "trinity")),
        "user": os.environ.get("PGUSER", "trinity"),
        "password": os.environ.get("PGPASSWORD", "trinity"),
        "min_conn": int(os.environ.get("PG_MIN_CONN", "1")),
        "max_conn": int(os.environ.get("PG_MAX_CONN", "10")),
    }


class PostgreSQLAdapter(StorageAdapter):
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
        self._pool = None
        self._connected = False
        self._pool_lock = threading.Lock()

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

            self._create_tables()
            self._connected = True

    @contextmanager
    def _get_conn(self) -> Iterator[Any]:
        """Get a connection from the pool (context manager)."""
        if not self._pool or not self._connected:
            raise RuntimeError(
                "PostgreSQL adapter not connected. Call connect() first."
            )

        conn = None
        try:
            conn = self._pool.getconn()
            yield conn
        finally:
            if conn and self._pool:
                self._pool.putconn(conn)

    def disconnect(self) -> None:
        """Close all connections in the pool."""
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

    def _create_tables(self) -> None:
        """Create database schema if not exists."""
        import psycopg2.extras

        init_sql = """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

        CREATE TABLE IF NOT EXISTS memories (
            memory_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            session_id    UUID NOT NULL,
            persona_id    VARCHAR(128) NOT NULL DEFAULT 'default',
            tenant_id     VARCHAR(128) NOT NULL DEFAULT 'default',
            content       TEXT NOT NULL,
            role          VARCHAR(32) NOT NULL DEFAULT 'user',
            importance    DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            tags          TEXT[] DEFAULT '{}',
            category      VARCHAR(128) NOT NULL DEFAULT 'general',
            sha256_hash   VARCHAR(64) NOT NULL,
            status        VARCHAR(32) NOT NULL DEFAULT 'active',
            version       INTEGER NOT NULL DEFAULT 1,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS memory_versions (
            version_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            memory_id     UUID NOT NULL REFERENCES memories(memory_id),
            content       TEXT NOT NULL,
            sha256_hash   VARCHAR(64) NOT NULL,
            operation     VARCHAR(32) NOT NULL DEFAULT 'CREATE',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        -- Indexes for search performance
        CREATE INDEX IF NOT EXISTS idx_memories_persona ON memories(persona_id);
        CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id);
        CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN(tags);
        CREATE INDEX IF NOT EXISTS idx_memories_content_fts
            ON memories USING GIN(to_tsvector('simple', content));

        -- Insert sample data if empty
        INSERT INTO memories (memory_id, session_id, persona_id, tenant_id, content, role, sha256_hash, category)
        SELECT uuid_generate_v4(), uuid_generate_v4(), 'system', 'default', 'Trinity PostgreSQL initialized at ' || NOW(), 'system',
               sha256('Trinity PostgreSQL initialized'), 'system'
        WHERE NOT EXISTS (SELECT 1 FROM memories WHERE persona_id = 'system' AND role = 'system');
        """

        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # Split and execute each statement
                    for statement in init_sql.split(";"):
                        stmt = statement.strip()
                        if stmt:
                            cur.execute(stmt)
                    conn.commit()
            logger.info("PostgreSQL schema created/verified")
        except Exception as e:
            logger.warning("Schema creation issue (may already exist): %s", e)

    # ── Hashing ───────────────────────────────────────────────��────

    @staticmethod
    def _compute_sha256(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    # ── CRUD Operations ────────────────────────────────────────────

    def store_memory(
        self,
        content: str,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
    ) -> Dict[str, Any]:
        import psycopg2.extras

        memory_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        if not session_id:
            session_id = str(uuid.uuid4())
        sha256_hash = self._compute_sha256(content)
        now = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO memories
                    (memory_id, session_id, persona_id, tenant_id, content, role,
                     importance, tags, category, sha256_hash, status, version, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', 1, %s::timestamptz, %s::timestamptz)
                """, (memory_id, session_id, persona_id, tenant_id, content, role,
                      importance, tags or [], category, sha256_hash, now, now))

                cur.execute("""
                    INSERT INTO memory_versions
                    (version_id, memory_id, content, sha256_hash, operation, created_at)
                    VALUES (%s, %s, %s, %s, 'CREATE', %s::timestamptz)
                """, (version_id, memory_id, content, sha256_hash, now))
                conn.commit()

        return {
            "memory_id": memory_id,
            "version_id": version_id,
            "sha256_hash": sha256_hash,
            "timestamp": now,
            "persona_id": persona_id,
            "session_id": session_id,
        }

    def search_memories(
        self,
        query: str,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        top_k: int = 10,
    ) -> List[Dict[str, Any]]:
        import psycopg2.extras

        conditions = ["status = 'active'"]
        params = []

        if persona_id:
            conditions.append("persona_id = %s")
            params.append(persona_id)
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)

        where = " AND ".join(conditions)
        params.extend([query, query, top_k])

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(f"""
                    SELECT *,
                           ts_rank(to_tsvector('simple', content),
                                   plainto_tsquery('simple', %s)) as score
                    FROM memories
                    WHERE {where}
                      AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
                    ORDER BY score DESC, importance DESC, created_at DESC
                    LIMIT %s
                """, params)

                results = []
                for row in cur.fetchall():
                    results.append({
                        "memory_id": str(row["memory_id"]),
                        "content": row["content"],
                        "content_preview": row["content"][:100],
                        "persona_id": row["persona_id"],
                        "session_id": str(row["session_id"]),
                        "role": row["role"],
                        "importance": float(row["importance"]),
                        "tags": row["tags"],
                        "category": row["category"],
                        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
                        "score": float(row["score"]) if row["score"] else 0.0,
                    })

        return results

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM memories WHERE memory_id = %s::uuid", (memory_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    def get_persona_memories(self, persona_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
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
                    "UPDATE memories SET status = 'deleted', updated_at = NOW() WHERE memory_id = %s::uuid",
                    (memory_id,)
                )
                conn.commit()
                return cur.rowcount > 0

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
                cur.execute("SELECT * FROM memories WHERE memory_id = %s::uuid", (memory_id,))
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
                    updates.append("tags = %s")
                    params.append(tags)

                if category is not None:
                    updates.append("category = %s")
                    params.append(category)

                params.append(memory_id)

                update_sql = f"UPDATE memories SET {', '.join(updates)} WHERE memory_id = %s::uuid"
                cur.execute(update_sql, params)

                # Version trail
                if content is not None:
                    new_content = content
                    cur.execute("""
                        INSERT INTO memory_versions
                        (version_id, memory_id, content, sha256_hash, operation, created_at)
                        VALUES (%s, %s, %s, %s, 'UPDATE', %s::timestamptz)
                    """, (version_id, memory_id, new_content, self._compute_sha256(new_content), now))

                conn.commit()

                # Return updated memory
                cur.execute("SELECT * FROM memories WHERE memory_id = %s::uuid", (memory_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    # ── Version Chain ─────────────────────────────────────────────

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT * FROM memory_versions
                    WHERE memory_id = %s::uuid ORDER BY created_at ASC
                """, (memory_id,))
                return [dict(row) for row in cur.fetchall()]

    def get_all_memories(self, limit: int = 200) -> List[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("""
                    SELECT * FROM memories
                    WHERE status = 'active'
                    ORDER BY created_at DESC LIMIT %s
                """, (limit,))
                return [dict(row) for row in cur.fetchall()]

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
                                (memory_id, session_id, persona_id, tenant_id, content, role,
                                 importance, tags, category, sha256_hash, status, version,
                                 created_at, updated_at)
                                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                                        %s::timestamptz, %s::timestamptz)
                                ON CONFLICT (memory_id) DO NOTHING
                            """, (
                                row_dict.get("memory_id", str(uuid.uuid4())),
                                row_dict.get("session_id", str(uuid.uuid4())),
                                row_dict.get("persona_id", "default"),
                                row_dict.get("tenant_id", "default"),
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
            except Exception:
                pass  # versions table may not exist in older SQLite

        finally:
            sqlite_conn.close()

        logger.info(
            "Migration complete: %d memories, %d versions, %d errors",
            stats["memories_migrated"], stats["versions_migrated"], stats["errors"],
        )
        return stats

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

                    cur.execute("SELECT COUNT(DISTINCT tenant_id) FROM memories")
                    tenants_count = cur.fetchone()[0]

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
                "total_personas": personas,
                "total_tenants": tenants_count,
            }
        except Exception as e:
            return {
                "adapter": "postgresql",
                "connected": True,
                "error": str(e),
            }
