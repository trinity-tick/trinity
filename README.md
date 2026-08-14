---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_55465776963511f1ac84525400f8a581
    ReservedCode1: T6Zs5aQIQbXYrY63vpU17HOWXH9UPKYV6kFf+yH54Bbv/kWJumqkdUD0QBUUfKQfNi6XpnGeJ3WeYpETaLFqRGhZOADTXx0/QOpLHh+8KWibwlYin3dHi6MtUW4iMdGQIla5OcH0ZoOxRDC7Yix8BUbjfuTVXtNxWh2TzU7OMCCG5BD+EWNwKqtENs4=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_55465776963511f1ac84525400f8a581
    ReservedCode2: T6Zs5aQIQbXYrY63vpU17HOWXH9UPKYV6kFf+yH54Bbv/kWJumqkdUD0QBUUfKQfNi6XpnGeJ3WeYpETaLFqRGhZOADTXx0/QOpLHh+8KWibwlYin3dHi6MtUW4iMdGQIla5OcH0ZoOxRDC7Yix8BUbjfuTVXtNxWh2TzU7OMCCG5BD+EWNwKqtENs4=
---

# Trinity — Memory Operating System

> **v8.2.0** — Multi-Agent Shared Memory with 50-Layer Guardian Chain and Brain-Inspired Architecture

Trinity is not a "memory library." It is a **Memory Operating System** — an
infrastructure layer that any memory store (vector DB, graph DB, SQLite) can
plug into, with identity, RBAC, auditing, and economic protocols on top.

---

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                    Agent Layer                        │
│  Multi-Agent • A2A v0.3 • 4 Anchor Identity          │
├──────────────────────────────────────────────────────┤
│                  Governance Layer                     │
│  RBAC (6 roles) • 50-Layer Guardian Chain • DCSA-EJP │
├──────────────────────────────────────────────────────┤
│                  Memory Layer                         │
│  7-channel retrieval • FTS5+jieba • Causal Graph     │
│  Self-Evolving • Federated • Multimodal              │
├──────────────────────────────────────────────────────┤
│                  Storage Layer                        │
│  SQLite/PostgreSQL • Vector Index • Graph DB          │
│  Loihi 2 / TrueNorth (neuromorphic)                  │
├──────────────────────────────────────────────────────┤
│                  Economic Layer                       │
│  TrustExchange — Memory Trading Market               │
└──────────────────────────────────────────────────────┘
```

**22 sub-packages, 505+ Python files, 227K+ lines, 135+ API endpoints.**

---

## Quick Start

```bash
# Install from source
cd trinity
pip install -e .

# Verify
python -c "import trinity; print(trinity.__version__)"
# → 8.2.0

# Run all self-tests (208 pass)
python scripts/run_all_self_tests.py
```

### Docker (4 container stack)

```bash
docker-compose up -d
# trinity-mcp  :8000
# trinity-api  :8005
# trinity-db   :5430
# trinity-dash :3000
```

---

## Key Features

| Feature | Status | Notes |
|---------|--------|-------|
| **Multi-Agent Shared Memory** | ✅ | RBAC 6 roles, scope enforcement at write time |
| **50-Layer Guardian Chain** | ✅ | Injection → Sandbox → Audit → Sanitize → Self-heal |
| **BM25 + Jieba Chinese Retrieval** | ✅ | CJK auto-detect, FTS5 with COALESCE fallback |
| **GraphQL API (Strawberry)** | ✅ | Query/Mutation/Subscription, 6/6 integration PASS |
| **Raft Consensus Cluster** | ✅ | 100/100 writes, 4/4 checks, 3-node local |
| **Causal Reasoning** | ✅ | CausalGraph + Counterfactual Engine |
| **Neuromorphic Chip Adapter** | ✅ | Loihi 2 (SNN), TrueNorth (<100mW) |
| **TrustExchange Market** | ✅ | On-chain hash audit, KYC/AML compliance |
| **129-Paper Brain Alignment** | ✅ | P1–P129 in `second_brain` (304 modules) |
| **Self-Evolving Memory** | ✅ | EvolutionScheduler + SelfHealingPipeline |
| **Federated Memory** | ✅ | FederatedAggregator + PrivacyBudget |
| **CI/CD Pipeline** | ✅ | GitHub Actions, Makefile, 30s timeout |
| **SDK (Python / TypeScript / Go)** | ✅ | Three languages, read-only interfaces |

---

## API Overview

### REST (135+ endpoints)

```
GET  /health              → {"status": "ok", "version": "8.2.0"}
POST /memories            → Create memory
GET  /memories/{id}       → Retrieve memory
GET  /agents              → List agents
GET  /dashboard           → Dashboard data
GET  /benchmark           → Benchmark results
```

### GraphQL

```graphql
# Health
query { health { status version uptimeSeconds componentStatus } }

