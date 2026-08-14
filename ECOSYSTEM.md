---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 1ed0c9b5065b1819decccf0a8be25f33_2b1394a793f011f1a102525400826444
    ReservedCode1: tZUwZ0dtPwau6iZzfa9uoj34ZvgsRNUZvXd9mdQ7i7SCIABZ7vsGLhWHtwzGGocTtARXEKqpuhDPwojm7+4gTVquL9djYMStRN4qVIPJ0OJpAhsYV75569RD9WYIp1vzXZXcZiegSpjPdDjtLHFBleXdNN5sWyE6IVPp4CVI9DC9ff1Cuw3H2DcZmnw=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 1ed0c9b5065b1819decccf0a8be25f33_2b1394a793f011f1a102525400826444
    ReservedCode2: tZUwZ0dtPwau6iZzfa9uoj34ZvgsRNUZvXd9mdQ7i7SCIABZ7vsGLhWHtwzGGocTtARXEKqpuhDPwojm7+4gTVquL9djYMStRN4qVIPJ0OJpAhsYV75569RD9WYIp1vzXZXcZiegSpjPdDjtLHFBleXdNN5sWyE6IVPp4CVI9DC9ff1Cuw3H2DcZmnw=
---

# Trinity Ecosystem

## Module Inventory

Trinity ships **260+ modules** across 8 functional layers. Modules follow a `CB{N}` (Capability Block) numbering scheme.

### Layer Architecture

| Layer | Range | Count | Description |
|:------|:------|:-----:|:------------|
| **Core Engine** | engine_core | 8 | Engine façade + 7 sub-modules (types / governance / memory_core / memory_tiers / data_pipeline / guardian_retrieval / optimization) |
| **Retrieval** | CB53–CB56 | 4 | BEAM-LIGHT, Exabase 3-Stage, Hindsight 4-Network, Zikkaron Hopfield |
| **Ingestion** | CB45–CB52 | 8 | Cascade extraction, relationship management, entity resolution |
| **Memory Tier** | CB1–CB44 | 44 | Multi-tier hierarchical memory (working / episodic / semantic / procedural) |
| **Guardian** | GuardianChainV50 | 1 | 50-level guardian chain with anti-forgetting and compression audit |
| **Evolution** | evolution/ | 5 | MetaEvolution 5-phase cycle, auto-curricula, engram memory |
| **Network Fusion** | CB58–CB75 | 18 | Temporal validity, zero-LLM retrieval, dimensional memory, Hebbian graph, hypergraph, awakening pipeline, anchor tracking |
| **Daemon** | daemon/ | 12 | Prompt compression auditor, reasoning drift, consolidation sleep, SDPO anti-forgetting |

### Full Module Map (Selected Highlights)

#### Retrieval Layer
| Module | Code | Paper / Benchmark |
|:-------|:-----|:------------------|
| BEAM-LIGHT | CB53 | ICLR 2026 BEAM Benchmark |
| Exabase 3-Stage | CB54 | LongMemEval 96.4% SOTA |
| Hindsight 4-Network | CB55 | BEAM 10M SOTA 64.1% |
| Zikkaron Hopfield | CB56 | Non-LLM SOTA 40.4% |

#### Network Fusion (CB58–CB75)
| Module | Code | Paper |
|:-------|:-----|:------|
| Temporal Validity Window | CB58 | Zep / Graphiti dual-temporal |
| Zero-LLM Retrieval | CB59 | Mandol Zero-LLM |
| Procedural Memory | CB60 | LangMem LoCoMo 58.1% |
| Dimensional Memory | CB61 | DimMem structured dimensions |
| Cognitive Value Model | CB62 | Value-based retention |
| Hierarchical Navigation | CB63 | Multi-granularity routing |
| Harmonic Memory | CB64 | Wave-based representation |
| Filesystem Memory | CB65 | OS-level memory organizer |
| Dual-Branch Memory | CB66 | tri_mem: procedural + declarative |
| Streaming Ingestion | CB67 | Real-time memory pipeline |
| Meta-Learning Optimizer | CB68 | Learned memory policies |
| Neural-Symbolic Reasoner | CB69 | Logic + embedding fusion |
| Multi-Agent Shared Memory | CB70 | Cross-agent state + A2A |
| Self-Evolution Lineage | CB71 | Versioned knowledge evolution |
| Hebbian Memory Graph | CB72 | HeLa-Mem weight-strengthened edges |
| Hypergraph Memory | CB73 | HyperMem LoCoMo 92.73% |
| Memory Awakening Pipeline | CB74 | EverMemOS staged recall |
| Anchor Fact Tracking | CB75 | AnchorMem conflict-resilient facts |

