"""
Second Brain engine — 122 modules for memory encoding, retrieval, reasoning, and self-evolution.

Papers: P1-P129 aligned
Guardian chain: 50-tier
Retrieval channels: 47-way
Exported modules: 29 (6 originals + 23 newly activated from engine facade)
Version: sourced from trinity.version (single source of truth)
"""

# ── 导入噪音门控（2026-08-15）──────────────────────────────────────────
# 各模块模块级有 60 处 print("[Pxxx] ... initialized") 横幅，import trinity 时
# 刷屏。设 TRINITY_QUIET_IMPORT=1 时过滤这些横幅（在导入本包子模块前打补丁，
# 单点覆盖全部模块）。未设置时行为不变。
import builtins as _builtins
import os as _os

if _os.environ.get("TRINITY_QUIET_IMPORT") == "1":
    _orig_print = _builtins.print

    def _quiet_import_print(*args, **kwargs):
        first = args[0] if args else ""
        if isinstance(first, str) and (
            first.startswith("[P") or first.startswith("[Second Brain")
        ):
            return
        _orig_print(*args, **kwargs)

    _builtins.print = _quiet_import_print


from trinity.modules.second_brain.engine import (
    SecondBrainV636 as Engine,
    VERSION,
    # ── Retrieval channels (47-way core, BEAM, external benchmarks) ──
    RetrievalSystemV47,
    ExabaseRetrieval,
    BEAMLIGHT,
    HindsightFourNetwork,
    ZikkaronHopfield,
    SpreadingActivationGraph,
    # ── Memory core ──
    GuardianChainV50,
    MultiHeadRecurrentMemory,
    HippocampalComplementaryMemory,
    ThreeLayerHierarchicalMemory,
    # ── Lifecycle & governance ──
    IdentityPreservingConsolidator,
    ElephantAgentStateContinuity,
    ConstraintSteerableOversight,
    OnlineSafetyMonitor,
    ReasoningDriftAuditor,
    # ── Temporal & versioning ──
    TemporalValidity,
    TokenEfficientMemory,
    RelationalVersioning,
    ProgressiveCascade,
    # ── Ingestion & curation ──
    AgentNativeCuration,
    ContextualChunkIngestion,
    SelfOptimizingMemory,
    # ── Diagnostics & observation ──
    GroundTruthEpisodes,
    ObserverReflector,
)
from trinity.modules.second_brain.continuous_eval import (
    RagasMetrics,
    ContinuousEvalEngine,
    EvalResultStore,
    EvalAlert,
    AlertLevel,
    CONTINUOUS_EVAL_ENABLED,
    CONTINUOUS_EVAL_WINDOW,
    CONTINUOUS_EVAL_ALERT_THRESHOLD,
    CONTINUOUS_EVAL_ALERT_CONSECUTIVE,
    CONTINUOUS_EVAL_BUFFER_SIZE,
    BEAMLIGHT_DIMENSIONS,
    create_eval_engine,
    self_test as continuous_eval_self_test,
)
from trinity.modules.second_brain.contextual_embedding import (
    ContextualChunk,
    ContextualEmbedder,
    CONTEXTUAL_ENABLED,
    DEFAULT_CONTEXTUAL_CONTEXT_WINDOW,
    DEFAULT_CONTEXTUAL_SUMMARY_MAX_TOKENS,
    create_contextual_embedder,
    self_test as contextual_embedding_self_test,
)
from trinity.modules.second_brain.selective_recall import (
    SelectiveRecallRouter,
    SelectiveRecallManager,
    RecallDecision,
    IntentClass,
    SelectiveRecallStats,
    SELECTIVE_RECALL_ENABLED,
    SELECTIVE_RECALL_MAX_TOKENS,
    SELECTIVE_RECALL_FORCE_KEYWORDS,
    SELECTIVE_RECALL_LLM_THRESHOLD,
    self_test as selective_recall_self_test,
)
from trinity.modules.second_brain.prompt_ingestion import (
    IngestionPrompts,
    StructuredMemoryUnit,
    PromptIngestionPipeline,
    INGEST_EXTRACT,
    INGEST_FILTER,
    INGEST_DEDUP,
    INGEST_SUMMARIZE,
    self_test as prompt_ingestion_self_test,
)
from trinity.modules.second_brain.consensus_voting import (
    MemorySnapshot,
    ConsensusVoter,
    ConsensusResult,
    MemoryVersionManager,
    CONSENSUS_THRESHOLD,
    CONSENSUS_RECENCY_HALF_LIFE,
    CONSENSUS_MIN_VERSIONS_FOR_VOTE,
    CONSENSUS_AUTO_RESOLVE,
    self_test as consensus_voting_self_test,
)
from trinity.modules.second_brain.federated_memory import (
    FederatedMemoryModel,
    FederatedAggregator,
    FederationOrchestrator,
    PrivacyBudget,
    add_gaussian_noise,
    clip_gradients,
    self_test as federated_memory_self_test,
)
from trinity.modules.second_brain.self_healing import (
    SelfHealingPipeline,
    MemoryHealthMonitor,
    SelfHealingScheduler,
    self_test as self_healing_self_test,
)
from trinity.modules.second_brain.causal_memory import (
    CausalMemory,
    self_test as causal_memory_self_test,
)
from trinity.modules.second_brain.causal_semantic_graph_memory import (
    CausalSemanticGraphMemory,
    CounterfactualReasoningEngine,
    CommonsenseCompletionBridge,
    ActMemEvaluator,
    self_test as causal_semantic_graph_self_test,
)

