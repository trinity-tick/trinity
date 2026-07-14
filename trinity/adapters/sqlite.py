"""SQLite storage adapter — single-tenant default backend."""

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .base import StorageAdapter


class SQLiteAdapter(StorageAdapter):
    """SQLite-based storage adapter.

    Default backend for single-tenant deployments.
    Supports persona_id and session_id scoping.
    """

    def __init__(self, db_path: str = "trinity_store.db"):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        """Connect to SQLite database and create tables if needed."""
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._create_tables()

    def disconnect(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None

    def _create_tables(self) -> None:
        cursor = self._conn.cursor()

        # Auto-migrate old schema (pre-v6.37 without persona_id/tenant_id)
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
        if cursor.fetchone():
            cursor.execute("PRAGMA table_info(memories)")
            cols = [row[1] for row in cursor.fetchall()]
            if 'persona_id' not in cols:
                cursor.executescript("""
                    DROP TABLE IF EXISTS memory_versions;
                    DROP TABLE IF EXISTS memories;
                    DROP TABLE IF EXISTS sessions;
                    DROP TABLE IF EXISTS personas;
                    DROP TABLE IF EXISTS tenants;
                """)

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS tenants (
                tenant_id   TEXT PRIMARY KEY,
                name        TEXT NOT NULL UNIQUE,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS personas (
                persona_id  TEXT PRIMARY KEY,
                tenant_id   TEXT REFERENCES tenants(tenant_id),
                name        TEXT NOT NULL,
                metadata    TEXT DEFAULT '{}',
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, name)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                session_id  TEXT PRIMARY KEY,
                persona_id  TEXT REFERENCES personas(persona_id),
                tenant_id   TEXT REFERENCES tenants(tenant_id),
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memories (
                memory_id   TEXT PRIMARY KEY,
                session_id  TEXT REFERENCES sessions(session_id),
                persona_id  TEXT REFERENCES personas(persona_id),
                tenant_id   TEXT REFERENCES tenants(tenant_id),
                content     TEXT NOT NULL,
                role        TEXT DEFAULT 'user',
                importance  REAL DEFAULT 0.5,
                tags        TEXT DEFAULT '[]',
                category    TEXT DEFAULT 'general',
                sha256_hash TEXT,
                status      TEXT DEFAULT 'active',
                version     INTEGER DEFAULT 1,
                created_at  TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS memory_versions (
                version_id   TEXT PRIMARY KEY,
                memory_id    TEXT,
                content      TEXT NOT NULL,
                sha256_hash  TEXT,
                operation    TEXT DEFAULT 'CREATE',
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_memories_persona ON memories(persona_id);
            CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id);
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
        """)
        self._conn.commit()

        # Ensure default tenant exists
        cursor.execute("INSERT OR IGNORE INTO tenants (tenant_id, name) VALUES (?, ?)",
                      ("default", "default"))

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
        conn = self._conn
        if not conn:
            raise RuntimeError("Not connected. Call connect() first.")

        memory_id = f"mem_{uuid.uuid4().hex[:16]}"
        version_id = f"ver_{uuid.uuid4().hex[:12]}"
        if not session_id:
            session_id = f"sess_{uuid.uuid4().hex[:12]}"
        tags_json = json.dumps(tags or [])
        sha256_hash = self._compute_sha256(content)
        now = datetime.now(timezone.utc).isoformat()

        conn.execute("""
            INSERT INTO memories
            (memory_id, session_id, persona_id, tenant_id, content, role,
             importance, tags, category, sha256_hash, status, version, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', 1, ?, ?)
        """, (memory_id, session_id, persona_id, tenant_id, content, role,
              importance, tags_json, category, sha256_hash, now, now))

        conn.execute("""
            INSERT INTO memory_versions
            (version_id, memory_id, content, sha256_hash, operation, created_at)
            VALUES (?, ?, ?, ?, 'CREATE', ?)
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
        conn = self._conn
        if not conn:
            return []

        conditions = ["status = 'active'"]
        params: List[Any] = []

        if persona_id:
            conditions.append("persona_id = ?")
            params.append(persona_id)
        if tenant_id:
            conditions.append("tenant_id = ?")
            params.append(tenant_id)

        where = " AND ".join(conditions)

        # Keyword-based search (in production, replace with vector search)
        like_term = f"%{query}%"
        # NOTE: ? order = [WHERE params..., LIKE, LIKE, LIMIT]
        # CASE uses LIKE directly without ?, to avoid param ordering issues
        full_params = params + [like_term, like_term, top_k]
        cursor = conn.execute(f"""
            SELECT memory_id, content, persona_id, session_id, role,
                   importance, tags, category, created_at,
                   CASE WHEN content LIKE '{like_term}' THEN 0.8 ELSE 0.0 END as score
            FROM memories
            WHERE {where}
              AND (content LIKE ? OR content LIKE ?)
            ORDER BY importance DESC, created_at DESC
            LIMIT ?
        """, full_params)

        results = []
        for row in cursor.fetchall():
            results.append({
                "memory_id": row["memory_id"],
                "content": row["content"],
                "content_preview": row["content"][:100],
                "persona_id": row["persona_id"],
                "session_id": row["session_id"],
                "role": row["role"],
                "importance": row["importance"],
                "tags": json.loads(row["tags"]),
                "category": row["category"],
                "created_at": row["created_at"],
                "score": row["score"],
            })

        return results

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return None

        cursor = conn.execute(
            "SELECT * FROM memories WHERE memory_id = ?", (memory_id,)
        )
        row = cursor.fetchone()
        if not row:
            return None
        return dict(row)

    def get_persona_memories(
        self, persona_id: str, limit: int = 50
    ) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memories
            WHERE persona_id = ? AND status = 'active'
            ORDER BY created_at DESC
            LIMIT ?
        """, (persona_id, limit))
        return [dict(row) for row in cursor.fetchall()]

    def delete_memory(self, memory_id: str) -> bool:
        conn = self._conn
        if not conn:
            return False

        cursor = conn.execute(
            "SELECT memory_id FROM memories WHERE memory_id = ?",
            (memory_id,)
        )
        if not cursor.fetchone():
            return False

        conn.execute(
            "UPDATE memories SET status = 'deleted', updated_at = datetime('now') WHERE memory_id = ?",
            (memory_id,)
        )
        conn.execute("""
            INSERT INTO memory_versions (version_id, memory_id, content, sha256_hash, operation, created_at)
            SELECT ? || '_del', memory_id, content, sha256_hash, 'DELETE', datetime('now')
            FROM memories WHERE memory_id = ?
        """, (memory_id, memory_id))
        conn.commit()
        return True

    def get_version_chain(self, memory_id: str) -> List[Dict[str, Any]]:
        conn = self._conn
        if not conn:
            return []

        cursor = conn.execute("""
            SELECT * FROM memory_versions
            WHERE memory_id = ?
            ORDER BY created_at ASC
        """, (memory_id,))
        return [dict(row) for row in cursor.fetchall()]

    def diagnostics(self) -> Dict[str, Any]:
        conn = self._conn
        if not conn:
            return {"error": "Not connected"}

        cursor = conn.execute("SELECT COUNT(*) as c FROM memories")
        total = cursor.fetchone()["c"]

        cursor = conn.execute(
            "SELECT COUNT(*) as c FROM memories WHERE status = 'active'"
        )
        active = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(DISTINCT persona_id) as c FROM memories")
        personas = cursor.fetchone()["c"]

        cursor = conn.execute("SELECT COUNT(*) as c FROM memory_versions")
        versions = cursor.fetchone()["c"]

        db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "adapter": "sqlite",
            "db_path": self.db_path,
            "db_size_mb": round(db_size / (1024 * 1024), 2),
            "total_memories": total,
            "active_memories": active,
            "total_personas": personas,
            "total_versions": versions,
        }
