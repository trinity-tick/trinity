"""Unit tests for evolution package — A/B testing framework (v8.6.0)."""
import pytest

from trinity.evolution import (
    ABTestConfig,
    ABTestResult,
    EvolutionScheduler,
    EvolutionCycleResult,
)
from trinity.evolution.strategies import InterventionLevel


class TestABTestFramework:
    """Unit tests for the A/B testing framework in EvolutionScheduler."""

    def test_ab_config_defaults(self):
        config = ABTestConfig()
        assert config.enabled is False
        assert config.metric_fn is None
        assert config.min_improvement_threshold == 0.0
        assert config.max_trial_cycles == 3
        assert config.rollback_on_regression is True

    def test_ab_config_custom(self):
        config = ABTestConfig(
            enabled=True,
            min_improvement_threshold=0.05,
            max_trial_cycles=5,
            rollback_on_regression=False,
        )
        assert config.enabled is True
        assert config.min_improvement_threshold == 0.05
        assert config.max_trial_cycles == 5
        assert config.rollback_on_regression is False

    def test_ab_test_result_fields(self):
        result = ABTestResult(
            cycle_id="cycle_1",
            baseline_score=0.80,
            experimental_score=0.85,
            delta=0.05,
            accepted=True,
            reason="Improvement accepted",
            timestamp="2026-08-11T00:00:00Z",
        )
        assert result.accepted is True
        assert result.delta == 0.05
        assert result.reason == "Improvement accepted"

    def test_scheduler_default_ab_disabled(self):
        scheduler = EvolutionScheduler()
        config = scheduler.get_ab_config()
        assert config["enabled"] is False
        assert config["has_metric_fn"] is False

    def test_enable_ab_testing(self):
        scheduler = EvolutionScheduler()
        scheduler.enable_ab_testing(
            metric_fn=lambda: 0.75,
            min_improvement_threshold=0.02,
        )
        config = scheduler.get_ab_config()
        assert config["enabled"] is True
        assert config["has_metric_fn"] is True
        assert config["min_improvement_threshold"] == 0.02

    def test_disable_ab_testing(self):
        scheduler = EvolutionScheduler()
        scheduler.enable_ab_testing(metric_fn=lambda: 0.75)
        result = scheduler.disable_ab_testing()
        assert result["status"] == "ab_disabled"
        config = scheduler.get_ab_config()
        assert config["enabled"] is False

    def test_get_ab_history_empty(self):
        scheduler = EvolutionScheduler()
        history = scheduler.get_ab_history()
        assert isinstance(history, list)
        assert len(history) == 0

    def test_run_cycle_ab_improvement(self):
        """A/B test with improvement → should accept."""
        scheduler = EvolutionScheduler(auto_intervention_level=InterventionLevel.READ_ONLY)
        scheduler.enable_ab_testing(
            metric_fn=lambda: 0.90,
            min_improvement_threshold=0.0,
        )
        result = scheduler.run_evolution_cycle()
        assert result.status == "completed"
        assert result.ab_result is not None
        # baseline from a fresh scheduler is 0.90, same metric_fn → delta=0
        assert result.ab_result.accepted is True

    def test_run_cycle_ab_regression(self):
        """A/B test with regression → should reject and note rollback."""
        calls = iter([0.80, 0.72])  # first=baseline, second=experimental

        scheduler = EvolutionScheduler(auto_intervention_level=InterventionLevel.READ_ONLY)
        scheduler.enable_ab_testing(
            metric_fn=lambda: next(calls),
            min_improvement_threshold=0.01,
            rollback_on_regression=True,
        )
        result = scheduler.run_evolution_cycle()
        assert result.ab_result is not None
        assert result.ab_result.accepted is False
        assert "Rollback" in result.ab_result.reason or "Regression" in result.ab_result.reason

    def test_run_cycle_no_ab(self):
        """Without AB enabled, ab_result should be None."""
        scheduler = EvolutionScheduler(auto_intervention_level=InterventionLevel.READ_ONLY)
        result = scheduler.run_evolution_cycle()
        assert result.status == "completed"
        assert result.ab_result is None

    def test_paused_cycle_ab_none(self):
        """Paused scheduler returns no A/B result."""
        scheduler = EvolutionScheduler()
        scheduler.enable_ab_testing(metric_fn=lambda: 0.75)
        scheduler.pause()
        result = scheduler.run_evolution_cycle()
        assert result.status == "paused"
        assert result.ab_result is None

    def test_trial_limit_forces_accept(self):
        """After max_trial_cycles rejections, the cycle is force-accepted."""
        calls = iter([0.80, 0.70])  # always regression

        scheduler = EvolutionScheduler(auto_intervention_level=InterventionLevel.READ_ONLY)
        scheduler.enable_ab_testing(
            metric_fn=lambda: next(calls),
            min_improvement_threshold=0.01,
            max_trial_cycles=2,
            rollback_on_regression=True,
        )

        # Cycle 1: rejected
        r1 = scheduler.run_evolution_cycle()
        assert r1.ab_result is not None
        assert r1.ab_result.accepted is False

        # Cycle 2: trial limit reached, force-accepted
        calls_2 = iter([0.80, 0.70])
        scheduler.enable_ab_testing(
            metric_fn=lambda: next(calls_2),
            min_improvement_threshold=0.01,
            max_trial_cycles=2,
            rollback_on_regression=True,
        )
        # override _trial_count to 1 to simulate second trial
        scheduler._trial_count = 1
        r2 = scheduler.run_evolution_cycle()
        assert r2.ab_result is not None
        assert r2.ab_result.accepted is True  # force-accepted
        assert "Trial limit" in r2.ab_result.reason

    def test_evolution_schedule_cycle(self):
        scheduler = EvolutionScheduler()
        result = scheduler.schedule_cycle(interval_hours=6)
        assert result["interval_hours"] == 6
        assert result["paused"] is False
        assert result["scheduled"] is True

    def test_get_evolution_history(self):
        scheduler = EvolutionScheduler(auto_intervention_level=InterventionLevel.READ_ONLY)
        scheduler.run_evolution_cycle()
        scheduler.run_evolution_cycle()
        history = scheduler.get_evolution_history()
        assert len(history) == 2
        assert all(isinstance(r, EvolutionCycleResult) for r in history)

    def test_ab_history_after_cycles(self):
        scheduler = EvolutionScheduler(auto_intervention_level=InterventionLevel.READ_ONLY)
        scheduler.enable_ab_testing(metric_fn=lambda: 0.75)
        scheduler.run_evolution_cycle()
        scheduler.run_evolution_cycle()
        ab_history = scheduler.get_ab_history()
        assert len(ab_history) == 2
        assert all(isinstance(r, ABTestResult) for r in ab_history)
