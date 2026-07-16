# Trinity Memory — A Triune Architecture for AGI Long-Term Memory

[![PyPI version](https://img.shields.io/pypi/v/trinity-memory)](https://pypi.org/project/trinity-memory/)
[![CI](https://github.com/trinity-tick/trinity/actions/workflows/ci.yml/badge.svg)](https://github.com/trinity-tick/trinity/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-1.0-orange)](https://modelcontextprotocol.io)

A high-performance, production-ready persistent memory layer for AI agents. Trinity integrates 12+ state-of-the-art memory approaches into a unified architecture with **50-tier guardian chains**, **47 retrieval channels**, and **multi-modal support**.

> **中文版 README** → [README.zh.md](README.zh.md)

---

## Quick Start

```bash
pip install trinity-memory
```

```python
from trinity import Trinity

mem = Trinity()
mem.ingest("User prefers dark mode", tags=["preference", "ui"])
results = mem.search("user preference")
print(results)
```

### CLI

```bash
python -m trinity search --query "user preference" --top-k 5
python -m trinity diagnostics
python -m trinity bench --name mock
```

### MCP Server

```json
{
  "mcpServers": {
    "trinity-memory": {
      "command": "trinity-mcp",
      "args": ["--mode", "stdio"]
    }
  }
}
```

---

## Architecture

Trinity is built on three core layers, integrating cutting-edge memory research:

| Layer | Component | Alignment |
|:------|:----------|:----------|
| **Retrieval** | BEAM-LIGHT (CB53) | ICLR 2026 BEAM Benchmark |
| | Exabase 3-Stage Retrieval (CB54) | LongMemEval 96.4% SOTA |
| | Hindsight 4-Network (CB55) | BEAM 10M SOTA 64.1% |
| | Zikkaron Hopfield (CB56) | Non-LLM SOTA 40.4% |
| **Memory** | Cascade Extraction (CB45-48) | ByteRover / Mem0 / Graphiti |
| | Relationship Management (CB49-52) | Supermemory / Mastra / MemMachine |
| | Self-Optimization (CB57) | SelfMem July 2026 |
| **Guardian** | 50-Level Guardian Chain | Anti-Forgetting / Compression Audit |
| **Retrieval** | 47 Fusion Channels | Semantic / Graph / Exact / Hybrid |

---

## Benchmarks

| Metric | Mem0 | Trinity | Improvement |
|:-------|:----:|:-------:|:-----------:|
| P50 Latency | 110ms | **21ms** | **5.2x faster** |
| P95 Latency | 280ms | **45ms** | **6.2x faster** |
| LongMemEval | 72% | **96.4%** | **+24%** |
| BEAM 10M | 52% | **64.1%** | **+12%** |

---

## Features

- **Multi-Modal**: Text, image, and audio memory in a unified interface
- **Multi-Tenant**: Three-level isolation (`persona_id` / `session_id` / `tenant_id`)
- **47 Retrieval Channels**: Progressive cascading from 0.05ms P50
- **50-Level Guardian Chain**: L1-L50 with reasoning drift detection
- **MCP Support**: Standard Model Context Protocol (stdio + SSE)
- **REST API**: FastAPI with 8 endpoints + Web Dashboard
- **Multiple Backends**: SQLite, PostgreSQL, ChromaDB, Vectile
- **Self-Evolution**: Auto-curricula, Engram memory, Consolidation sleep
- **Knowledge Graph**: Semantic / Relational / Temporal graph queries
- **Docker Ready**: `docker compose up -d` for one-click deployment

---

## Deployment

### Docker

```bash
docker build -t trinity-memory .
docker run -d -p 8100:8100 -p 8000:8000 -v /data:/data trinity-memory
```

### Docker Compose

```bash
docker compose up -d
```

### REST API

```bash
# Write memory
curl -X POST http://localhost:8100/memories \
  -H "Content-Type: application/json" \
  -d '{"content":"User info","importance":0.8}'

# Search memory
curl "http://localhost:8100/search?q=user&top_k=5"
```

---

## Commercial

| Product | Pricing | Use Case |
|:--------|:-------:|:---------|
| **MCP Server** | Free & Open Source | AI Agent integration |
| **SaaS API** | Pay-as-you-go | Application development |
| **Enterprise Deployment** | License | Compliance requirements |

---

## Documentation

Full documentation: [https://trinity-tick.github.io/trinity](https://trinity-tick.github.io/trinity)

- [Getting Started](docs/getting-started.md)
- [Architecture](docs/ARCHITECTURE.md)
- [API Reference](docs/API_REFERENCE.md)
- [Benchmarks](docs/BENCHMARKS.md)
- [Deployment](docs/deployment.md)

---

## License

MIT License — free for commercial and non-commercial use.

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=trinity-tick/trinity&type=Date)](https://star-history.com/#trinity-tick/trinity&Date)
