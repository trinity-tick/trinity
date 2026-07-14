# Trinity Documentation

<div align="center">

![Python Version](https://img.shields.io/badge/python-3.10%2B-blue)
[![License](https://img.shields.io/badge/license-Apache%202.0-green)](https://github.com/agentic-ai/trinity/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/trinity-memory)](https://pypi.org/project/trinity-memory/)
[![Build Status](https://img.shields.io/github/actions/workflow/status/agentic-ai/trinity/ci.yml?branch=main)](https://github.com/agentic-ai/trinity/actions)
[![Code Coverage](https://img.shields.io/codecov/c/github/agentic-ai/trinity)](https://codecov.io/gh/agentic-ai/trinity)
[![Downloads](https://img.shields.io/pypi/dm/trinity-memory)](https://pypi.org/project/trinity-memory/)
[![Docker Pulls](https://img.shields.io/docker/pulls/agenticai/trinity)](https://hub.docker.com/r/agenticai/trinity)

**A persistent, long-term memory layer for AI agents — with multi-modal, multi-tenant, and MCP-native support.**

</div>

---

## Overview

Trinity is an open-source memory engine designed to give AI agents **persistent, long-term recall**. Unlike ephemeral context windows, Trinity stores structured memories across sessions, users, and modalities — enabling agents to remember facts, preferences, conversations, images, and audio over indefinite time spans.

Built for the modern agentic AI stack, Trinity integrates natively with:

- **LangChain / LangGraph** — via a dedicated adapter
- **MCP (Model Context Protocol)** — as a first-class memory server
- **REST API** — for any HTTP-capable agent framework
- **CLI** — for scripting and automation

### Key Features

| Feature | Description |
|---|---|
| **🧠 Persistent Memory** | Stores facts, beliefs, preferences, and conversation summaries across sessions |
| **🔍 Semantic Search** | Retrieves relevant memories using vector embeddings with hybrid search |
| **🏢 Multi-Tenant** | Full isolation by tenant, session, and persona — data never leaks between tenants |
| **🖼️ Multimodal** | Supports image, audio, and text memories with cross-modal retrieval |
| **⚙️ MCP Native** | First-class MCP server implementation for seamless agent integration |
| **🐳 Docker Ready** | One-command deployment with docker-compose and PostgreSQL |
| **📊 Benchmark Suite** | Built-in latency profiling, concurrency testing, and evaluation tools |

---

## Quick Start

```python
from trinity import Trinity

# Initialize with a PostgreSQL backend
memory = Trinity(
    backend="postgresql",
    host="localhost",
    port=5432,
    database="trinity",
    user="trinity",
    password="trinity"
)

# Store a memory
memory.store(
    user_id="alice",
    content="Alice prefers concise responses and Python over JavaScript.",
    memory_type="preference",
    metadata={"source": "conversation", "confidence": 0.95}
)

# Retrieve relevant memories
results = memory.retrieve(
    user_id="alice",
    query="What language does Alice prefer?",
    top_k=5
)

for r in results:
    print(f"[{r.memory_type}] {r.content} (score: {r.score:.2f})")
```

---

## Why Trinity?

Most AI agents today suffer from **amnesia** — they forget everything between turns. Trinity solves this by providing:

1. **Persistence** — Memories survive restarts, redeployments, and service migrations.
2. **Isolation** — Multi-tenant architecture ensures complete data separation.
3. **Multi-modality** — Store and search across text, images, and audio in a unified index.
4. **Speed** — Sub-10ms retrieval latency with PostgreSQL + pgvector.
5. **Standards** — Built on open protocols (MCP, REST, SQL) — no vendor lock-in.

---

## Next Steps

- **[Getting Started](getting-started.md)** — Install Trinity and run your first example in 5 minutes.
- **[Architecture](architecture.md)** — Understand the system design, components, and data flow.
- **[API Reference](api-reference.md)** — Complete documentation for Python, CLI, MCP, and REST APIs.
- **[Deployment](deployment.md)** — Docker, docker-compose, and production configuration guide.
