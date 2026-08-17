---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_dc7e2194952711f195a2525400e6dd8f
    ReservedCode1: /FLCnZOji7x6ZJ+M9MJ3IkDLm+5KZ0WfjfwAomcl6CuSTqsJK+YeqFj0Kh2j2y+WPTJDulDaG1CDysjDvM8Trht2zzwHQzL16EU0HaVpjbUN9ZX63AmOUBZYU9EPNRau9Ys88DtS71j8os2IBcuObrohCjcCzrM8EY1u5O8ei2WQqqbsumFI1FTWAO4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_dc7e2194952711f195a2525400e6dd8f
    ReservedCode2: /FLCnZOji7x6ZJ+M9MJ3IkDLm+5KZ0WfjfwAomcl6CuSTqsJK+YeqFj0Kh2j2y+WPTJDulDaG1CDysjDvM8Trht2zzwHQzL16EU0HaVpjbUN9ZX63AmOUBZYU9EPNRau9Ys88DtS71j8os2IBcuObrohCjcCzrM8EY1u5O8ei2WQqqbsumFI1FTWAO4=
---

# Trinity SDK

Standardized Python SDK for the **Trinity Memory System** — a clean, typed HTTP client wrapping the Trinity REST API.

## Installation

```bash
pip install trinity-memory[sdk]
```

Or from source:

```bash
git clone https://github.com/trinity-tick/trinity.git
cd trinity
pip install -e .[sdk]
```

## Quick Start

```python
from trinity.sdk import TrinitySDK

# Context manager — auto-closes the session
with TrinitySDK(base_url="http://localhost:8001") as trinity:
    # Write a memory
    trinity.write("User prefers dark mode", modality="text")

    # Search
    results = trinity.search("dark mode", limit=5)
    for item in results.get("results", []):
        print(item.get("content"))

    # Check health
    health = trinity.health()
    print(f"Status: {health['status']}, Memory count: {health.get('memory_count', 0)}")
```

## API Reference

### Constructor

```python
TrinitySDK(
    base_url="http://localhost:8001",  # Trinity API endpoint
    persona_id="default",             # Default persona
    agent_id="default",               # Default agent namespace
)
```

### Memory Operations

| Method | Description | Returns |
|---|---|---|
| `write(content, ...)` | Ingest a new memory | `{memory_id, version_id, timestamp}` |
| `search(query, ...)` | Semantic search | `{results, pushed_memories}` |
| `read(memory_id)` | Read single memory | Memory dict |
| `list_all(...)` | Paginated listing | `{memories}` |
| `age()` | Trigger TTL cleanup | `{aged_count}` |
| `stats()` | Pool statistics | `{total_memories, expired_count, ...}` |

#### write() parameters

```python
trinity.write(
    content="The memory text",
    modality="text",          # text | code | trace | image_description
    ttl_seconds=3600,         # Optional expiry
    metadata={"key": "val"},  # Optional metadata dict
    source_uri="/path/to/source.py",  # Optional origin URI
)
```

#### search() parameters

```python
trinity.search(
    query="search terms",
    limit=10,                 # Max results
    modality="code",          # Optional filter
    agent_id="file-agent",    # Optional namespace
    ranked=True,              # Enable multi-stage ranking
)
```

### Knowledge Graph

| Method | Description |
|---|---|
| `entities(name=None, type=None)` | Search entities |
| `relations(subject_id, predicate, object_id)` | Query relations |
| `traverse(start_id, max_hops=3)` | Multi-hop traversal |
| `explore(topic)` | Topic-based knowledge card |

### Agent Weights

| Method | Description |
|---|---|
| `weights()` | Get all agent weights |
| `set_weight(agent_id, weight)` | Set agent weight |

### Memory Links

| Method | Description |
|---|---|
| `links(memory_id)` | Get outgoing + incoming links |
| `link(source_id, target_id, link_type)` | Create a link |

### Conflict Management

| Method | Description |
|---|---|
| `conflicts(memory_id)` | View conflict chain |
| `resolve(memory_id, keep_id)` | Resolve conflict |

### Health

| Method | Description |
|---|---|
| `health()` | Server health check → `{status, version, uptime, memory_count, vector_index_size}` |

## Error Handling

All exceptions inherit from `TrinityError`:

| Exception | HTTP Status |
|---|---|
| `ConnectionError` | Network unreachable |
| `AuthenticationError` | 401 |
| `MemoryNotFound` | 404 |
| `DuplicateMemory` | 409 (duplicate hash) |
| `ConflictError` | 409 |
| `ValidationError` | 400 / 422 |

```python
from trinity.sdk import TrinitySDK, TrinityError

try:
    with TrinitySDK() as trinity:
        trinity.read("nonexistent-id")
except MemoryNotFound as e:
    print(f"Not found: {e}")
except TrinityError as e:
    print(f"SDK error ({e.status_code}): {e}")
```

## Data Types

`trinity.sdk.types` provides typed dataclasses for all API structures:

```python
from trinity.sdk.types import Memory, SearchResult, Entity, Relation, Stats, Health

# Parse raw API dicts into typed objects:
mem = Memory.from_dict(api_response)
print(mem.content, mem.modality, mem.importance_score)
```

| Type | Key Fields |
|---|---|
| `Memory` | id, content, modality, metadata, agent_id, persona_id, created_at, ttl_seconds, importance_score, content_hash |
| `SearchResult` | memory, score, layer_scores, pushed |
| `Entity` | id, name, type, properties |
| `Relation` | id, subject_id, predicate, object_id |
| `Stats` | total_memories, expired_count, agent_distribution, modality_distribution |
| `Health` | status, version, uptime, memory_count, vector_index_size |

## Context Manager

```python
with TrinitySDK(base_url="http://localhost:8001") as trinity:
    # Session reused across calls
    trinity.write("first")
    trinity.write("second")
# Session auto-closed on exit
```

Without context manager, call `.close()` when done:

```python
sdk = TrinitySDK()
sdk.write("hello")
sdk.close()
```
*（内容由AI生成，仅供参考）*
