# Multi-Tenant Architecture

Trinity provides enterprise-grade multi-tenant isolation, ensuring that data from different tenants, users, personas, and sessions never intermix. This page covers the multi-tenant model, configuration, and best practices.

---

## Isolation Model

Trinity implements a four-level isolation hierarchy:

```
Tenant (Organization)
  └── Persona (Role/Identity)
        └── User (Individual)
              └── Session (Conversation Turn)
```

Each level provides progressively finer-grained access control:

| Level | Scope | Purpose |
|---|---|---|
| **Tenant** | Organization-wide | Complete data isolation between customers |
| **Persona** | Role-based | Different memory sets for different agent roles |
| **User** | Individual | Per-user memory profiles |
| **Session** | Conversation | Ephemeral context window for a single interaction |

---

## Tenant Isolation

### How It Works

Every memory stored in Trinity is tagged with a `tenant_id`. All queries include an implicit tenant filter that is enforced at the database level:

```python
# Tenant A stores memories
memory.store(
    user_id="alice",
    content="Alice works on Project Alpha.",
    tenant_id="tenant-acme-corp",
    memory_type="fact"
)

# Tenant B stores memories — completely isolated
memory.store(
    user_id="alice",
    content="Alice works on Project Beta.",
    tenant_id="tenant-other-inc",
    memory_type="fact"
)

# Retrieval only returns memories from the specified tenant
results = memory.retrieve(
    user_id="alice",
    query="What project?",
    tenant_id="tenant-acme-corp"  # Only returns Project Alpha
)
```

### Database-Level Enforcement

Trinity uses PostgreSQL row-level security (RLS) for tenant isolation:

```sql
-- Enable row-level security on the memories table
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;

-- Create a policy that restricts access by tenant_id
CREATE POLICY tenant_isolation ON memories
    USING (tenant_id = current_setting('app.current_tenant')::TEXT);
```

This ensures that even direct SQL queries cannot bypass tenant isolation.

---

## Persona Isolation

Personas allow different "personalities" or roles to maintain separate memory profiles:

```python
# Store memories for different personas
memory.store(
    user_id="alice",
    content="Alice's coding assistant persona prefers verbose explanations.",
    persona_id="coding-assistant",
    memory_type="preference"
)

memory.store(
    user_id="alice",
    content="Alice's writing assistant persona prefers Markdown output.",
    persona_id="writing-assistant",
    memory_type="preference"
)

# Retrieve only for the active persona
results = memory.retrieve(
    user_id="alice",
    query="Output format preferences",
    persona_id="writing-assistant"
)
```

---

## Session Isolation

Sessions provide temporal isolation for conversation turns:

```python
# Session 1: Morning conversation
memory.store(
    user_id="alice",
    content="Planning to deploy the new feature tomorrow.",
    session_id="session-morning",
    memory_type="episodic"
)

# Session 2: Afternoon conversation (separate context)
results = memory.retrieve(
    user_id="alice",
    query="Deployment plans",
    session_id="session-afternoon",  # Won't find the morning memory
    top_k=5
)
```

---

## PostgreSQL Setup

### Step 1: Install PostgreSQL with pgvector

```bash
# Using Docker
docker run -d \
  --name trinity-pg \
  -e POSTGRES_DB=trinity \
  -e POSTGRES_USER=trinity \
  -e POSTGRES_PASSWORD=trinity \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

### Step 2: Configure pgvector

```sql
-- Connect to the database
\c trinity

-- Enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Verify installation
SELECT extname, extversion FROM pg_extension WHERE extname = 'vector';
```

### Step 3: Initialize Schema

Trinity auto-creates the schema on first connection, but you can also initialize manually:

```sql
-- Create the memories table with tenant isolation
CREATE TABLE memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    session_id TEXT,
    persona_id TEXT,
    memory_type TEXT NOT NULL DEFAULT 'general',
    content TEXT NOT NULL,
    embedding vector(1536),
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    ttl TIMESTAMPTZ,
    is_active BOOLEAN DEFAULT TRUE
);

