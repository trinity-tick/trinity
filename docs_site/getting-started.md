# Getting Started

This guide will walk you through installing Trinity and running your first memory-enabled agent in under five minutes.

---

## Prerequisites

- Python 3.10 or later
- pip or uv package manager
- (Optional) Docker and Docker Compose for PostgreSQL backend

---

## Installation

Install Trinity from PyPI:

```bash
pip install trinity-memory
```

Or using uv:

```bash
uv pip install trinity-memory
```

### Verify Installation

```bash
python -c "import trinity; print(trinity.__version__)"
```

### Optional Dependencies

```bash
# Install with PostgreSQL support
pip install trinity-memory[postgres]

# Install with multimodal support
pip install trinity-memory[multimodal]

# Install all extras
pip install trinity-memory[all]
```

---

## 5-Minute Quickstart

### Step 1: Start PostgreSQL (with pgvector)

Use Docker Compose to quickly spin up a PostgreSQL instance with the pgvector extension:

```bash
docker run -d \
  --name trinity-db \
  -e POSTGRES_DB=trinity \
  -e POSTGRES_USER=trinity \
  -e POSTGRES_PASSWORD=trinity \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

### Step 2: Initialize Trinity

```python
from trinity import Trinity

# Connect to the database
memory = Trinity(
    backend="postgresql",
    host="localhost",
    port=5432,
    database="trinity",
    user="trinity",
    password="trinity",
    embedding_model="text-embedding-ada-002"  # or any sentence-transformers model
)

# The database schema is auto-created on first connection
print("Trinity ready!")
```

### Step 3: Store and Retrieve Memories

```python
# Store multiple memory types
memory.store(user_id="alice", content="Alice is a software engineer.", memory_type="fact")
memory.store(user_id="alice", content="Prefers async Python patterns.", memory_type="preference")
memory.store(user_id="alice", content="Mentioned project deadline is next Friday.", memory_type="episodic")

# Retrieve relevant memories
results = memory.retrieve(
    user_id="alice",
    query="What does Alice do and what are her preferences?",
    top_k=10
)

print(f"Found {len(results)} relevant memories:")
for r in results:
    print(f"  [{r.memory_type}] {r.content} (relevance: {r.score:.2f})")
```

### Step 4: Run with LangChain

```python
from langchain.agents import AgentExecutor, create_openai_functions_agent
from langchain_openai import ChatOpenAI
from trinity.adapters.langchain import TrinityMemory

# Create the memory adapter
trinity_memory = TrinityMemory(
    trinity_client=memory,
    user_id="alice",
    session_id="session-001"
)

# Use as a LangChain memory module
# The adapter automatically stores and retrieves context from Trinity
```

---

## Examples

### Example 1: Conversational Agent with Memory

```python
from trinity import Trinity

memory = Trinity(backend="postgresql", host="localhost")

# Simulate a multi-turn conversation
turns = [
    "My name is Bob and I love hiking in the mountains.",
    "I'm planning a trip to Switzerland next month.",
    "I prefer trails that are moderate difficulty.",
    "What was my name and what do I like to do?",
]

for i, turn in enumerate(turns):
    if i < 3:
        # Store what the user said
        memory.store(user_id="bob", content=turn, memory_type="conversation")
    else:
        # Query the memory
        results = memory.retrieve(user_id="bob", query="Who is Bob and what are his interests?", top_k=5)
        for r in results:
            print(f"  → Memory: {r.content}")
```

### Example 2: Using the CLI

```bash
# Store a memory
trinity store --user alice --content "Alice likes Rust for systems programming"

# Retrieve memories
trinity retrieve --user alice --query "What programming languages does Alice like?"

# List all users
trinity users

# Run in interactive mode
trinity chat --user alice
```

### Example 3: Using the MCP Server

```bash
# Start the MCP server
trinity mcp-server --host 0.0.0.0 --port 8000

# In another terminal, use any MCP client
# The server exposes tools for store, retrieve, delete, and search operations
```

---

## Next Steps

- **[Architecture Overview](architecture.md)** — Understand how Trinity works under the hood.
- **[API Reference](api-reference.md)** — Explore all available commands and interfaces.
- **[Deployment Guide](deployment.md)** — Set up Trinity for production use.
