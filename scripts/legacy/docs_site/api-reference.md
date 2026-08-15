# API Reference

Trinity provides four interfaces for interacting with the memory system: a Python SDK, a CLI, an MCP server, and a REST API. This page documents all available operations for each interface.

---

## Python API

### Trinity Class

The `Trinity` class is the primary entry point for all memory operations.

#### Constructor

```python
from trinity import Trinity

memory = Trinity(
    backend: str = "postgresql",
    host: str = "localhost",
    port: int = 5432,
    database: str = "trinity",
    user: str = "trinity",
    password: str = "trinity",
    embedding_model: str = "text-embedding-ada-002",
    embedding_dim: int = 1536,
    table_prefix: str = "",
    pool_size: int = 10,
    max_retries: int = 3,
    timeout_seconds: int = 30,
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `backend` | `str` | `"postgresql"` | Storage backend (`"postgresql"` or `"sqlite"`) |
| `host` | `str` | `"localhost"` | Database host address |
| `port` | `int` | `5432` | Database port |
| `database` | `str` | `"trinity"` | Database name |
| `user` | `str` | `"trinity"` | Database user |
| `password` | `str` | `"trinity"` | Database password |
| `embedding_model` | `str` | `"text-embedding-ada-002"` | Model for generating embeddings |
| `embedding_dim` | `int` | `1536` | Dimensionality of embeddings |
| `table_prefix` | `str` | `""` | Prefix for database table names |
| `pool_size` | `int` | `10` | Connection pool size |
| `max_retries` | `int` | `3` | Maximum retry attempts for transient failures |
| `timeout_seconds` | `int` | `30` | Operation timeout in seconds |

---

#### Core Methods

##### `store()`

Store a memory in the database.

```python
memory_id: str = memory.store(
    user_id: str,
    content: str,
    memory_type: str = "general",
    tenant_id: str | None = None,
    session_id: str | None = None,
    persona_id: str | None = None,
    metadata: dict | None = None,
    embedding: list[float] | None = None,
    ttl_seconds: int | None = None,
)
```

**Parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `user_id` | `str` | Yes | Unique identifier for the user |
| `content` | `str` | Yes | The memory content to store |
| `memory_type` | `str` | No | Type classification (`fact`, `preference`, `episodic`, `conversation`, `general`) |
| `tenant_id` | `str` | No | Tenant identifier for multi-tenant isolation |
| `session_id` | `str` | No | Session identifier for context grouping |
| `persona_id` | `str` | No | Persona identifier for role-based access |
| `metadata` | `dict` | No | Arbitrary key-value metadata |
| `embedding` | `list[float]` | No | Pre-computed embedding vector (auto-generated if omitted) |
| `ttl_seconds` | `int` | No | Time-to-live for ephemeral memories |

**Returns:** `str` — The unique ID of the stored memory.

---

##### `retrieve()`

Retrieve memories relevant to a query.

```python
results: list[MemoryResult] = memory.retrieve(
    user_id: str,
    query: str,
    top_k: int = 10,
    tenant_id: str | None = None,
    session_id: str | None = None,
    persona_id: str | None = None,
    memory_types: list[str] | None = None,
    min_score: float = 0.0,
    include_metadata: bool = True,
    hybrid_search: bool = True,
    alpha: float = 0.7,
    time_range: tuple[datetime, datetime] | None = None,
)
```

**Parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `user_id` | `str` | — | User identifier |
| `query` | `str` | — | Natural language query |
| `top_k` | `int` | `10` | Maximum number of results |
| `tenant_id` | `str` | `None` | Filter by tenant |
| `session_id` | `str` | `None` | Filter by session |
| `persona_id` | `str` | `None` | Filter by persona |
| `memory_types` | `list[str]` | `None` | Filter by memory types |
| `min_score` | `float` | `0.0` | Minimum relevance threshold |
| `include_metadata` | `bool` | `True` | Include metadata in results |
| `hybrid_search` | `bool` | `True` | Enable hybrid vector + text search |
| `alpha` | `float` | `0.7` | Weight for vector similarity (0 = full-text, 1 = vector) |
| `time_range` | `tuple` | `None` | Filter by creation time range |

**Returns:** `list[MemoryResult]` — Ranked list of memory results.

**`MemoryResult` attributes:**

| Attribute | Type | Description |
|---|---|---|
| `id` | `str` | Unique memory identifier |
| `content` | `str` | Memory content text |
| `memory_type` | `str` | Type classification |
| `score` | `float` | Relevance score (0.0 to 1.0) |
| `created_at` | `datetime` | Creation timestamp |
| `metadata` | `dict` | Associated metadata |
| `user_id` | `str` | Owning user |
| `session_id` | `str` | Owning session |

---

##### `delete()`

Delete a memory by ID or filter.

```python
deleted_count: int = memory.delete(
    memory_id: str | None = None,
    user_id: str | None = None,
    tenant_id: str | None = None,
    memory_type: str | None = None,
    before: datetime | None = None,
)
```

**Returns:** `int` — Number of deleted memories.

---

##### `update()`

Update an existing memory.

```python
success: bool = memory.update(
    memory_id: str,
    content: str | None = None,
    metadata: dict | None = None,
    memory_type: str | None = None,
)
```

**Returns:** `bool` — `True` if update succeeded.

---

##### `search()`

Advanced search with custom filters.

```python
results: list[MemoryResult] = memory.search(
    query: str | None = None,
    filters: dict | None = None,
    top_k: int = 10,
    sort_by: str = "relevance",
    sort_order: str = "desc",
)
```

---

##### `get_stats()`

Retrieve memory statistics for a user or tenant.

```python
stats: dict = memory.get_stats(
    user_id: str | None = None,
    tenant_id: str | None = None,
)
```

**Returns:** Dictionary with counts by memory type, total memories, storage size, etc.

---

##### `clear_session()`

Clear all memories for a session.

```python
count: int = memory.clear_session(
    session_id: str,
    tenant_id: str | None = None,
)
```

---

### LangChain Adapter

```python
from trinity.adapters.langchain import TrinityMemory

