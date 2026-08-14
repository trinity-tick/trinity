"""Evolution Scheduler — orchestrates periodic self-evolution cycles.

Runs the full evolution pipeline:
  1. Analyse usage patterns
  2. Collect and aggregate feedback
  3. Generate mutation suggestions
  4. Evaluate strategies
  5. Apply optimal mutations
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .usage_analyzer import UsageAnalyzer
from .feedback_collector import FeedbackCollector
from .mutation_engine import MutationEngine
from .optimization_engine import OptimizationEngine
from .strategies import StrategyRegistry, create_default_strategies, InterventionLevel


# ═══════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ABTestConfig:
    """Configuration for A/B testing evolution mutations.

    Parameters
    ----------
    enabled : bool
        Whether A/B testing mode is active.
    metric_fn : callable or None
        Baseline metric function (returns float 0.0–1.0).
    min_improvement_threshold : float
        Minimum improvement required to accept mutations (default 0.0).
    max_trial_cycles : int
        Maximum cycles to run before forcing a decision.
    rollback_on_regression : bool
        Whether to auto-revert if performance degrades.
    """
    enabled: bool = False
    metric_fn: Optional[Callable[[], float]] = None
    min_improvement_threshold: float = 0.0
    max_trial_cycles: int = 3
    rollback_on_regression: bool = True


@dataclass
class ABTestResult:
    """Result of A/B comparing baseline (A) vs experimental (B)."""
    cycle_id: str
    baseline_score: float
    experimental_score: float
    delta: float
    accepted: bool
    reason: str
    timestamp: str


@dataclass
class EvolutionCycleResult:
    """Full result of one evolution cycle."""
    cycle_id: str
    timestamp: str
    strategy_triggers: List[str]
    applied_mutations: int
    index_changes: int
    prune_candidates: int
    quality_alerts: int
    status: str = "completed"    # completed | paused | error
    details: Dict[str, Any] = field(default_factory=dict)
    ab_result: Optional[ABTestResult] = None   # v8.6.0


# ═══════════════════════════════════════════════════════════════════════════
# Scheduler
# ═══════════════════════════════════════════════════════════════════════════

class EvolutionScheduler:
    """Schedules and executes self-evolution cycles.

    Parameters
    ----------
    interval_hours : int
        Default interval between cycles.
    auto_intervention_level : InterventionLevel
        Maximum autonomous intervention authority.
    """

    def __init__(
        self,
        interval_hours: int = 4,
        auto_intervention_level: InterventionLevel = InterventionLevel.SUGGEST,
        ab_config: Optional[ABTestConfig] = None,
    ):
        self.interval_hours = interval_hours
        self.auto_intervention_level = auto_intervention_level

        # Sub-engines
        self.analyzer = UsageAnalyzer()
        self.collector = FeedbackCollector()
        self.mutator = MutationEngine()
        self.optimizer = OptimizationEngine()

        # Strategy
        self.strategies = create_default_strategies()

        # A/B testing (v8.6.0)
        self.ab_config = ab_config or ABTestConfig()
        self._baseline_metrics: Optional[Dict[str, float]] = None
        self._trial_count: int = 0

        # State
        self._paused: bool = False
        self._history: List[EvolutionCycleResult] = []
        self._cycle_count: int = 0

    # ── Scheduling ──────────────────────────────────────────────────────

    def schedule_cycle(self, interval_hours: Optional[int] = None) -> Dict[str, Any]:
        """Schedule or update the evolution cycle interval."""
        if interval_hours is not None:
            self.interval_hours = interval_hours
        return {
            "scheduled": True,
            "interval_hours": self.interval_hours,
            "paused": self._paused,
            "next_cycle": f"{self.interval_hours}h from now",
        }

    def pause(self) -> Dict[str, Any]:
        self._paused = True
        return {"status": "paused", "timestamp": datetime.now(timezone.utc).isoformat()}

    def resume(self) -> Dict[str, Any]:
        self._paused = False
        return {"status": "resumed", "timestamp": datetime.now(timezone.utc).isoformat()}

    # ── Full Cycle ──────────────────────────────────────────────────────

    def run_evolution_cycle(self) -> EvolutionCycleResult:
        """Execute a complete evolution cycle.

        Pipeline: analyse usage → collect feedback → generate mutations →
        evaluate strategies → apply best.

        When A/B testing is enabled (v8.6.0), captures baseline metrics
        before mutations and experimental metrics after, then accepts or
        rolls back based on ABTestConfig thresholds.
        """
        if self._paused:
            return EvolutionCycleResult(
                cycle_id=f"cycle_{self._cycle_count}",
                timestamp=datetime.now(timezone.utc).isoformat(),
                strategy_triggers=[],
                applied_mutations=0,
                index_changes=0,
                prune_candidates=0,
                quality_alerts=0,
                status="paused",
            )

        self._cycle_count += 1
        cycle_id = f"cycle_{self._cycle_count}"
        timestamp = datetime.now(timezone.utc).isoformat()

        # ── A/B: capture baseline before mutations ──────────────────
        ab_result: Optional[ABTestResult] = None
        baseline_score: Optional[float] = None

        if self.ab_config.enabled and self.ab_config.metric_fn is not None:
            try:
                baseline_score = self.ab_config.metric_fn()
            except Exception:
                baseline_score = None

        # Phase 1: Analyse usage
        hotspots = self.analyzer.analyze_hotspots()
        patterns = self.analyzer.detect_patterns()

        # Phase 2: Collect feedback
        quality_issues = self.collector.detect_quality_issues()

        # Phase 3: Generate mutations (fed by analysis)

        # Phase 4: Evaluate strategies
        context = {
            "hotspots": hotspots,
            "patterns": patterns,
            "quality_issues": quality_issues,
            "suggestions": self.mutator.get_pending(),
        }

        triggered: List[str] = []
        mutations_applied = 0
        for s in self.strategies.get_strategies(enabled_only=True):
            try:
                if s.condition_fn(context):
                    triggered.append(s.name)
                    # Only execute if intervention level allows
                    if s.max_intervention <= self.auto_intervention_level:
                        s.action_fn(context)
                        mutations_applied += 1
            except Exception:
                pass

        # Phase 5: Apply optimisations
        self.optimizer.optimize_indexes(hotspots)

        # ── A/B: measure after mutations ────────────────────────────
        if baseline_score is not None and self.ab_config.metric_fn is not None:
            try:
                experimental_score = self.ab_config.metric_fn()
                delta = round(experimental_score - baseline_score, 4)

                accepted = delta >= self.ab_config.min_improvement_threshold
                if not accepted and self.ab_config.rollback_on_regression:
                    reason = (
                        f"Regression detected (delta={delta}), "
                        f"rollback recommended"
                    )
                elif not accepted:
                    reason = (
                        f"Below threshold (delta={delta} < "
                        f"{self.ab_config.min_improvement_threshold})"
                    )
                else:
                    reason = f"Improvement accepted (delta={delta})"
                    self._trial_count = 0  # reset trial on success

                ab_result = ABTestResult(
                    cycle_id=cycle_id,
                    baseline_score=round(baseline_score, 4),
                    experimental_score=round(experimental_score, 4),
                    delta=delta,
                    accepted=accepted,
                    reason=reason,
                    timestamp=timestamp,
                )
            except Exception:
                pass

        # ── Trial limit gate ────────────────────────────────────────
        if ab_result is not None and not ab_result.accepted:
            self._trial_count += 1
            if self._trial_count >= self.ab_config.max_trial_cycles:
                ab_result.reason += (
                    f" | Trial limit reached ({self._trial_count}/{self.ab_config.max_trial_cycles}), "
                    "accepting by force"
                )
                ab_result.accepted = True
                self._trial_count = 0

        result = EvolutionCycleResult(
            cycle_id=cycle_id,
            timestamp=timestamp,
            strategy_triggers=triggered,
            applied_mutations=mutations_applied,
            index_changes=len(self.optimizer._stats.index_changes),
            prune_candidates=0,
            quality_alerts=len(quality_issues),
            details={
                "hotspot_count": len(hotspots),
                "pattern_count": len(patterns),
                "quality_issue_count": len(quality_issues),
            },
            ab_result=ab_result,
        )

        self._history.append(result)
        return result

    # ── History ─────────────────────────────────────────────────────────

    def get_evolution_history(self, limit: int = 20) -> List[EvolutionCycleResult]:
        return self._history[-limit:]

    # ── A/B Testing Helpers (v8.6.0) ────────────────────────────────────

    def enable_ab_testing(self, metric_fn: Callable[[], float], **config_overrides) -> Dict[str, Any]:
        """Enable A/B testing mode with a custom metric function.

        Parameters
        ----------
        metric_fn: Zero-argument callable that returns a float score 0.0–1.0.
        **config_overrides: Overrides for ABTestConfig fields.

        Returns
        -------
        Dict with current A/B configuration.
        """
        self.ab_config = ABTestConfig(
            enabled=True,
            metric_fn=metric_fn,
            **{k: v for k, v in config_overrides.items()
               if k in ("min_improvement_threshold", "max_trial_cycles", "rollback_on_regression")},
        )
        self._trial_count = 0
        return self.get_ab_config()

    def disable_ab_testing(self) -> Dict[str, Any]:
        """Disable A/B testing mode."""
        self.ab_config.enabled = False
        self._baseline_metrics = None
        self._trial_count = 0
        return {"status": "ab_disabled"}

    def get_ab_config(self) -> Dict[str, Any]:
        """Return current A/B testing configuration and stats."""
        return {
            "enabled": self.ab_config.enabled,
            "min_improvement_threshold": self.ab_config.min_improvement_threshold,
            "max_trial_cycles": self.ab_config.max_trial_cycles,
            "rollback_on_regression": self.ab_config.rollback_on_regression,
            "current_trial": self._trial_count,
            "has_metric_fn": self.ab_config.metric_fn is not None,
        }

    def get_ab_history(self) -> List[ABTestResult]:
        """Return A/B test results from all cycles that had A/B enabled."""
        return [
            r.ab_result for r in self._history
            if r.ab_result is not None
        ]
