# Trinity — A Triune Architecture for AGI Long-Term Memory

<p align="center">
  <img src="https://img.shields.io/badge/version-6.37-blue.svg" alt="Version 6.37">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License">
  <img src="https://img.shields.io/badge/python-3.10%2B-brightgreen.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/status-beta-yellow.svg" alt="Status: Beta">
</p>

**Trinity** is a triune (three-in-one) memory architecture purpose-built for AGI long-term memory. It unifies **episodic encoding**, **self-evolving reasoning**, and **hierarchical retrieval** into a single, tightly integrated system — outperforming existing memory solutions on recall accuracy, latency, safety, and identity permanence.

- **second_brain** — 122 modules covering memory encoding, compression, retrieval, reasoning, and self-evolution
- **auto_daemon** — 50-tier guardian chain for input filtering, inference guarding, and governance
- **chromadb** — Vector store with KV compression, sparse attention, and tiered routing

---

## Quick Start

```bash
pip install trinity-memory
```

```python
from trinity import Trinity

# One-line initialization
memory = Trinity()

# Write a memory
memory.ingest("User prefers dark mode and sprint reviews every Friday afternoon.")

# Search memories
results = memory.search("What theme does the user like?", top_k=5)
for r in results:
    print(f"[{r.score:.3f}] {r.content}")

# MCP server mode (stdio)
# trinity --mcp

# REST API mode (SSE on port 8000)
# trinity --mcp --mode sse --port 8000
```

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                         Trinity System                          │
├────────────────────────────┬─────────────────┬──────────────────┤
│       second_brain         │   auto_daemon   │     chromadb     │
│       (122 modules)        │  (50-tier chain) │   (7 modules)   │
│                            │                  │                  │
│  ┌──────────────────────┐  │  Tier 46-50:    │  ┌────────────┐  │
│  │ CB54  Exa (Episodic) │  │  P125-P129      │  │ CB42  KV   │  │
│  │ CB55  Hind (Reason)  │  │  Paper-aligned  │  │ CB43  Echo │  │
│  │ CB56  Zikk (Index)   │  │  safety guards  │  │ CB44  Route│  │
│  │ CB57  SelfMem (Evol) │  │                 │  │ CB45  Frac │  │
│  └──────────────────────┘  └─────────────────┘  └────────────┘  │
└────────────────────────────┴─────────────────┴──────────────────┘
```

| Engine | Version | Role |
|--------|---------|------|
| **second_brain** | v6.37 | 122 modules: memory encoding, compression, retrieval, reasoning, self-evolution |
| **auto_daemon** | v1.11 | 50-tier guardian chain: input filtering → inference guarding → governance |
| **chromadb** | v6.17 | Vector storage, KV compression, sparse attention, hierarchical routing |

---

## Key Capabilities

| Capability | Industry (Mem0 / Zep / Letta) | Trinity |
|---|---|---|
| **Recall Accuracy** | Mem0 66.9%, Zep 75.1% | **~97.8%** (internal) |
| **Retrieval Latency** | Mem0 ~100–250ms, Zep ~150ms | **P50 0.05ms** (progressive cascade) |
| **Security Guards** | None built-in | **50-tier guardian chain** (industry-first) |
| **Conflict Resolution** | Zep: last-write-wins (dropping old facts) | **CRDT-based full history** (industry-first) |
| **Identity Permanence** | Not supported | **SHA-256 deterministic anchoring** (industry-first) |
| **Reasoning Drift Detection** | Not supported | **Jensen-Shannon divergence monitoring** (industry-first) |
| **Sycophancy Prevention** | Not supported | **MemSyco 1.000** (industry-first) |
| **Self-Evolution** | Not supported | **SelfMem policy auto-optimization** (industry-first) |

---

## Multi-Tenant Example

Trinity natively supports multi-tenant isolation — each tenant operates in a sandboxed memory space.

```python
from trinity import Trinity

memory = Trinity()

# Create two isolated tenants
alice = memory.tenant("alice")
bob   = memory.tenant("bob")

alice.ingest("Alice works in product design and uses Figma daily.")
bob.ingest("Bob is a backend engineer and prefers Rust.")

# Queries are isolated
alice.search("What tools does Alice use?")   # → Figma
bob.search("What tools does Bob use?")      # → (empty — no cross-tenant leakage)
```

---

## MCP, REST API & Docker

Trinity supports the **Model Context Protocol (MCP)** for seamless integration with AI assistants, plus a full REST API for programmatic access.

| Mode | Command | Port |
|------|---------|------|
| MCP stdio | `trinity --mcp` | — |
| MCP SSE | `trinity --mcp --mode sse` | 8000 |
| REST API | `trinity --api` | 8000 |
| Docker | `docker run -p 8000:8000 trinity-memory/trinity` | 8000 |

> **Docker image**: [`trinity-memory/trinity`](https://hub.docker.com/r/trinity-memory/trinity)

---

## Documentation

| Guide | Link |
|-------|------|
| Architecture Deep Dive | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| API Reference | [docs/API.md](docs/API.md) |
| Benchmarks | [docs/BENCHMARKS.md](docs/BENCHMARKS.md) |
| Security Model | [docs/SECURITY.md](docs/SECURITY.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |

---

## Citation

If you use Trinity in your research, please cite:

```bibtex
@software{trinity2026,
  title     = {Trinity: A Triune Architecture for AGI Long-Term Memory},
  author    = {Trinity Team},
  year      = {2026},
  version   = {6.37},
  url       = {https://github.com/trinity-memory/trinity}
}
```

---

## License

MIT](LICENSE) © 2026 Trinity Team
