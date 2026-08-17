"""
Trinity Agents Sub-Package
==========================
Agent Brain + A2A Bridge + Three-Layer Memory + Aggregator + Dimensions
+ Auto Discovery + Observability + Benchmark (v7.1.0)

Core:
  - AgentBrain — autonomous memory loop (shared memory pool)
  - AgentBridge — Main↔Trinity dispatch bridge (Embedded Context)
  - Memory Layers — Working / Episodic / Semantic (dimension-aware)
  - Dimensions — 8-dimension vector indexing engine
  - Aggregator — shared cross-agent memory pool
  - A2A Adapter — contextId management, Message/Part, AgentCard
  - Observability — request tracing, latency metrics, health dashboard
  - Benchmark — LongMemEval/MemSyco compatible evaluation pipeline

Exports:
  - AgentBrain, MemoryAgentProtocol, AgentMemoryContext, DecisionEngine
  - create_agent_brain, self_test
  - AgentBridge, PromotionGate
  - EpisodicMemory, SemanticMemory, WorkingMemory, MemoryLayerManager
  - DimensionEngine, DimensionVector, MemoryCategory, MemoryScope
  - MemoryAggregator
  - A2AContextManager, AgentCardRegistry
  - ObservabilityManager, RequestTracer
  - MemoryBenchmark
"""

from trinity.agents.agent_brain import (
    AgentBrain,
    MemoryAgentProtocol,
    AgentMemoryContext,
    DecisionEngine,
    create_agent_brain,
    self_test,
)

from trinity.agents.bridge import (
    AgentBridge,
    PromotionGate,
)

from trinity.agents.memory_layers import (
    EpisodicMemory,
    SemanticMemory,
    WorkingMemory,
    MemoryLayerManager,
)

from trinity.agents.dimensions import (
    DimensionEngine,
    DimensionVector,
    MemoryCategory,
    MemoryScope,
    RelationType,
    TimeBucket,
    create_dimension_engine,
)

from trinity.agents.aggregator import (
    MemoryAggregator,
    create_aggregator,
)

from trinity.agents.degradation import (
    DegradationManager,
    ServiceTier,
)

from trinity.agents.consolidation_daemon import (
    ConsolidationDaemon,
)

from trinity.agents.observability import (
    ObservabilityManager,
    RequestTracer,
)

from trinity.agents.benchmark import (
    MemoryBenchmark,
)

from trinity.agents.a2a_adapter import (
    A2AContextManager,
    AgentCardRegistry,
)

from trinity.agents.auto_discovery import (
    AutoRegistry,
    get_aggregator,
    get_bridge,
    ensure_bootstrapped,
    trinity_memory,
    trinity_context,
    TrinityContextManager,
)

__all__ = [
    "AgentBrain",
    "MemoryAgentProtocol",
    "AgentMemoryContext",
    "DecisionEngine",
    "create_agent_brain",
    "self_test",
    "AgentBridge",
    "PromotionGate",
    "EpisodicMemory",
    "SemanticMemory",
    "WorkingMemory",
    "MemoryLayerManager",
    "DimensionEngine",
    "DimensionVector",
    "MemoryCategory",
    "MemoryScope",
    "RelationType",
    "TimeBucket",
    "create_dimension_engine",
    "MemoryAggregator",
    "create_aggregator",
    "DegradationManager",
    "ServiceTier",
    "ConsolidationDaemon",
    "ObservabilityManager",
    "RequestTracer",
    "MemoryBenchmark",
    "A2AContextManager",
    "AgentCardRegistry",
    "AutoRegistry",
    "get_aggregator",
    "get_bridge",
    "ensure_bootstrapped",
    "trinity_memory",
    "trinity_context",
    "TrinityContextManager",
]