agent_memory = TrinityMemory(
    trinity_client: Trinity,
    user_id: str,
    session_id: str | None = None,
    persona_id: str | None = None,
    memory_limit: int = 10,
    relevance_threshold: float = 0.3,
)
```

The adapter implements LangChain's `BaseMemory` interface, making it compatible with LangChain agents and chains.

---

## CLI Commands

Trinity comes with a powerful command-line interface.

### Global Options

| Flag | Description |
|---|---|
| `--host` | Database host (default: `localhost`) |
| `--port` | Database port (default: `5432`) |
| `--db` | Database name (default: `trinity`) |
| `--user` | Database user |
| `--password` | Database password |
| `--tenant` | Tenant ID for multi-tenant operations |
| `--json` | Output in JSON format |
| `--verbose` | Enable verbose logging |

### Commands

#### `trinity store`

Store a new memory.

```bash
trinity store --user <user_id> --content "memory text" [options]

Options:
  --type TEXT       Memory type (fact, preference, episodic, conversation)
  --session TEXT    Session ID
  --persona TEXT    Persona ID
  --metadata JSON   JSON metadata string
  --ttl INT         Time-to-live in seconds
```

#### `trinity retrieve`

Retrieve relevant memories.

```bash
trinity retrieve --user <user_id> --query "search query" [options]

Options:
  --top-k INT      Number of results (default: 10)
  --min-score FLOAT Minimum relevance threshold
  --type TEXT       Filter by memory type
  --session TEXT    Filter by session
  --no-hybrid      Disable hybrid search
```

#### `trinity delete`

Delete memories.

```bash
trinity delete [options]

Options:
  --id TEXT         Delete by memory ID
  --user TEXT       Delete all memories for a user
  --type TEXT       Delete by type
  --before DATETIME Delete memories before timestamp
  --force           Skip confirmation prompt
```

#### `trinity users`

List users with memory data.

```bash
trinity users [options]

Options:
  --tenant TEXT     Filter by tenant
  --stats           Show memory statistics per user
```

#### `trinity stats`

Show memory statistics.

```bash
trinity stats [options]

Options:
  --user TEXT       Stats for a specific user
  --tenant TEXT     Stats for a specific tenant
```

#### `trinity chat`

Interactive chat mode with automatic memory management.

```bash
trinity chat --user <user_id> [options]

Options:
  --session TEXT    Session ID
  --persona TEXT    Persona ID
  --model TEXT      LLM model for responses (default: gpt-4)
  --no-memory      Disable memory (ephemeral mode)
```

#### `trinity mcp-server`

Start the MCP server.

```bash
trinity mcp-server [options]

Options:
  --host TEXT       Server host (default: 0.0.0.0)
  --port INT        Server port (default: 8000)
  --workers INT     Number of workers (default: 1)
  --ssl-cert PATH   SSL certificate path
  --ssl-key PATH    SSL key path