# Memory Search (BM25 + jieba)
query { searchMemories(query: "machine learning", topK: 5) { score memory { memoryId content } } }

# Agents
query { agents { agentId name status } }

# Diagnostics
query { diagnostics { component health latencyMs errorRate } }
```

---

## Benchmark Scores

> 完整实测与官方参考口径见 [docs_site/benchmarks.md](docs_site/benchmarks.md)（2026-08-14 统一版，Trinity v8.2.0）。

| Benchmark | Score | Dataset | Conditions |
|-----------|:-----:|---------|------------|
| LongMemEval (simulated) | R@5 = **0.9818** | 55 questions, self-built | BM25 + jieba, multi-term merge |
| LongMemEval-style (500q) | R@5 / MRR（见 benchmarks.md） | 500 questions, community mock | FTS5 keyword, 6 categories |
| SQuAD v1.1 (adapted) | R@5 = **98.3%** (177/180) | SQuAD v1.1 dev, 180 questions | BM25/FTS5 retrieval → passage selection |
| LoCoMo (subset) | R@5 = **0.88**, MRR = **0.5353** | 38 self-built questions, session-aggregate | 官方 1982 题集本环境网络不可达 |
| BEAM Scale | R@5 = **1.000**（1K/10K/100K 见 benchmarks.md） | 50 queries × scale | PostgreSQL FTS |
| GraphQL Load Test | p50=**2.06ms**, p99=**29.25ms** | 100 QPS, 20 workers | Strawberry execute_sync, 0 errors |
| Cluster Stress | **5/5 checks**（单 leader 已修复） | 3-node Raft, multi-process | Exactly 1 leader, commit advanced |
| pytest | **135 passed / 33 skipped / 0 failed** | trinity/tests 全量 | 2026-08-14 修复后基线 |

> **Note**: LongMemEval simulated result uses a template-generated 55-question set, NOT the official 500-question LongMemEval-S; the 500-question mock set follows LongMemEval-S category structure but is community-generated. Official LongMemEval-S / LoCoMo (1982 questions) datasets are **unreachable from this environment** (GitHub/HuggingFace blocked); once network access is available, replace with official sets. SQuAD score reflects passage-selection retrieval, not end-to-end memory recall.

---

## Research Foundation

Trinity's `second_brain` module aligns with 129 brain-inspired papers:

- **Memory**: HebbianMemoryGraph, HippocampalComplementaryMemory, OnlineFocusedMemory
- **Evolution**: SelfOptimizingMemory (EvolM), MetaMemoryOptimizer
- **Safety**: AdversarialMemoryDefense, PoisonedMemoryAuditor, BackdoorDetector
- **Causality**: CausalSemanticGraphMemory, CounterfactualReasoningEngine
- **Economics**: TrustExchange, PrivacyBudget, FederatedAggregator

---

## Requirements

- Python 3.11+
- Docker Desktop (for containerized deployment)
- jieba >= 0.42.1 (Chinese word segmentation)
- strawberry-graphql >= 0.224 (GraphQL API)
- Optional: Loihi 2 / TrueNorth hardware for neuromorphic mode

---

## License

MIT — see [pyproject.toml](pyproject.toml) and LICENSE file.

---

*Trinity: not a memory library — a memory operating system.*
*（内容由AI生成，仅供参考）*
