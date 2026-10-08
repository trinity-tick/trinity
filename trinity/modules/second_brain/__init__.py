"""
Second Brain engine — memory encoding, retrieval, reasoning, and self-evolution.

Papers: P1-P129 aligned

**能力口径（T6 2026-10-06 校正；判据 tests/unit/test_capability_hygiene_20261006.py）**

  · Modules        : **22**（`engine_core.SecondBrainV636.total_modules`）。
    原文案写「122 modules」—— 那 122 里有 100 个 `range()` 生成的占位名 M1..M100，
    已于 2026-09-30 移除（见 `tests/unit/test_capability_name_reality_20260930.py`：
    `total_modules == modules_real`、`modules_generated_placeholder == 0`）。
  · Guardian chain : **50 declared / 0 enforcing**。50 是可追溯的设计名册（L1..L50，
    带论文出处），但当前**无执行体**；引用这个数时必须说明是 declared 口径。
  · Retrieval ch.  : **47 registered / 0 contributing**。47 是通道注册表
    （`channel_1..channel_47`），不持有记忆数据源，`search()` 按契约返回 `[]`。
  · Exported names : **len(__all__)** —— 模块常量 `EXPORTED_COUNT`（下方），
    测试要求它与 `len(__all__)` 一致。原文案写「29 (6 originals + 23 newly activated)」
    与现实的 91 不符，已改为派生值。
  · Version        : sourced from trinity.version (single source of truth)

以上四个数字的「声明口径 / 实现口径」对照表在
`trinity/modules/second_brain/capability_ledger.py::ROSTER`（机器可核对的单一来源）。
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


# ── 自报口径（T6 2026-10-06）─────────────────────────────────────────────
# 模块头部 docstring 里的「Exported names」必须是**派生值**而不是手抄字面量：
# 旧字面量 29 与现实的 91 长期不符，且没有任何判据盯着它。
# `tests/unit/test_capability_hygiene_20261006.py` 要求
#   EXPORTED_COUNT == len(__all__) == capability_ledger.ROSTER["second_brain_exports"]["value"]
EXPORTED_COUNT = len(__all__)

