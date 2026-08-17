-- ============================================================
-- Trinity PostgreSQL Initialization Script
-- Generated: 2026-08-07
-- Purpose:   Create memory schema aligned with
--            trinity/adapters/postgresql.py PostgreSQLAdapter._create_tables()
-- ============================================================

-- Extensions
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ============================================================
-- 1. tenants
-- ============================================================
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   VARCHAR(128) PRIMARY KEY,
    name        VARCHAR(256) NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2. personas
-- ============================================================
CREATE TABLE IF NOT EXISTS personas (
    persona_id  VARCHAR(128) PRIMARY KEY,
    tenant_id   VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id),
    name        VARCHAR(256) NOT NULL,
    metadata    JSONB DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 3. sessions
-- ============================================================
CREATE TABLE IF NOT EXISTS sessions (
    session_id  VARCHAR(128) PRIMARY KEY,
    persona_id  VARCHAR(128) NOT NULL REFERENCES personas(persona_id),
    tenant_id   VARCHAR(128) NOT NULL REFERENCES tenants(tenant_id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 4. memories (primary storage — 29 rows migrated)
-- ============================================================
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

-- ============================================================
-- 5. memory_versions (version chain — 13 rows migrated)
-- ============================================================
CREATE TABLE IF NOT EXISTS memory_versions (
    version_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    memory_id     UUID NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    content       TEXT NOT NULL,
    sha256_hash   VARCHAR(64) NOT NULL,
    operation     VARCHAR(32) NOT NULL DEFAULT 'CREATE',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 6. audit_log
-- ============================================================
CREATE TABLE IF NOT EXISTS audit_log (
    id              SERIAL PRIMARY KEY,
    action          VARCHAR(64) NOT NULL,
    memory_id       UUID REFERENCES memories(memory_id) ON DELETE SET NULL,
    persona_id      VARCHAR(128),
    content_hash    VARCHAR(64),
    metadata        JSONB DEFAULT '{}'::jsonb,
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- Indexes (aligned with PostgreSQLAdapter._create_tables)
-- ============================================================

-- Persona / tenant / status lookups
CREATE INDEX IF NOT EXISTS idx_memories_persona ON memories(persona_id);
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);

-- Time-based queries
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);

-- Importance ranking
CREATE INDEX IF NOT EXISTS idx_memories_importance ON memories(importance DESC);

-- Tag array search
CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN(tags);

-- Full-text search (pg_trgm + GIN tsvector)
CREATE INDEX IF NOT EXISTS idx_memories_content_fts
    ON memories USING GIN(to_tsvector('simple', content));

-- memory_versions lookup
CREATE INDEX IF NOT EXISTS idx_memory_versions_memid
    ON memory_versions(memory_id);

-- audit_log lookups
CREATE INDEX IF NOT EXISTS idx_audit_log_persona ON audit_log(persona_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp ON audit_log(timestamp DESC);

-- ============================================================
-- Seed: default tenant
-- ============================================================
INSERT INTO tenants (tenant_id, name)
VALUES ('default', 'Default Tenant')
ON CONFLICT (tenant_id) DO NOTHING;

-- ============================================================
-- Done
-- ============================================================
DO $$
BEGIN
    RAISE NOTICE 'Trinity PostgreSQL schema initialized successfully.';
END $$;