__all__ = [
    "Engine",
    "VERSION",
    # ── Retrieval channels ──
    "RetrievalSystemV47",
    "ExabaseRetrieval",
    "BEAMLIGHT",
    "HindsightFourNetwork",
    "ZikkaronHopfield",
    "SpreadingActivationGraph",
    # ── Memory core ──
    "GuardianChainV50",
    "MultiHeadRecurrentMemory",
    "HippocampalComplementaryMemory",
    "ThreeLayerHierarchicalMemory",
    # ── Lifecycle & governance ──
    "IdentityPreservingConsolidator",
    "ElephantAgentStateContinuity",
    "ConstraintSteerableOversight",
    "OnlineSafetyMonitor",
    "ReasoningDriftAuditor",
    # ── Temporal & versioning ──
    "TemporalValidity",
    "TokenEfficientMemory",
    "RelationalVersioning",
    "ProgressiveCascade",
    # ── Ingestion & curation ──
    "AgentNativeCuration",
    "ContextualChunkIngestion",
    "SelfOptimizingMemory",
    # ── Diagnostics & observation ──
    "GroundTruthEpisodes",
    "ObserverReflector",
    # ── Existing (6 modules) ──
    "RagasMetrics",
    "ContinuousEvalEngine",
    "EvalResultStore",
    "EvalAlert",
    "AlertLevel",
    "CONTINUOUS_EVAL_ENABLED",
    "CONTINUOUS_EVAL_WINDOW",
    "CONTINUOUS_EVAL_ALERT_THRESHOLD",
    "CONTINUOUS_EVAL_ALERT_CONSECUTIVE",
    "CONTINUOUS_EVAL_BUFFER_SIZE",
    "BEAMLIGHT_DIMENSIONS",
    "create_eval_engine",
    "continuous_eval_self_test",
    "ContextualChunk",
    "ContextualEmbedder",
    "CONTEXTUAL_ENABLED",
    "DEFAULT_CONTEXTUAL_CONTEXT_WINDOW",
    "DEFAULT_CONTEXTUAL_SUMMARY_MAX_TOKENS",
    "create_contextual_embedder",
    "contextual_embedding_self_test",
    "SelectiveRecallRouter",
    "SelectiveRecallManager",
    "RecallDecision",
    "IntentClass",
    "SelectiveRecallStats",
    "SELECTIVE_RECALL_ENABLED",
    "SELECTIVE_RECALL_MAX_TOKENS",
    "SELECTIVE_RECALL_FORCE_KEYWORDS",
    "SELECTIVE_RECALL_LLM_THRESHOLD",
    "selective_recall_self_test",
    "IngestionPrompts",
    "StructuredMemoryUnit",
    "PromptIngestionPipeline",
    "INGEST_EXTRACT",
    "INGEST_FILTER",
    "INGEST_DEDUP",
    "INGEST_SUMMARIZE",
    "prompt_ingestion_self_test",
    "MemorySnapshot",
    "ConsensusVoter",
    "ConsensusResult",
    "MemoryVersionManager",
    "CONSENSUS_THRESHOLD",
    "CONSENSUS_RECENCY_HALF_LIFE",
    "CONSENSUS_MIN_VERSIONS_FOR_VOTE",
    "CONSENSUS_AUTO_RESOLVE",
    "consensus_voting_self_test",
    # P1-7: Federated Memory
    "FederatedMemoryModel",
    "FederatedAggregator",
    "FederationOrchestrator",
    "PrivacyBudget",
    "add_gaussian_noise",
    "clip_gradients",
    "federated_memory_self_test",
    # P2-5: Self Healing Memory
    "SelfHealingPipeline",
    "MemoryHealthMonitor",
    "SelfHealingScheduler",
    "self_healing_self_test",
    # P2-6: Causal Reasoning Memory
    "CausalMemory",
    "CausalSemanticGraphMemory",
    "CounterfactualReasoningEngine",
    "CommonsenseCompletionBridge",
    "ActMemEvaluator",
    "causal_memory_self_test",
    "causal_semantic_graph_self_test",
]
