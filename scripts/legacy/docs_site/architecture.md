# Architecture

Trinity is designed as a modular, layered system that separates concerns between storage, embedding, retrieval, and API access. This page describes the system architecture, component interactions, and data flow.

---

## High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Client Layer                                │
│  ┌───────────┐  ┌──────────┐  ┌───────────┐  ┌──────────────────┐  │
│  │ LangChain │  │  MCP     │  │  REST     │  │  Python SDK      │  │
│  │ Adapter   │  │  Client  │  │  Client   │  │  (Direct)        │  │
│  └─────┬─────┘  └────┬─────┘  └─────┬─────┘  └───────┬──────────┘  │
└────────┼──────────────┼──────────────┼────────────────┼─────────────┘
         │              │              │                │
         ▼              ▼              ▼                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                         API Layer                                   │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │                    Trinity Engine                             │   │
│  │  ┌────────────┐  ┌──────────────┐  ┌───────────────────┐    │   │
│  │  │ CLI Parser │  │  MCP Server  │  │  REST Server      │    │   │
│  │  └────────────┘  └──────────────┘  └───────────────────┘    │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        Core Engine                                  │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐   │   │
│  │  │  Bridge      │  │  Client      │  │  Anti-Forget    │   │   │
│  │  │  Manager     │  │  (Memory CRUD)│  │  Guard          │   │   │
│  │  └──────────────┘  └──────────────┘  └─────────────────┘   │   │
│  └──────────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Adapter Layer                                 │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │  PostgreSQL      │  │  SQLite          │  │  (Future:        │  │
│  │  (pgvector)      │  │  (Development)   │  │   Chroma, etc.)  │  │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Core Components

### 1. Trinity Engine (`trinity/engine.py`)

The central orchestrator that coordinates all operations. It handles:

- **Session management** — creating and maintaining user sessions
- **Memory operations** — storing, retrieving, updating, and deleting memories
- **Tenant isolation** — enforcing data boundaries between tenants
- **Pipeline orchestration** — coordinating embedding, storage, and retrieval

### 2. Bridge Manager (`trinity/core/bridge.py`)

The bridge layer abstracts the underlying storage backend and provides a unified interface for all CRUD operations. It handles:

- Backend selection and connection pooling
- Schema migration and versioning
- Transaction management with rollback support

### 3. Client (`trinity/core/client.py`)

High-level client that provides the developer-facing API. It wraps the bridge and adds:

- Embedding generation and caching
- Query optimization and result ranking
- Automatic metadata enrichment

### 4. Anti-Forgetting Guard (`trinity/daemon/anti_forgetting_guard.py`)

A background process that prevents memory drift and decay by:

- Periodically reinforcing important memories
- Detecting contradictory memories
- Merging duplicate or overlapping entries

### 5. Prompt Compression Auditor (`trinity/daemon/prompt_compression_auditor.py`)

Optimizes context window usage by:

- Compressing verbose memory entries
- Prioritizing high-relevance memories
- Truncating low-value content

---

## Data Flow

### Memory Storage Flow

```
User/Agent → API Layer → Core Engine → Embedding Model → Vector Store
                                        → Metadata Store (SQL)
                                        → Full-text Index
```

1. **Input** — User or agent submits a memory with content, type, and metadata.
2. **Validation** — The engine validates the payload and tenant context.
3. **Embedding** — Content is passed through the embedding model to generate a vector.
4. **Storage** — The vector is stored in pgvector, while metadata is stored in relational tables.
5. **Indexing** — Full-text search index is updated for hybrid retrieval.

### Memory Retrieval Flow

```
User/Agent → Query → API Layer → Core Engine → Embedding Model → Vector Search
                                                                    ↓
                                         Re-rank ← Hybrid Results ← Full-text Search
                                                                    ↓
                                                              Return Results
```

1. **Query** — User or agent submits a natural language query.
2. **Embedding** — The query is embedded into a vector.
3. **Vector Search** — ANN (Approximate Nearest Neighbor) search finds top-k candidates.
4. **Hybrid Search** — Results are combined with full-text BM25 scores.
5. **Re-ranking** — Results are re-ranked using a weighted combination of vector similarity and text relevance.
6. **Filtering** — Results are filtered by tenant, user, session, memory type, and time range.

---

## Component Diagram

```
┌────────────────────────────────────────────────────────────────┐
│                     Trinity Engine                              │
│                                                                │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌───────────────┐  │
│  │  Store   │  │ Retrieve │  │  Delete  │  │  Search       │  │
│  │  Memory  │  │ Memories │  │  Memory  │  │  (Hybrid)     │  │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └───────┬───────┘  │
│       │              │              │                │          │
│       └──────────────┴──────────────┴────────────────┘          │
│                              │                                   │
│                     ┌────────▼────────┐                         │
│                     │  Bridge Manager │                         │
│                     └────────┬────────┘                         │
│                              │                                   │
│              ┌───────────────┼───────────────┐                  │
│              ▼               ▼                ▼                  │
│        ┌──────────┐   ┌──────────┐    ┌──────────┐             │
│        │PostgreSQL│   │  SQLite  │    │  Future  │             │
│        │ + vector │   │ (Dev)    │    │Backends  │             │
│        └──────────┘   └──────────┘    └──────────┘             │
└────────────────────────────────────────────────────────────────┘
```

---

## Database Schema

### PostgreSQL (Production)

Trinity uses PostgreSQL with the `pgvector` extension for vector storage. The core schema includes:

| Table | Purpose |
|---|---|
| `memories` | Core memory storage with vector, text, and metadata |
| `tenants` | Multi-tenant isolation boundaries |
| `sessions` | Session tracking and context windows |
| `personas` | Persona definitions per user/tenant |
| `embeddings` | Embedding cache and model registry |
| `memory_graph` | Relationship edges between memories |

### SQLite (Development)

For local development and testing, Trinity supports SQLite with a simplified schema that mirrors the PostgreSQL structure without vector support (using in-memory embedding comparison).

---

## Security Boundaries

Trinity enforces strict isolation at multiple levels:

```
Tenant A ───── Tenant Boundary ───── Tenant B
   │                                      │
   ├─ Persona A1                          ├─ Persona B1
   ├─ Persona A2                          ├─ Persona B2
   │                                      │
   ├─ Session A1-1                        ├─ Session B1-1
   ├─ Session A1-2                        ├─ Session B1-2
```

- **Tenant-level** — Complete data isolation with tenant ID scoping on all queries.
- **Persona-level** — Memory access restricted to the owning persona.
- **Session-level** — Optional session-scoped retrieval for context-aware agents.

---

## Next Steps

- **[API Reference](api-reference.md)** — Detailed API documentation.
- **[Multi-Tenant Architecture](multi-tenant.md)** — Deep dive into tenant isolation.
- **[Multimodal Support](multimodal.md)** — How Trinity handles images and audio.
