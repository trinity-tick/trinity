"""Trinity Self-Evolving Memory System — evolution package.

Submodules:
    usage_analyzer      — access pattern analysis (hotspots, patterns, heatmaps)
    feedback_collector  — agent feedback aggregation and quality detection
    mutation_engine     — memory mutation suggestions and auto-application
    optimization_engine — index, graph, pruning, defragmentation optimisation
    evolution_scheduler — orchestrates periodic self-evolution cycles
    strategies          — pluggable evolution strategy registry
"""

from .usage_analyzer import (
    UsageAnalyzer,
    AccessEntry,
    Hotspot,
    UsagePattern,
    Heatmap,
)
from .feedback_collector import (
    FeedbackCollector,
    FeedbackEntry,
    FeedbackAggregate,
    QualityIssue,
    QualityTrend,
)
from .mutation_engine import (
    MutationEngine,
    MergeSuggestion,
    EnrichSuggestion,
    SplitSuggestion,
    SynthesisMemory,
)
from .optimization_engine import (
    OptimizationEngine,
    IndexChange,
    GraphReorganization,
    PruneResult,
    OptimizationStats,
)
from .evolution_scheduler import (
    ABTestConfig,
    ABTestResult,
    EvolutionScheduler,
    EvolutionCycleResult,
)
from .strategies import (
    StrategyRegistry,
    EvolutionStrategy,
    InterventionLevel,
    create_default_strategies,
)
# MetaEvolution（Observe→Analyze→Plan→Execute→Certify 循环引擎）——
# 此前未在此导出，导致 `from trinity.evolution import MetaEvolution`
# （trinity_init.py 的用法）报 ImportError。
from .core import MetaEvolution, EvolutionCycle, EvolutionPhase, EvolutionState

__all__ = [
    # usage_analyzer
    "UsageAnalyzer",
    "AccessEntry",
    "Hotspot",
    "UsagePattern",
    "Heatmap",
    # feedback_collector
    "FeedbackCollector",
    "FeedbackEntry",
    "FeedbackAggregate",
    "QualityIssue",
    "QualityTrend",
    # mutation_engine
    "MutationEngine",
    "MergeSuggestion",
    "EnrichSuggestion",
    "SplitSuggestion",
    "SynthesisMemory",
    # optimization_engine
    "OptimizationEngine",
    "IndexChange",
    "GraphReorganization",
    "PruneResult",
    "OptimizationStats",
    # evolution_scheduler
    "EvolutionScheduler",
    "EvolutionCycleResult",
    "ABTestConfig",
    "ABTestResult",
    # strategies
    "StrategyRegistry",
    "EvolutionStrategy",
    "InterventionLevel",
    "create_default_strategies",
    # core
    "MetaEvolution",
    "EvolutionCycle",
    "EvolutionPhase",
    "EvolutionState",
]
