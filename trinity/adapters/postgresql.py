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

            # 2026-09 修复：先置 _connected 再建表（否则 _get_conn 在
            # _create_tables 内抛 "not connected"，schema 创建被 except 吞掉
            # —— 新库永远建不出表。旧库表为早年 SQL 迁移所建，未暴露。）
            self._connected = True
            self._create_tables()

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
        -- 2026-09: pgvector 向量通道（融合第 2 步）——embedding 列 + HNSW 索引
        CREATE EXTENSION IF NOT EXISTS vector;

        CREATE TABLE IF NOT EXISTS memories (
            memory_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
            session_id    UUID NOT NULL,
            persona_id    VARCHAR(128) NOT NULL DEFAULT 'default',
            tenant_id     VARCHAR(128) NOT NULL DEFAULT 'default',
            agent_id      VARCHAR(128) NOT NULL DEFAULT 'default',
            content       TEXT NOT NULL,
            content_tsv   tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,  -- 2026-09: 物化 tsvector（检索排序加速，EXECUTION 104.8）
            embedding     vector(1024),          -- 2026-09: pgvector 语义向量（可选，回填后参与向量检索）
            role          VARCHAR(32) NOT NULL DEFAULT 'user',
            importance    DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            tags          TEXT[] DEFAULT '{}',
            category      VARCHAR(128) NOT NULL DEFAULT 'general',
            sha256_hash   VARCHAR(64) NOT NULL,
            status        VARCHAR(32) NOT NULL DEFAULT 'active',
            version       INTEGER NOT NULL DEFAULT 1,
            ttl_seconds   INTEGER,
            last_accessed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            access_count  INTEGER NOT NULL DEFAULT 0,
            importance_score DOUBLE PRECISION NOT NULL DEFAULT 0.0,
            content_hash   VARCHAR(64),
            conflict_group_id UUID,
            is_resolved    BOOLEAN NOT NULL DEFAULT FALSE,
            modality       VARCHAR(32) NOT NULL DEFAULT 'text',
            metadata       JSONB NOT NULL DEFAULT '{}',
            source_uri     TEXT,
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
        CREATE INDEX IF NOT EXISTS idx_memories_agent ON memories(agent_id);
        CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
        CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);
        CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN(tags);
        CREATE INDEX IF NOT EXISTS idx_memories_content_fts
            ON memories USING GIN(to_tsvector('simple', content));
        CREATE INDEX IF NOT EXISTS idx_memories_content_tsv
            ON memories USING GIN(content_tsv);
        -- 2026-09: pgvector HNSW 余弦索引（向量直查通道）
        CREATE INDEX IF NOT EXISTS idx_memories_embedding
            ON memories USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS idx_memories_ttl ON memories(ttl_seconds, created_at);
        CREATE INDEX IF NOT EXISTS idx_memories_last_access ON memories(last_accessed_at);
        CREATE INDEX IF NOT EXISTS idx_memories_modality ON memories(modality);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memories_content_hash
            ON memories(persona_id, agent_id, content_hash)
            WHERE content_hash IS NOT NULL;

        CREATE TABLE IF NOT EXISTS agent_weights (
            agent_id   VARCHAR(128) PRIMARY KEY,
            weight     DOUBLE PRECISION NOT NULL DEFAULT 1.0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS memory_links (
            id         VARCHAR(64) PRIMARY KEY,
            source_id  UUID NOT NULL,
            target_id  UUID NOT NULL,
            link_type  VARCHAR(32) NOT NULL DEFAULT 'semantic',
            strength   DOUBLE PRECISION NOT NULL DEFAULT 0.5,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_memory_links_source
            ON memory_links(source_id);
        CREATE INDEX IF NOT EXISTS idx_memory_links_target
            ON memory_links(target_id);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_links_pair_type
            ON memory_links(source_id, target_id, link_type);

        CREATE TABLE IF NOT EXISTS entities (
            id         VARCHAR(64) PRIMARY KEY,
            name       VARCHAR(512) NOT NULL,
            type       VARCHAR(32) NOT NULL DEFAULT 'concept',
            properties JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

        CREATE TABLE IF NOT EXISTS relations (
            id          VARCHAR(64) PRIMARY KEY,
            subject_id  VARCHAR(64) NOT NULL,
            predicate   VARCHAR(64) NOT NULL DEFAULT 'related_to',
            object_id   VARCHAR(64) NOT NULL,
            properties  JSONB NOT NULL DEFAULT '{}',
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_relations_subject ON relations(subject_id);
        CREATE INDEX IF NOT EXISTS idx_relations_object ON relations(object_id);
        CREATE INDEX IF NOT EXISTS idx_relations_predicate ON relations(predicate);

        CREATE TABLE IF NOT EXISTS audit_log (
            id           VARCHAR(64) PRIMARY KEY,
            memory_id    UUID,
            action       VARCHAR(32) NOT NULL,
            agent_id     VARCHAR(128),
            persona_id   VARCHAR(128),
            timestamp    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            details      JSONB NOT NULL DEFAULT '{}',
            checksum     VARCHAR(64)
        );
        CREATE INDEX IF NOT EXISTS idx_audit_memory ON audit_log(memory_id);
        CREATE INDEX IF NOT EXISTS idx_audit_agent_time ON audit_log(agent_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_persona_time ON audit_log(persona_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_action_time ON audit_log(action, timestamp);

        CREATE TABLE IF NOT EXISTS identity_anchors (
            id          VARCHAR(64) PRIMARY KEY,
            agent_id    VARCHAR(128) NOT NULL,
            anchor_type VARCHAR(64) NOT NULL,
            content     TEXT NOT NULL DEFAULT '{}',
            version     INTEGER NOT NULL DEFAULT 1,
            checksum    VARCHAR(64),
            created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_identity_anchors_agent
            ON identity_anchors(agent_id);
        CREATE INDEX IF NOT EXISTS idx_identity_anchors_type
            ON identity_anchors(agent_id, anchor_type);

        -- DCSA-EJP: 双循环宪法自审计
        CREATE TABLE IF NOT EXISTS audit_runs (
            run_id VARCHAR(64) PRIMARY KEY,
            agent_id VARCHAR(128) NOT NULL,
            task TEXT DEFAULT '',
            executor_result JSONB DEFAULT '{}',
            auditor_result JSONB DEFAULT '{}',
            disagreement_flag BOOLEAN DEFAULT FALSE,
            packet_json JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_audit_runs_agent ON audit_runs(agent_id);
        CREATE INDEX IF NOT EXISTS idx_audit_runs_time ON audit_runs(created_at);

        CREATE TABLE IF NOT EXISTS constitutional_violations (
            violation_id VARCHAR(64) PRIMARY KEY,
            run_id VARCHAR(64) NOT NULL,
            invariant VARCHAR(128) NOT NULL,
            severity VARCHAR(16) NOT NULL DEFAULT 'medium',
            context JSONB DEFAULT '{}',
            timestamp TIMESTAMPTZ DEFAULT NOW(),
            FOREIGN KEY (run_id) REFERENCES audit_runs(run_id)
        );
        CREATE INDEX IF NOT EXISTS idx_violations_run ON constitutional_violations(run_id);
        CREATE INDEX IF NOT EXISTS idx_violations_invariant ON constitutional_violations(invariant);

        -- A2A Protocol: cross-agent task tracking
        CREATE TABLE IF NOT EXISTS a2a_tasks (
            task_id     VARCHAR(64) PRIMARY KEY,
            from_agent  VARCHAR(128) NOT NULL,
            to_agent    VARCHAR(128) NOT NULL,
            payload     JSONB DEFAULT '{}',
            status      VARCHAR(32) DEFAULT 'pending',
            result      JSONB,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_a2a_tasks_from ON a2a_tasks(from_agent);
        CREATE INDEX IF NOT EXISTS idx_a2a_tasks_to ON a2a_tasks(to_agent);
        CREATE INDEX IF NOT EXISTS idx_a2a_tasks_status ON a2a_tasks(status);

        CREATE TABLE IF NOT EXISTS agent_registry (
            agent_id        VARCHAR(128) PRIMARY KEY,
            card_json       JSONB NOT NULL DEFAULT '{}',
            registered_at   TIMESTAMPTZ DEFAULT NOW(),
            last_heartbeat  TIMESTAMPTZ DEFAULT NOW(),
            status          VARCHAR(32) DEFAULT 'active'
        );
        CREATE INDEX IF NOT EXISTS idx_agent_registry_status ON agent_registry(status);

        -- Insert sample data if empty
        -- 2026-09 修复: sha256() 返回 bytea, 转 varchar(64) 会超长(backslash-x 转义) -> encode hex (新库必炸, 旧库表非空未触发)
        INSERT INTO memories (memory_id, session_id, persona_id, tenant_id, content, role, sha256_hash, category)
        SELECT uuid_generate_v4(), uuid_generate_v4(), 'system', 'default', 'Trinity PostgreSQL initialized at ' || NOW(), 'system',
               encode(sha256('Trinity PostgreSQL initialized'), 'hex'), 'system'
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
        agent_id: str = "default",
        role: str = "user",
        importance: float = 0.5,
        tags: Optional[List[str]] = None,
        category: str = "general",
        ttl_seconds: Optional[int] = None,
        modality: str = "text",
        metadata: Optional[Dict[str, Any]] = None,
        source_uri: Optional[str] = None,
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
                    (memory_id, session_id, persona_id, tenant_id, agent_id, content, role,
                     importance, tags, category, sha256_hash, status, version,
                     ttl_seconds, last_accessed_at, access_count, importance_score,
                     content_hash, conflict_group_id, is_resolved,
                     modality, metadata, source_uri,
                     created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', 1,
                            %s, %s::timestamptz, 0, 0.0, %s, NULL, FALSE,
                            %s, %s::jsonb, %s,
                            %s::timestamptz, %s::timestamptz)
                """, (memory_id, session_id, persona_id, tenant_id, agent_id, content, role,
                      importance, json.dumps(tags or [], ensure_ascii=False), category, sha256_hash, ttl_seconds, now,
                      sha256_hash, modality, json.dumps(metadata or {}, ensure_ascii=False),
                      source_uri, now, now))

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
        agent_id: Optional[str] = None,
        app_id: Optional[str] = None,
        session_id: Optional[str] = None,
        category: Optional[str] = None,
        top_k: int = 10,
        **kwargs: Any,
    ) -> List[Dict[str, Any]]:
        import psycopg2.extras

        conditions = ["status = 'active'"]
        params = []

        if persona_id:
            conditions.append("persona_id = %s")
            params.append(persona_id)
        if tenant_id:
            # 2026-08-29: NULL 记忆全局可见（与 SQLite 行为一致——迁移数据 tenant_id 为 NULL）
            conditions.append("(tenant_id = %s OR tenant_id IS NULL)")
            params.append(tenant_id)
        if agent_id:
            conditions.append("(agent_id = %s OR agent_id IS NULL)")
            params.append(agent_id)
        if app_id:
            conditions.append("app_id = %s")
            params.append(app_id)
        if category:
            conditions.append("category = %s")
            params.append(category)

        where = " AND ".join(conditions)
        # 2026-09（EXECUTION 109，pg_jieba 方案 C 应用层落地）：
        # 中文查询优先走 content_tsv_zh（jieba 分词 tsvector，GIN 索引），
        # 排序用 ts_rank；ILIKE OR 降级为兜底（tsv_zh 缺失/空结果时）。
        import jieba as _jb
        _jb.setLogLevel(60)
        _zh_words = []
        try:
            _zh_words = [w.strip() for w in _jb.cut(query)
                         if w.strip() and len(w.strip()) >= 2][:12]
        except Exception:
            _zh_words = []
        # OR 语义（词间 |）：中文长查询任一命中即可（AND 会 0 命中——
        # 实测 '用户偏好 咖啡' AND 无记忆同时含三词）；与向量通道互补。
        # 注意：plainto_tsquery 不识别 |（按空格拆 AND），须用 to_tsquery。
        if _zh_words:
            _tsv_zh_query = " | ".join(_zh_words)
            _tsv_zh_fn = "to_tsquery"
        else:
            _tsv_zh_query = query
            _tsv_zh_fn = "plainto_tsquery"
        _like_clause = "content ILIKE %s"
        _tail_params = [_tsv_zh_query, "%" + query + "%", top_k]
        if len(query) > 8:
            try:
                _words = [w for w in _zh_words if len(w) >= 3][:6]
            except Exception:
                _words = []
            if _words:
                _like_clause = "(content ILIKE %s OR " + " OR ".join(["content ILIKE %s"] * len(_words)) + ")"
                _tail_params = [_tsv_zh_query, "%" + query + "%"] + ["%" + w + "%" for w in _words] + [top_k]
        # params order = SELECT(1) + tsv_zh SELECT(1) + WHERE + tail
        params = [_tsv_zh_query, _tsv_zh_query] + params + _tail_params

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(f"""
                    SELECT *,
                           CASE WHEN content_tsv_zh @@ {_tsv_zh_fn}('simple', %s)
                                THEN ts_rank(content_tsv_zh,
                                             {_tsv_zh_fn}('simple', %s))
                                ELSE 0.1 END as score
                    FROM memories
                    WHERE {where}
                      AND (content_tsv_zh @@ {_tsv_zh_fn}('simple', %s)
                           OR {_like_clause})
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
                        "importance": float(row["importance"]) if row["importance"] is not None else 0.0,  # 2026-09 NULL 防御
                        "tags": row["tags"],
                        "category": row["category"],
                        "modality": row["modality"],
                        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
                        "metadata": row["metadata"] if "metadata" in row.keys() else None,
                        "score": float(row["score"]) if row["score"] else 0.0,
                    })

                # ── 自动 touch：更新搜索命中记忆的访问时间 ──────
                if results:
                    memory_ids = [r["memory_id"] for r in results]
                    now = datetime.now(timezone.utc).isoformat()
                    with conn.cursor() as cur2:
                        cur2.execute("""
                            UPDATE memories
                            SET last_accessed_at = %s::timestamptz,
                                access_count = access_count + 1,
                                updated_at = %s::timestamptz
                            WHERE memory_id = ANY(%s)
                        """, (now, now, memory_ids))
                    conn.commit()

        return results

    # ── 2026-09: pgvector 向量通道（PG 融合第 2 步）──────────────────
    def vector_search(
        self,
        query_vec: Any,
        top_k: int = 10,
        agent_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        tenant_id: Optional[str] = None,
        status: str = "active",
    ) -> List[Dict[str, Any]]:
        """pgvector HNSW 余弦相似检索（embedding <=> query_vec）。

        仅返回已回填 embedding 的记忆；未回填时自然排除（embedding IS NOT NULL）。
        与 search_memories 输出 schema 一致，供引擎 _vector_search 直查。
        """
        import psycopg2.extras
        try:
            import numpy as _np
            vec = _np.asarray(query_vec, dtype=_np.float32).reshape(-1)
        except Exception:
            vec = list(query_vec)
        vec_str = "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"

        conditions = ["status = %s", "embedding IS NOT NULL"]
        # 占位符顺序 = SELECT(1) + WHERE 条件 + ORDER BY(1) + LIMIT(1)
        params: List[Any] = [vec_str]  # SELECT 里的 %s::vector
        cond_params: List[Any] = [status]
        if persona_id:
            conditions.append("persona_id = %s"); cond_params.append(persona_id)
        if tenant_id:
            conditions.append("(tenant_id = %s OR tenant_id IS NULL)"); cond_params.append(tenant_id)
        if agent_id:
            conditions.append("(agent_id = %s OR agent_id IS NULL)"); cond_params.append(agent_id)
        where = " AND ".join(conditions)
        params = params + cond_params + [vec_str, top_k]

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(f"""
                    SELECT memory_id, content, persona_id, session_id, role,
                           importance, tags, category, modality, created_at,
                           1 - (embedding <=> %s::vector) AS score
                    FROM memories
                    WHERE {where}
                    ORDER BY embedding <=> %s::vector
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
                        "importance": float(row["importance"]) if row["importance"] is not None else 0.0,  # 2026-09 NULL 防御
                        "tags": row["tags"],
                        "category": row["category"],
                        "modality": row["modality"],
                        "created_at": row["created_at"].isoformat() if hasattr(row["created_at"], "isoformat") else str(row["created_at"]),
                        "metadata": row["metadata"] if "metadata" in row.keys() else None,
                        "score": float(row["score"]) if row["score"] is not None else 0.0,
                    })
                return results

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
                return [{"memory_id": str(r["memory_id"]), "content": r["content"]} for r in cur.fetchall()]

    def get_memory(self, memory_id: str) -> Optional[Dict[str, Any]]:
        import psycopg2.extras

        with self._get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute("SELECT * FROM memories WHERE memory_id = %s", (memory_id,))
                row = cur.fetchone()
                return dict(row) if row else None

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
                    updates.append("tags = %s")
                    params.append(tags)

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

                conn.commit()

                # Return updated memory
                cur.execute("SELECT * FROM memories WHERE memory_id = %s", (memory_id,))
                row = cur.fetchone()
                return dict(row) if row else None

    # ── Version Chain ─────────────────────────────────────────────

    def archive_memories(self, memory_ids: List[str]) -> int:
        """批量将记忆标记为 archived（衰减压缩回写；与 SQLiteAdapter 同接口）。

        镜像 memory_compressor._archive_originals 的历史裸 SQL 行为。
        """
        if not memory_ids:
            return 0
        count = 0
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for mid in memory_ids:
                    cur.execute(
                        "UPDATE memories SET status = 'archived', "
                        "updated_at = NOW() WHERE memory_id::text = %s",
                        (str(mid),),
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
                return [dict(row) for row in cur.fetchall()]

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
                return [dict(row) for row in cur.fetchall()]

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
                            access_count = access_count + 1,
                            updated_at = %s::timestamptz
                        WHERE memory_id = %s
                    """, (now, now, memory_id))
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

                    cur.execute("""
                        SELECT agent_id, COUNT(*) as cnt FROM memories
                        WHERE status = 'active'
                        GROUP BY agent_id
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

                    cur.execute("""
                        SELECT modality, COUNT(*) as cnt
                        FROM memories
                        WHERE status = 'active'
                        GROUP BY modality
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

                    cur.execute("SELECT COUNT(*) as c FROM memories WHERE conflict_group_id IS NOT NULL AND is_resolved = TRUE")
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

    def upsert_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建或更新实体（幂等：按 name + type 去重）。"""
        import uuid as _uuid
        if not self._connected:
            return {"error": "Not connected"}
        eid = f"ent_{_uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        props_json = json.dumps(properties or {}, ensure_ascii=False)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM entities WHERE name = %s AND type = %s",
                    (name, etype),
                )
                row = cur.fetchone()
                if row:
                    eid = row[0]
                    cur.execute(
                        "UPDATE entities SET properties = %s, created_at = %s "
                        "WHERE id = %s",
                        (props_json, now, eid),
                    )
                else:
                    cur.execute(
                        "INSERT INTO entities (id, name, type, properties, created_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (eid, name, etype, props_json, now),
                    )
                conn.commit()
        return {"id": eid, "name": name, "type": etype,
                "properties": (properties or {}), "created_at": now.isoformat()}

    def get_entity(self, entity_id: str) -> Optional[Dict[str, Any]]:
        """查询实体详情（含关联关系）。"""
        if not self._connected:
            return None
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM entities WHERE id = %s", (entity_id,))
                columns = [desc[0] for desc in cur.description]
                row = cur.fetchone()
                if not row:
                    return None
                entity = dict(zip(columns, row))
                for date_field in ("created_at",):
                    if entity.get(date_field):
                        entity[date_field] = entity[date_field].isoformat()
                entity["properties"] = entity.get("properties", {}) or {}

                cur.execute(
                    "SELECT * FROM relations WHERE subject_id = %s", (entity_id,)
                )
                rcols = [desc[0] for desc in cur.description]
                out_rows = cur.fetchall()
                entity["relations_outgoing"] = [dict(zip(rcols, r)) for r in out_rows]

                cur.execute(
                    "SELECT * FROM relations WHERE object_id = %s", (entity_id,)
                )
                in_rows = cur.fetchall()
                entity["relations_incoming"] = [dict(zip(rcols, r)) for r in in_rows]
        return entity

    def search_entities(self, name: Optional[str] = None,
                        etype: Optional[str] = None,
                        limit: int = 20) -> List[Dict[str, Any]]:
        """搜索实体。"""
        if not self._connected:
            return []
        sql = "SELECT * FROM entities WHERE 1=1"
        params: list = []
        if name:
            sql += " AND name ILIKE %s"
            params.append(f"%{name}%")
        if etype:
            sql += " AND type = %s"
            params.append(etype)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def create_relation(self, subject_id: str, predicate: str,
                        object_id: str,
                        properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建关系（幂等去重）。"""
        import uuid as _uuid
        if not self._connected:
            return {"error": "Not connected"}
        if subject_id == object_id:
            return {"error": "Cannot create self-referencing relation"}
        rid = hashlib.sha256(
            f"{subject_id}:{predicate}:{object_id}".encode()
        ).hexdigest()[:32]
        now = datetime.now(timezone.utc)
        props_json = json.dumps(properties or {}, ensure_ascii=False)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO relations (id, subject_id, predicate, object_id,
                                           properties, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (id) DO NOTHING
                """, (rid, subject_id, predicate, object_id, props_json, now))
                conn.commit()
        return {"id": rid, "subject_id": subject_id, "predicate": predicate,
                "object_id": object_id, "properties": (properties or {}),
                "created_at": now.isoformat()}

    def query_relations(self, subject_id: Optional[str] = None,
                        predicate: Optional[str] = None,
                        object_id: Optional[str] = None,
                        limit: int = 50) -> List[Dict[str, Any]]:
        """查询关系。"""
        if not self._connected:
            return []
        sql = "SELECT * FROM relations WHERE 1=1"
        params: list = []
        if subject_id:
            sql += " AND subject_id = %s"
            params.append(subject_id)
        if predicate:
            sql += " AND predicate = %s"
            params.append(predicate)
        if object_id:
            sql += " AND object_id = %s"
            params.append(object_id)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                columns = [desc[0] for desc in cur.description]
                return [dict(zip(columns, row)) for row in cur.fetchall()]

    def traverse(self, start_id: str, max_hops: int = 3) -> Dict[str, Any]:
        """多跳遍历子图。"""
        if not self._connected:
            return {"nodes": [], "edges": []}
        max_hops = max(1, min(max_hops, 5))
        visited: set = set()
        node_ids: set = {start_id}
        edges: list = []

        with self._get_conn() as conn:
            with conn.cursor() as cur:
                for _hop in range(max_hops):
                    if not node_ids:
                        break
                    visited |= node_ids
                    next_ids: set = set()
                    for nid in node_ids:
                        for col in ("subject_id", "object_id"):
                            other_col = "object_id" if col == "subject_id" else "subject_id"
                            cur.execute(
                                f"SELECT * FROM relations WHERE {col} = %s", (nid,)
                            )
                            rcols = [desc[0] for desc in cur.description]
                            for row in cur.fetchall():
                                r = dict(zip(rcols, row))
                                other = r[other_col]
                                edges.append(r)
                                if other not in visited:
                                    next_ids.add(other)
                    node_ids = next_ids - visited

                all_nodes = set()
                for e in edges:
                    all_nodes.add(e["subject_id"])
                    all_nodes.add(e["object_id"])
                all_nodes.add(start_id)

                node_list: list = []
                for nid in all_nodes:
                    cur.execute("SELECT * FROM entities WHERE id = %s", (nid,))
                    ncols = [desc[0] for desc in cur.description]
                    nrow = cur.fetchone()
                    if nrow:
                        n = dict(zip(ncols, nrow))
                        n["properties"] = n.get("properties", {}) or {}
                        node_list.append(n)

        return {"nodes": node_list, "edges": edges}

    def create_entity(self, name: str, etype: str = "concept",
                      properties: Optional[Dict] = None) -> Dict[str, Any]:
        """创建新实体（非幂等，实体已存在时返回错误）。"""
        if not self._connected:
            return {"error": "Not connected"}
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM entities WHERE name = %s AND type = %s LIMIT 1",
                    (name, etype),
                )
                row = cur.fetchone()
                if row:
                    return {"error": "Entity exists", "entity_id": row[0]}
        return self.upsert_entity(name=name, etype=etype, properties=properties)

    def get_entity_by_name(self, name: str,
                           etype: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """按名称精确匹配单个实体。"""
        if not self._connected:
            return None
        with self._get_conn() as conn:
            with conn.cursor() as cur:
                if etype:
                    cur.execute(
                        "SELECT id FROM entities WHERE name = %s AND type = %s LIMIT 1",
                        (name, etype),
                    )
                else:
                    cur.execute(
                        "SELECT id FROM entities WHERE name = %s LIMIT 1",
                        (name,),
                    )
                row = cur.fetchone()
                if not row:
                    return None
                return self.get_entity(row[0])

    def get_neighbors(self, entity_id: str) -> Dict[str, Any]:
        """获取实体的 1-hop 邻居。"""
        subgraph = self.traverse(entity_id, max_hops=1)
        entity = self.get_entity(entity_id)
        neighbors = []
        nodes_seen = {entity_id}
        if entity:
            entity.pop("relations_outgoing", None)
            entity.pop("relations_incoming", None)
        for node in subgraph.get("nodes", []):
            nid = node.get("id", "")
            if nid != entity_id and nid not in nodes_seen:
                nodes_seen.add(nid)
                neighbors.append(node)
        return {
            "entity": entity,
            "neighbors": neighbors,
            "relations": subgraph.get("edges", []),
        }

    def query_graph(self, query: str, limit: int = 20) -> Dict[str, Any]:
        """通过关键词搜索实体，返回以匹配实体为中心的子图。"""
        matches = self.search_entities(name=query, limit=limit)
        if not matches:
            return {"match_entities": [], "nodes": [], "edges": []}

        all_nodes: dict = {}
        all_edges: dict = {}
        match_entities = []

        for ent in matches:
            ent_copy = dict(ent)
            ent_copy.pop("relations_outgoing", None)
            ent_copy.pop("relations_incoming", None)
            match_entities.append(ent_copy)
            all_nodes[ent["id"]] = ent_copy

            sub = self.traverse(ent["id"], max_hops=1)
            for node in sub.get("nodes", []):
                nid = node.get("id", "")
                if nid not in all_nodes:
                    all_nodes[nid] = node
            for edge in sub.get("edges", []):
                eid = edge.get("id", "")
                if eid not in all_edges:
                    all_edges[eid] = edge

        return {
            "match_entities": match_entities,
            "nodes": list(all_nodes.values()),
            "edges": list(all_edges.values()),
        }

    # ── Audit Log Methods ──────────────────────────────────────────

    def write_audit_log(self, memory_id: str = None, action: str = "",
                         agent_id: str = None, persona_id: str = None,
                         details: dict = None) -> None:
        """向 audit_log 表写入一条审计记录（链式 SHA-256 防篡改）。"""
        if not self._connected:
            return
        import uuid as _uuid
        audit_id = str(_uuid.uuid4())
        details_dict = details or {}

        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    # 获取上一条 checksum
                    cur.execute(
                        "SELECT checksum FROM audit_log ORDER BY timestamp DESC, id DESC LIMIT 1"
                    )
                    prev = cur.fetchone()
                    prev_checksum = prev[0] if prev and prev[0] else ""
                    _now_iso = datetime.now(timezone.utc).isoformat()

                    # 计算链式哈希
                    payload = json.dumps({
                        "id": audit_id,
                        "memory_id": str(memory_id) if memory_id else None,
                        "action": action,
                        "agent_id": agent_id,
                        "persona_id": persona_id,
                        "timestamp": _now_iso,  # 2026-09 (EXECUTION 123): 与 verify 一致
                        "details": details_dict,
                        "prev_checksum": prev_checksum,
                    }, sort_keys=True, ensure_ascii=False)
                    chain_checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()

                    cur.execute("""
                        INSERT INTO audit_log (id, memory_id, action, agent_id, persona_id, details, checksum, timestamp)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        audit_id, memory_id, action, agent_id, persona_id,
                        json.dumps(details_dict), chain_checksum, _now_iso,
                    ))
                conn.commit()
        except Exception as e:
            logger.warning("Failed to write audit log: %s", e)

    def get_audit_trail(self, memory_id: str) -> List[Dict[str, Any]]:
        """查看某条记忆的完整变更历史。"""
        if not self._connected:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, memory_id, action, agent_id, persona_id,
                               timestamp, details, checksum
                        FROM audit_log
                        WHERE memory_id = %s
                        ORDER BY timestamp ASC, id ASC
                    """, (memory_id,))
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
            logger.warning("get_audit_trail failed: %s", e)
            return []

    def replay_agent_session(self, agent_id: str,
                              start_time: str = None,
                              end_time: str = None) -> List[Dict[str, Any]]:
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
                    if start_time:
                        query += " AND timestamp >= %s"
                        params.append(start_time)
                    if end_time:
                        query += " AND timestamp <= %s"
                        params.append(end_time)
                    query += " ORDER BY timestamp ASC, id ASC"
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
        """保存 SAGE 图快照（JSONB 单行，幂等 upsert）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO sage_graph (id, snapshot) VALUES ('graph', %s)
                        ON CONFLICT (id) DO UPDATE SET snapshot = EXCLUDED.snapshot, updated_at = NOW()
                    """, (json.dumps(snapshot, ensure_ascii=False, default=str),))
                conn.commit()
            return True
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
    def context_save(self, last_query: str, percepts: list) -> bool:
        """持久化最近上下文（跨进程/重启保留）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO session_context (id, last_query, percepts)
                        VALUES ('ctx', %s, %s)
                        ON CONFLICT (id) DO UPDATE SET
                            last_query = EXCLUDED.last_query,
                            percepts = EXCLUDED.percepts,
                            updated_at = NOW()
                    """, (last_query, json.dumps(percepts, ensure_ascii=False)))
                conn.commit()
            return True
        except Exception:
            return False

    def context_load(self):
        """读取持久化上下文（无则 None）。"""
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT last_query, percepts FROM session_context WHERE id = 'ctx'")
                    row = cur.fetchone()
                    if not row:
                        return None
                    _p = row[1]
                    if isinstance(_p, str):
                        import json as _j
                        _p = _j.loads(_p)
                    return {"last_query": row[0] or "", "percepts": _p or []}
            return None
        except Exception:
            return None

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

    def verify_audit_integrity(self) -> Dict[str, Any]:
        """验证审计链完整性。"""
        if not self._connected:
            return {"integrity_ok": False, "error": "Not connected"}
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT id, memory_id, action, agent_id, persona_id, "
                        "timestamp, details, checksum "
                        "FROM audit_log ORDER BY timestamp ASC, id ASC"
                    )
                    cols = [desc[0] for desc in cur.description]
                    entries = [dict(zip(cols, row)) for row in cur.fetchall()]

            if not entries:
                return {"integrity_ok": True, "total_entries": 0,
                        "tampered": [], "details": "审计日志为空"}

            tampered = []
            prev_checksum = ""
            for d in entries:
                if hasattr(d["timestamp"], "isoformat"):
                    d["timestamp"] = d["timestamp"].isoformat()
                if d.get("memory_id"):
                    d["memory_id"] = str(d["memory_id"])
                details = d.get("details", {}) or {}
                payload = json.dumps({
                    "id": d["id"],
                    "memory_id": d.get("memory_id"),
                    "action": d["action"],
                    "agent_id": d.get("agent_id"),
                    "persona_id": d.get("persona_id"),
                    "timestamp": d["timestamp"],
                    "details": details,
                    "prev_checksum": prev_checksum,
                }, sort_keys=True, ensure_ascii=False)
                expected = hashlib.sha256(payload.encode("utf-8")).hexdigest()
                if expected != d["checksum"]:
                    tampered.append({"id": d["id"], "expected": expected, "actual": d["checksum"]})
                prev_checksum = d["checksum"]

            return {
                "integrity_ok": len(tampered) == 0,
                "total_entries": len(entries),
                "tampered_count": len(tampered),
                "tampered": tampered,
                "details": "所有审计记录完整一致" if len(tampered) == 0
                            else f"发现 {len(tampered)} 条记录校验和不匹配，可能存在篡改",
            }
        except Exception as e:
            return {"integrity_ok": False, "error": str(e)}

    def get_audit_summary(self, start_time: str = None,
                           end_time: str = None) -> Dict[str, Any]:
        """审计摘要：各操作计数、活跃 Agent、操作峰值时段。"""
        if not self._connected:
            return {"error": "Not connected"}
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    where_clauses = ["1=1"]
                    params: list = []
                    if start_time:
                        where_clauses.append("timestamp >= %s")
                        params.append(start_time)
                    if end_time:
                        where_clauses.append("timestamp <= %s")
                        params.append(end_time)
                    where_sql = " AND ".join(where_clauses)

                    cur.execute(f"SELECT COUNT(*) FROM audit_log WHERE {where_sql}", params)
                    total = cur.fetchone()[0]

                    cur.execute(
                        f"SELECT action, COUNT(*) as c FROM audit_log WHERE {where_sql} GROUP BY action ORDER BY c DESC",
                        params,
                    )
                    action_counts = {row[0]: row[1] for row in cur.fetchall()}

                    cur.execute(
                        f"SELECT agent_id, COUNT(*) as c FROM audit_log WHERE {where_sql} "
                        f"AND agent_id IS NOT NULL GROUP BY agent_id ORDER BY c DESC",
                        params,
                    )
                    active_agents = {row[0]: row[1] for row in cur.fetchall()}

                    cur.execute(
                        f"SELECT persona_id, COUNT(*) as c FROM audit_log WHERE {where_sql} "
                        f"AND persona_id IS NOT NULL GROUP BY persona_id ORDER BY c DESC",
                        params,
                    )
                    active_personas = {row[0]: row[1] for row in cur.fetchall()}

                    cur.execute(
                        f"SELECT TO_CHAR(timestamp, 'YYYY-MM-DD HH24') as hour_bucket, COUNT(*) as c "
                        f"FROM audit_log WHERE {where_sql} GROUP BY hour_bucket ORDER BY c DESC LIMIT 5",
                        params,
                    )
                    peak_hours = [{"hour": row[0], "count": row[1]} for row in cur.fetchall()]

            return {
                "total_entries": total,
                "action_counts": action_counts,
                "active_agents": active_agents,
                "active_personas": active_personas,
                "peak_hours": peak_hours,
                "time_range": {"start": start_time, "end": end_time},
            }
        except Exception as e:
            return {"error": str(e)}

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

    def log_audit_run(self, run_id: str, agent_id: str, task: str,
                       executor_result: str, auditor_result: str,
                       disagreement_flag: bool = False,
                       packet_json: str = "{}") -> bool:
        if not self._connected:
            return False
        try:
            import json as _json
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO audit_runs "
                        "(run_id, agent_id, task, executor_result, auditor_result, disagreement_flag, packet_json) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (run_id) DO UPDATE SET "
                        "auditor_result = EXCLUDED.auditor_result, "
                        "disagreement_flag = EXCLUDED.disagreement_flag, "
                        "packet_json = EXCLUDED.packet_json",
                        (run_id, agent_id, task,
                         _json.dumps(executor_result) if not isinstance(executor_result, str) else executor_result,
                         _json.dumps(auditor_result) if not isinstance(auditor_result, str) else auditor_result,
                         disagreement_flag,
                         _json.dumps(packet_json) if not isinstance(packet_json, str) else packet_json),
                    )
            return True
        except Exception:
            return False

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

    def get_audit_history(self, agent_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        if not self._connected:
            return []
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM audit_runs WHERE agent_id = %s "
                        "ORDER BY created_at DESC LIMIT %s",
                        (agent_id, limit),
                    )
                    cols = [desc[0] for desc in cur.description]
                    return [dict(zip(cols, r)) for r in cur.fetchall()]
        except Exception:
            return []

    def get_audit_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        if not self._connected:
            return None
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM audit_runs WHERE run_id = %s", (run_id,))
                    row = cur.fetchone()
                    if not row:
                        return None
                    cols = [desc[0] for desc in cur.description]
                    result = dict(zip(cols, row))
                    cur.execute(
                        "SELECT * FROM constitutional_violations WHERE run_id = %s ORDER BY timestamp",
                        (run_id,),
                    )
                    vcols = [desc[0] for desc in cur.description]
                    result["violations"] = [dict(zip(vcols, r)) for r in cur.fetchall()]
                    return result
        except Exception:
            return None

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

    def _get_audit_run_count(self) -> int:
        """返回 audit_runs 表记录总数。"""
        if not self._connected:
            return 0
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM audit_runs")
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