#### Evolution Engine
| Module | Description |
|:-------|:------------|
| core.py | MetaEvolution: detect → plan → execute → validate → consolidate |
| auto_curricula.py | Self-paced task curriculum generation |
| engram_memory.py | Consolidation-sleep inspired memory replay |
| reinforcement.py | RL-based retention policy optimization |
| anomaly_detector.py | Drift detection and self-healing triggers |

#### Daemon Services
| Module | Description |
|:-------|:------------|
| prompt_compression_auditor.py | 10-layer compression integrity audit |
| reasoning_drift_detector.py | Chain-of-thought coherence monitoring |
| consolidation_sleep.py | Sleep-phase memory reorganization |
| sdpo_anti_forgetting.py | SDPO-based catastrophic forgetting prevention |
| coma_prompt_audit.py | COMA prompt-level attack surface scanning |
| knowledge_graph_builder.py | Semantic graph construction and query |
| vector_index_manager.py | Multi-backend vector index lifecycle |
| rest_api.py | FastAPI 8-endpoint REST server |
| mcp_server.py | Model Context Protocol stdio + SSE |
| web_dashboard.py | Real-time monitoring dashboard |
| observability.py | Metrics + tracing + alerting |
| gdpr_handler.py | GDPR Article 17 right-to-erasure |

---

## Papers & Research Alignment

Trinity integrates insights from **30+ papers** across memory systems, retrieval, and agent architectures.

### Core References

| Paper | Venue | Module(s) |
|:------|:------|:----------|
| MemArena: Arena for Memory Systems | arXiv:2509.21771 (2025) | benchmarks/ |
| BEAM: Benchmarking Everlasting Agent Memory | ICLR 2026 | CB53 |
| ExaBase: Retrieval-Augmented Language Models | LongMemEval 96.4% | CB54 |
| Hindsight: 4-Network Memory Fusion | BEAM 10M 64.1% | CB55 |
| LoCoMo: Long-Context Memory | EMNLP 2024 | CB60, arena |
| LongMemEval: Long-Term Memory Evaluation | NeurIPS 2024 | CB54, benchmarks |
| SDPO: Self-Distillation Preference Optimization | arXiv:2506 (2025) | daemon/sdpo |
| Mem0: The Memory Layer for AI | arXiv:2504 (2025) | CB45–CB48 |
| Graphiti: Temporal Knowledge Graph | Zep (2025) | CB58 |
| LangMem: Procedural Agent Memory | LangChain (2025) | CB60 |
| DimMem: Dimensional Memory Representation | 2025 | CB61 |
| HyperMem: Hypergraph Memory Framework | LoCoMo 92.73% | CB73 |
| HeLa-Mem: Hebbian-Lamarckian Memory | 2025 | CB72 |
| EverMemOS: Continuous Memory Awakening | 2025 | CB74 |
| AnchorMem: Anchored Fact Tracking | 2025 | CB75 |
| Mandol: Memory-Adaptive Neural Decomposition | Zero-LLM | CB59 |
| SelfMem: Self-Optimizing Memory | July 2026 | CB57 |
| MemoryAgentBench: Agent Memory Stress Test | ICLR 2026 | benchmarks |
| LoCoMo-R1: Reasoning-Augmented LoCoMo | DeepSeek-R1 (2025) | benchmarks |
| COMA: Compress Once, Monitor Always | 2025 | daemon/coma |
| BEAM 10M: Million-scale Memory Benchmark | ICLR 2026 | CB55 |
| ByteRover: Byte-level Memory Optimization | 2025 | CB45 |
| Supermemory: Hierarchical Memory Management | 2025 | CB49 |
| Mastra: Multi-Agent State Transfer | 2025 | CB70 |
| MemMachine: Memory Lifecycle Management | 2025 | CB52 |

