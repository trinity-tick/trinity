"""
Trinity — 三位一体智能记忆系统
===============================
v8.2.0 | VMS (Virtual Memory System) + Multi-Framework Adapters

Usage:
    from trinity import Trinity, AgentBridge, EpisodicMemory
    from trinity.vms import VMS
    memory = Trinity()
    memory.ingest("user prefers dark mode")
    results = memory.search("user preferences", top_k=5)
"""

from trinity.version import __version__, VERSION_MAJOR, VERSION_MINOR, VERSION_PATCH, VERSION_TUPLE, VERSION_STRING
# 2026-09-02：集中凭证解析 + psycopg2.connect 补丁（brain/* 等存量硬编码点自动生效）
from trinity.security import credentials as _credentials  # noqa: F401  # noqa: E402
__all__ = [
    "Trinity",
    "TrinityClient",
    # Agent Brain (v6.93.0)
    "AgentBrain",
    "MemoryAgentProtocol",
    "AgentMemoryContext",
    "DecisionEngine",
    "create_agent_brain",
    # Bridge & A2A (v6.94.0)
    "AgentBridge",
    "PromotionGate",
    "EpisodicMemory",
    "SemanticMemory",
    "WorkingMemory",
    "MemoryLayerManager",
    "A2AContextManager",
    "AgentCardRegistry",
    # Aggregator & Dimensions (v6.95.0)
    "DimensionEngine",
    "DimensionVector",
    "MemoryCategory",
    "MemoryScope",
    "MemoryAggregator",
    # Auto-Discovery (v6.96.0)
    "AutoRegistry",
    "TrinityContextManager",
    "get_aggregator",
    "get_bridge",
    "ensure_bootstrapped",
    "trinity_memory",
    "trinity_context",
    "__version__",
    # Multi-Anchor Identity (v8.0.0)
    "IdentityManager",
    "HybridRouter",
    "ANCHOR_TYPES",
    # DCSA-EJP Double-Loop Audit (v8.0.0)
    "Auditor",
    "ConstitutionalEngine",
    "JustificationPacket",
    # A2A Protocol (v8.0.0)
    "AgentCard",
    "SkillDef",
    "TaskManager",
    "CapabilityRegistry",
    "A2AProtocol",
    # Marvis A2A Adapter (v8.0.0)
    "MarvisAdapter",
    # Memory Compression (v8.2.0)
    "MemoryCompressor",
    "TokenCounter",
    "ImportanceScorer",
    "CompressedContext",
    # Active Memory Collection (v8.3.0)
    "EventDrivenCollector",
    "BackgroundScanner",
    "AgentConnector",
    "CollectorManager",
    "HookPoint",
    "CollectorState",
    "MemoryPayload",
    "CollectorStats",
    "BUILTIN_AGENTS",
    # A2A Ed25519 + x509 (v8.2.0)
    "Ed25519Signer",
    "SigningAlgorithm",
    "SigningBridge",
    "x509Certificate",
    "x509CertificateChain",
    # OpenTelemetry Tracing (v8.2.0)
    "Tracer",
    "get_tracer",
    "traced",
    "start_span",
    "end_span",
    "instrument_search",
    "instrument_write",
    # Performance Benchmark Suite (v8.2.0)
    "PerformanceBench",
    "measure_qps",
    "measure_latency",
    "measure_recall",
]

from trinity.core.client import Trinity, TrinityClient
from trinity.agents.agent_brain import (
    AgentBrain,
    MemoryAgentProtocol,
    AgentMemoryContext,
    DecisionEngine,
    create_agent_brain,
)
from trinity.agents.bridge import AgentBridge, PromotionGate
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
)
from trinity.agents.aggregator import MemoryAggregator
from trinity.agents.a2a_adapter import A2AContextManager, AgentCardRegistry
from trinity.agents.auto_discovery import (
    AutoRegistry,
    TrinityContextManager,
    get_aggregator,
    get_bridge,
    ensure_bootstrapped,
    trinity_memory,
    trinity_context,
)

# ── Multi-Anchor Identity (v8.0.0) ──────────────────────────────────────
from trinity.identity import IdentityManager, HybridRouter, ANCHOR_TYPES

# ── DCSA-EJP Double-Loop Audit (v8.0.0) ────────────────────────────────
from trinity.audit import Auditor, ConstitutionalEngine, JustificationPacket

# ── A2A Protocol (v8.0.0) ──────────────────────────────────────────────
from trinity.a2a import (
    AgentCard,
    SkillDef,
    TaskManager,
    CapabilityRegistry,
    A2AProtocol,
)

# ── Marvis A2A Adapter (v8.0.0) ─────────────────────────────────────────
from trinity.a2a.adapters import MarvisAdapter

# ── Memory Compression (v8.2.0) ─────────────────────────────────────────
from trinity.memory import (
    MemoryCompressor,
    TokenCounter,
    ImportanceScorer,
    CompressedContext,
)

# ── Active Memory Collection (v8.3.0) ────────────────────────────────────
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

# ── A2A Ed25519 + x509 (v8.2.0) ─────────────────────────────────────────
from trinity.a2a.ed25519_signer import (
    Ed25519Signer,
    SigningAlgorithm,
    SigningBridge,
    x509Certificate,
    x509CertificateChain,
)

# ── OpenTelemetry Tracing (v8.2.0) ──────────────────────────────────────
from trinity.telemetry import (
    Tracer,
    get_tracer,
    traced,
    start_span,
    end_span,
    instrument_search,
    instrument_write,
)

# ── Performance Benchmark Suite (v8.2.0) ────────────────────────────────
from trinity.benchmark.perf import (
    PerformanceBench,
    measure_qps,
    measure_latency,
    measure_recall,
)

# ── Auto-bootstrap on import (v8.0.0) ───────────────────────────────────
# Importing trinity triggers zero-config agent registration:
# discovers Trinity path, creates shared MemoryAggregator,
# registers AgentCard — all without manual API calls.
# Silently no-ops when TRINITY_MEMORY_ENABLED=0 or Trinity not found.
ensure_bootstrapped()