-- Create indexes for performance
CREATE INDEX idx_memories_tenant_user ON memories (tenant_id, user_id);
CREATE INDEX idx_memories_type ON memories (memory_type);
CREATE INDEX idx_memories_created ON memories (created_at DESC);
CREATE INDEX idx_memories_embedding ON memories USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- Enable RLS
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
```

### Step 4: Connection Pooling (Production)

For production deployments, use PgBouncer for connection pooling:

```bash
# docker-compose.yml addition
services:
  pgbouncer:
    image: edoburu/pgbouncer:latest
    environment:
      DB_HOST: trinity-pg
      DB_PORT: 5432
      DB_USER: trinity
      DB_PASSWORD: trinity
      DB_NAME: trinity
      POOL_MODE: transaction
      MAX_CLIENT_CONN: 100
      DEFAULT_POOL_SIZE: 25
    ports:
      - "6432:5432"
```

---

## Configuration

### Environment Variables

| Variable | Description | Default |
|---|---|---|
| `TRINITY_BACKEND` | Storage backend | `postgresql` |
| `TRINITY_DB_HOST` | Database host | `localhost` |
| `TRINITY_DB_PORT` | Database port | `5432` |
| `TRINITY_DB_NAME` | Database name | `trinity` |
| `TRINITY_DB_USER` | Database user | `trinity` |
| `TRINITY_DB_PASSWORD` | Database password | `trinity` |
| `TRINITY_POOL_SIZE` | Connection pool size | `10` |
| `TRINITY_DEFAULT_TENANT` | Default tenant for single-tenant mode | `default` |
| `TRINITY_EMBEDDING_MODEL` | Embedding model name | `text-embedding-ada-002` |
| `TRINITY_EMBEDDING_DIM` | Embedding dimensions | `1536` |

### YAML Configuration

```yaml
# trinity.yaml
backend: postgresql
database:
  host: localhost
  port: 5432
  name: trinity
  user: trinity
  password: trinity
  pool_size: 20
  ssl_mode: require

multi_tenant:
  enabled: true
  default_tenant: default
  rls_enforcement: strict
  tenant_header: X-Tenant-ID

embedding:
  model: text-embedding-ada-002
  dimensions: 1536
  batch_size: 100
  cache_enabled: true

retrieval:
  default_top_k: 10
  hybrid_search: true
  alpha: 0.7
  min_score: 0.0
```

---

## Best Practices

### 1. Always Specify Tenant IDs

Never rely on default values in production. Always explicitly pass `tenant_id`:

```python
# Good — explicit tenant
memory.store(user_id="alice", content="...", tenant_id="acme")

# Bad — assumes default tenant
memory.store(user_id="alice", content="...")
```

### 2. Use Personas for Role-Based Agents

If your system has agents with different roles (e.g., support vs. sales), assign each role a distinct persona ID.

### 3. Implement Session Timeouts

Set TTL on session-scoped memories to prevent context bloat:

```python
memory.store(
    user_id="alice",
    content="Temporary conversation context",
    session_id="session-current",
    ttl_seconds=3600  # Auto-expire after 1 hour
)
```

### 4. Monitor Tenant Storage

Use `get_stats()` to monitor per-tenant storage usage and set quotas:

```python
stats = memory.get_stats(tenant_id="acme")
if stats["total_storage_bytes"] > 1_000_000_000:  # 1 GB limit
    trigger_alert(tenant_id="acme")
```

### 5. Separate Database Instances (High Security)

For maximum isolation, consider separate PostgreSQL instances per tenant:

```python
class TenantRouter:
    def get_memory(self, tenant_id):
        config = TENANT_DATABASES[tenant_id]
        return Trinity(host=config["host"], database=config["db"], ...)
```

---

## Next Steps

- **[Multimodal Memory](multimodal.md)** — Extend memory to images and audio.
- **[Deployment Guide](deployment.md)** — Production deployment with Docker.
- **[API Reference](api-reference.md)** — Complete API documentation.