### Supplementary References

| Paper | Relevance |
|:------|:----------|
| MemVerse: Memory Universe for Agents | Memory state representation |
| Readable Minds: Theory of Mind for Agents | User modeling |
| LoCoMo-Plus: Enhanced LoCoMo | Evaluation extension |
| Memory Cleanup: Garbage Collection for Memory | Lifecycle management |
| Progressive Memory Cascade | Retrieval routing |
| Hippocampus-Cortex Bridge | Biological inspiration |
| Cognitive Memory Evaluator | Quality assessment |
| A2A: Agent-to-Agent Protocol | Cross-agent communication |
| GDPR Article 17 | Right to erasure compliance |

---

## Benchmark Datasets

Trinity's `benchmarks/` suite supports the following standard datasets:

| Dataset | Description | Format | Auto-Download |
|:--------|:------------|:-------|:-------------:|
| **LoCoMo** | Long-context memory (up to 100K turns) | JSONL | Yes |
| **LongMemEval** | Multi-session long-term memory | JSON | No |
| **LoCoMo-R1** | LoCoMo with reasoning traces | JSONL | No |
| **MemoryAgentBench** | Agent memory stress test (ICLR 2026) | JSONL | No |

### Custom Dataset

Add your own by subclassing `benchmarks.datasets.DatasetLoader`:

```python
from benchmarks.datasets import DatasetLoader, DatasetSample

class MyDataset(DatasetLoader):
    name = "MyDataset"
    source_file = "my_data.jsonl"

    def load(self) -> list[DatasetSample]:
        # parse and return samples
        ...
```

---

## Integration Interfaces

Trinity exposes 6 integration surfaces:

| Interface | Protocol | Use Case |
|:----------|:---------|:---------|
| **Python API** | `from trinity import Trinity` | Direct embedding |
| **MCP Server** | Model Context Protocol stdio + SSE | AI agent plugins |
| **REST API** | FastAPI (8 endpoints) | Web / mobile backends |
| **CLI** | `python -m trinity` | DevOps / scripting |
| **Docker** | `docker compose up -d` | Containerized deployment |
| **MemorySystem Protocol** | `benchmarks.arena.MemorySystem` | Custom evaluators |

### MemorySystem Protocol

All memory systems entering the arena must implement:

```python
class MySystem:
    name: str

    def ingest(self, conversation: list[dict[str, str]]) -> None: ...
    def retrieve(self, query: str, top_k: int = 10) -> list[str]: ...
    def generate(self, query: str, context: list[str]) -> str: ...
    def stats(self) -> dict[str, Any]: ...
```

---

## Storage Backends

| Backend | Status | Features |
|:--------|:------:|:---------|
| **SQLite** (FTS5) | Production | Default, zero-config |
| **PostgreSQL** (pg_trgm) | Production | UUID, GIN indexes, schema migration |
| **ChromaDB** | Beta | Vector-native embedding store |
| **Vectile** | Beta | Disk-backed vector index |

---

## Community Links

- **PyPI**: [pypi.org/project/trinity-memory](https://pypi.org/project/trinity-memory/)
- **Documentation**: [trinity-tick.github.io/trinity](https://trinity-tick.github.io/trinity)
- **Issues**: [github.com/trinity-tick/trinity/issues](https://github.com/trinity-tick/trinity/issues)
- **Contributing**: [COMMUNITY.md](COMMUNITY.md)
*（内容由AI生成，仅供参考）*
