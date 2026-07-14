-- Trinity PostgreSQL Initialization Script
-- Run automatically on first container start

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Users / Tenants
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name        VARCHAR(255) NOT NULL UNIQUE,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    is_active   BOOLEAN DEFAULT TRUE
);

-- Personas (per-tenant user profiles)
CREATE TABLE IF NOT EXISTS personas (
    persona_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID REFERENCES tenants(tenant_id),
    name        VARCHAR(255) NOT NULL,
    metadata    JSONB DEFAULT '{}',
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(tenant_id, name)
);

-- Sessions (per-persona conversation sessions)
CREATE TABLE IF NOT EXISTS sessions (
    session_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    persona_id  UUID REFERENCES personas(persona_id),
    tenant_id   UUID REFERENCES tenants(tenant_id),
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW(),
    metadata    JSONB DEFAULT '{}'
);

-- Memories (core memory store)
CREATE TABLE IF NOT EXISTS memories (
    memory_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id  UUID REFERENCES sessions(session_id),
    persona_id  UUID REFERENCES personas(persona_id),
    tenant_id   UUID REFERENCES tenants(tenant_id),
    content     TEXT NOT NULL,
    role        VARCHAR(50) DEFAULT 'user',
    importance  REAL DEFAULT 0.5,
    tags        TEXT[] DEFAULT '{}',
    category    VARCHAR(100) DEFAULT 'general',
    sha256_hash VARCHAR(64),
    status      VARCHAR(20) DEFAULT 'active',
    version     INTEGER DEFAULT 1,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Version chain (for audit/provenance)
CREATE TABLE IF NOT EXISTS memory_versions (
    version_id   UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    memory_id    UUID REFERENCES memories(memory_id),
    content      TEXT NOT NULL,
    sha256_hash  VARCHAR(64),
    operation    VARCHAR(20) DEFAULT 'CREATE',
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_memories_tenant ON memories(tenant_id);
CREATE INDEX IF NOT EXISTS idx_memories_persona ON memories(persona_id);
CREATE INDEX IF NOT EXISTS idx_memories_session ON memories(session_id);
CREATE INDEX IF NOT EXISTS idx_memories_created ON memories(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_tags ON memories USING GIN(tags);
CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);

-- Default tenant
INSERT INTO tenants (name) VALUES ('default')
ON CONFLICT (name) DO NOTHING;
