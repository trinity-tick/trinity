"""Trinity Memory — Letta-style virtual context management + consolidation
                      + Active Memory Collection (v8.5.0).

Provides:
  - MemoryCompressor: auto-summary, dedup, and importance-sorted context window
  - TokenCounter: token estimation and budget tracking
  - ImportanceScorer: composite scoring (access freq + time decay + correlation)
  - CompressedContext: structured result container
  - HippocampalConsolidator: sleep/wake memory consolidation engine
  - ConsolidationConfig / ConsolidationResult / MemoryItem / MemoryType
  - EventDrivenCollector: 6-hook event-driven active memory collector (v8.3.0)
  - BackgroundScanner: daemon scanner for uncaptured context (v8.3.0)
  - AgentConnector: per-agent event bridge connectors (v8.3.0)
  - CollectorManager: unified lifecycle manager for all collectors (v8.3.0)
  - StreamIngestor: streaming memory ingestion via Kafka/Redis/InMemory (v8.5.0)
  - StreamMessage / StreamBackend / InMemoryBackend / KafkaBackend / RedisStreamBackend
"""

from trinity.memory.compression import (
    MemoryCompressor,
    TokenCounter,
    ImportanceScorer,
    CompressedContext,
)

from trinity.memory.consolidation import (
    HippocampalConsolidator,
    ConsolidationConfig,
    ConsolidationResult,
    MemoryItem,
    MemoryType,
    ConsolidationPhase,
)

from trinity.memory.bench_integration import (
    MemoryBench,
    run_memory_baseline,
)

from trinity.memory.stream_ingest import (
    StreamIngestor,
    StreamMessage,
    StreamStatus,
    StreamBackend,
    InMemoryBackend,
    KafkaBackend,
    RedisStreamBackend,
    self_test as stream_ingest_self_test,
)

from trinity.memory.active_collector import (
    EventDrivenCollector,
    BackgroundScanner,
    AgentConnector,
    CollectorManager,
    HookPoint,
    CollectorState,
    MemoryPayload,
    CollectorStats,
    BUILTIN_AGENTS,
)

from trinity.memory.er_extractor import (
    EntityRelationExtractor,
    self_test as er_extractor_self_test,
)

from trinity.memory.layer_classifier import (
    LayerClassifier,
    self_test as layer_classifier_self_test,
)

from trinity.memory.consolidator import (
    MemoryConsolidator,
    self_test as consolidator_self_test,
)

from trinity.memory.memory_agent import (
    MemoryAgent,
    self_test as memory_agent_self_test,
)

__all__ = [
    "MemoryCompressor",
    "TokenCounter",
    "ImportanceScorer",
    "CompressedContext",
    "HippocampalConsolidator",
    "ConsolidationConfig",
    "ConsolidationResult",
    "MemoryItem",
    "MemoryType",
    "ConsolidationPhase",
    # v8.4.0 — Memory Benchmark Integration
    "MemoryBench",
    "run_memory_baseline",
    # v8.5.0 — Stream Ingest (P1-2)
    "StreamIngestor",
    "StreamMessage",
    "StreamStatus",
    "StreamBackend",
    "InMemoryBackend",
    "KafkaBackend",
    "RedisStreamBackend",
    "stream_ingest_self_test",
    # v8.3.0 — Active Memory Collection
    "EventDrivenCollector",
    "BackgroundScanner",
    "AgentConnector",
    "CollectorManager",
    "HookPoint",
    "CollectorState",
    "MemoryPayload",
    "CollectorStats",
    "BUILTIN_AGENTS",
    # v8.6.0 — Entity-Relation Extractor (P0.2)
    "EntityRelationExtractor",
    "er_extractor_self_test",
    # v8.6.1 — Memory Consolidator (P0.3)
    "MemoryConsolidator",
    "consolidator_self_test",
    # v8.7.0 — Three-Layer Classifier (P0.4)
    "LayerClassifier",
    "layer_classifier_self_test",
    # v8.8.0 — Memory Agent (P0 gap: async consolidation daemon)
    "MemoryAgent",
    "memory_agent_self_test",
]
