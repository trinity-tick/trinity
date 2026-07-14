"""PostgreSQL storage adapter — production multi-tenant backend."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .base import StorageAdapter


class PostgreSQLAdapter(StorageAdapter):
    """PostgreSQL-based storage adapter.

    Production backend with:
      - Multi-tenant isolation (tenant_id)
      - Multi-persona support (persona_id)
      - Session scoping (session_id)
      - Full-text search via pg_trgm
      - Version chain for audit/provenance
    """

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5432,
        dbname: str = "trinity",
        user: str = "trinity",
        password: str = "trinity",
    ):
        self.host = host
        self.port = port
        self.dbname = dbname
        self.user = user
        self.password = password
        self._conn = None

    def connect(self) -> None:
        """Connect to PostgreSQL."""
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise ImportError(
                "psycopg2 required for PostgreSQL adapter. "
                "Install: pip install trinity-memory[postgres]"
            )

        self._conn = psycopg2.connect(
            host=self.host,
            port=self.port,
            dbname=self.dbname,
            user=self.user,
            password=self.password,
        )
        self._conn.autocommit = True
        self._create_tables()

    def disconnect(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
            self._conn = None

    def _create_tables(self) -> None:
        cursor = self._conn.cursor()
        cursor.execute(open(
            os.path.join(os.path.dirname(__file__), "../../docker/init-db.sql"), "r"
        ).read())

    def _compute_sha256(self, content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

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
        if not self._conn:
            raise RuntimeError("Not connected.")

        import psycopg2.extras
        cursor = self._conn.cursor()

        memory_id = str(uuid.uuid4())
        version_id = str(uuid.uuid4())
        if not session_id:
            session_id = str(uuid.uuid4())
        sha256_hash = self._compute_sha256(content)
        now = datetime.now(timezone.utc).isoformat()

        cursor.execute("""
            INSERT INTO memories
            (memory_id, session_id, persona_id, tenant_id, content, role,
             importance, tags, category, sha256_hash, status, version, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', 1, %s, %s)
        """, (memory_id, session_id, persona_id, tenant_id, content, role,
              importance, tags or [], category, sha256_hash, now, now))

        cursor.execute("""
            INSERT INTO memory_versions
            (version_id, memory_id, content, sha256_hash, operation, created_at)
            VALUES (%s, %s, %s, %s, 'CREATE', %s)
        """, (version_id, memory_id, content, sha256_hash, now))

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
        if not self._conn:
            return []

        import psycopg2.extras
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        conditions = ["status = 'active'"]
        params = []

        if persona_id:
            conditions.append("persona_id = %s")
            params.append(persona_id)
        if tenant_id:
            conditions.append("tenant_id = %s")
            params.append(tenant_id)

        where = " AND ".join(conditions)

        cursor.execute(f"""
            SELECT *,
                   ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', %s)) as score
            FROM memories
            WHERE {where}
              AND to_tsvector('simple', content) @@ plainto_tsquery('simple', %s)
            ORDER BY score DESC, importance DESC, created_at DESC
            LIMIT %s
        """, [query, query, top_k])

        results = []
        for row in cursor.fetchall():
            results.append({
                "memory_id": row["memory_id"],
                "content": row["content"],
                "content_preview": row["content"][:100],
                "persona_id": row["persona_id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "importance": float(row["importance"]),
                "tags": row["tags"],
                "category": row["category"],
                "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
                "score": float(row["score"]) if row["score"] else 0.0,
            })

        return results

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        if not self._conn:
            return None

        import psycopg2.extras
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("SELECT * FROM memories WHERE memory_id = %s", (memory_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_persona_memories(self, persona_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        if not self._conn:
            return []

        import psycopg2.extras
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("""
            SELECT * FROM memories
            WHERE persona_id = %s AND status = 'active'
            ORDER BY created_at DESC LIMIT %s
        """, (persona_id, limit))
        return [dict(row) for row in cursor.fetchall()]

    def delete_memory(self, memory_id: str) -> bool:
        if not self._conn:
            return False

        cursor = self._conn.cursor()
        cursor.execute(
            "UPDATE memories SET status = 'deleted', updated_at = NOW() WHERE memory_id = %s",
            (memory_id,)
        )
        return cursor.rowcount > 0

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        if not self._conn:
            return []

        import psycopg2.extras
        cursor = self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cursor.execute("""
            SELECT * FROM memory_versions
            WHERE memory_id = %s ORDER BY created_at ASC
        """, (memory_id,))
        return [dict(row) for row in cursor.fetchall()]

    def diagnostics(self) -> Dict[str, Any]:
        if not self._conn:
            return {"error": "Not connected"}

        cursor = self._conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM memories")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM memories WHERE status = 'active'")
        active = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT persona_id) FROM memories")
        personas = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(DISTINCT tenant_id) FROM memories")
        tenants_count = cursor.fetchone()[0]

        return {
            "adapter": "postgresql",
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "total_memories": total,
            "active_memories": active,
            "total_personas": personas,
            "total_tenants": tenants_count,
        }