```

---

## MCP Server

Trinity implements the [Model Context Protocol (MCP)](https://modelcontextprotocol.io) as a first-class citizen.

### Available MCP Tools

| Tool | Description | Input Parameters |
|---|---|---|
| `trinity_store` | Store a memory | `user_id`, `content`, `memory_type`, `session_id`, `metadata` |
| `trinity_retrieve` | Retrieve memories | `user_id`, `query`, `top_k`, `memory_types` |
| `trinity_delete` | Delete a memory | `memory_id` or `user_id` |
| `trinity_search` | Advanced search | `query`, `filters`, `top_k` |
| `trinity_stats` | Get memory statistics | `user_id`, `tenant_id` |
| `trinity_users` | List users | `tenant_id` |

### MCP Resources

| Resource | Description |
|---|---|
| `trinity://memories/{user_id}` | List all memories for a user |
| `trinity://memory/{memory_id}` | Get a specific memory |
| `trinity://stats/{user_id}` | Get user statistics |
| `trinity://tenants` | List all tenants |

### Example MCP Client

```python
from trinity.mcp.langchain_adapter import TrinityMCPAdapter

# Create an MCP-compatible adapter
adapter = TrinityMCPAdapter(
    server_url="http://localhost:8000",
    user_id="alice"
)

# Use with any MCP-compatible agent framework
result = await adapter.call_tool("trinity_retrieve", {
    "query": "What projects is Alice working on?",
    "top_k": 5
})
```

---

## REST API

When the MCP server is running, it also exposes a REST API.

### Endpoints

#### `POST /api/v1/memories`

Store a new memory.

```json
{
  "user_id": "alice",
  "content": "Alice is working on a machine learning project.",
  "memory_type": "fact",
  "session_id": "session-001",
  "metadata": {"source": "chat", "confidence": 0.9}
}
```

**Response:** `201 Created`

```json
{
  "id": "mem_abc123",
  "status": "stored",
  "created_at": "2026-07-14T22:00:00Z"
}
```

#### `GET /api/v1/memories/{user_id}/retrieve`

Retrieve memories for a user.

**Query Parameters:** `query`, `top_k`, `memory_types`, `min_score`, `session_id`

**Response:** `200 OK`

```json
{
  "results": [
    {
      "id": "mem_abc123",
      "content": "Alice is working on a machine learning project.",
      "memory_type": "fact",
      "score": 0.92,
      "created_at": "2026-07-14T22:00:00Z",
      "metadata": {"source": "chat", "confidence": 0.9}
    }
  ],
  "query_time_ms": 4.2
}
```

#### `DELETE /api/v1/memories/{memory_id}`

Delete a specific memory.

**Response:** `204 No Content`

#### `GET /api/v1/stats/{user_id}`

Get memory statistics for a user.

**Response:** `200 OK`

```json
{
  "user_id": "alice",
  "total_memories": 142,
  "by_type": {
    "fact": 45,
    "preference": 32,
    "episodic": 28,
    "conversation": 37
  },
  "total_storage_bytes": 28456,
  "oldest_memory": "2026-06-01T10:00:00Z",
  "newest_memory": "2026-07-14T22:00:00Z"
}
```

#### `POST /api/v1/clear-session`

Clear all memories for a session.

```json
{
  "session_id": "session-001",
  "tenant_id": "tenant-abc"
}
```

**Response:** `200 OK`

```json
{
  "deleted_count": 37,
  "status": "cleared"
}
```

---

## Error Handling

All APIs return consistent error responses:

```json
{
  "error": {
    "code": "TENANT_MISMATCH",
    "message": "User does not belong to the specified tenant",
    "details": {
      "user_id": "alice",
      "tenant_id": "tenant-xyz"
    }
  }
}
```

### Error Codes

| Code | HTTP Status | Description |
|---|---|---|
| `INVALID_REQUEST` | 400 | Malformed request or missing parameters |
| `UNAUTHORIZED` | 401 | Authentication required |
| `FORBIDDEN` | 403 | Insufficient permissions |
| `NOT_FOUND` | 404 | Memory or resource not found |
| `TENANT_MISMATCH` | 403 | Tenant isolation violation |
| `QUOTA_EXCEEDED` | 429 | Rate limit or storage quota exceeded |
| `BACKEND_ERROR` | 500 | Internal storage backend error |
| `EMBEDDING_FAILED` | 502 | Embedding model returned an error |

---

## Next Steps

- **[Multi-Tenant Setup](multi-tenant.md)** — Configure tenant isolation and PostgreSQL.
- **[Multimodal Usage](multimodal.md)** — Working with images and audio memories.
- **[Deployment](deployment.md)** — Production deployment configuration.
