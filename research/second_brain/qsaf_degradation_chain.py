"""
# status: orphan (2026-08-15 audit, not in runtime path)
P27-3: QSAF Degradation Chain — QSAF Framework (2026.05).
Triadic: [Flood Detection] → [Entrenchment Breaking] → [Intervention Planning].

Monitors agent behavior across QSAF's five-stage degradation chain:
Context Flood → Resource Starvation → Behavioral Drift →
Memory Entrenchment → Functional Override.
Provides stage assessment, flood detection, entrenchment breaking,
and unified intervention planning.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums / Dataclasses
# ---------------------------------------------------------------------------

class DegradationStage(Enum):
    """QSAF five-stage degradation chain."""
    NORMAL = 0
    CONTEXT_FLOOD = 1
    RESOURCE_STARVATION = 2
    BEHAVIORAL_DRIFT = 3
    MEMORY_ENTRENCHMENT = 4
    FUNCTIONAL_OVERRIDE = 5


class InterventionType(Enum):
    """Types of intervention actions."""
    THROTTLE_CONTEXT = "throttle_context"
    RELEASE_RESOURCES = "release_resources"
    RESET_BEHAVIOR = "reset_behavior"
    BREAK_CYCLE = "break_cycle"
    HARD_RESET = "hard_reset"


@dataclass
class FloodAlert:
    """Alert produced when context flooding is detected."""
    triggered: bool = False
    current_token_count: int = 0
    budget_limit: int = 0
    utilization_pct: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class InterventionResult:
    """Outcome of an entrenchment-breaking intervention."""
    success: bool = False
    broken_patterns: int = 0
    detail: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class InterventionPlan:
    """Unified plan with recommended interventions."""
    current_stage: DegradationStage = DegradationStage.NORMAL
    recommended_actions: list[InterventionType] = field(default_factory=list)
    urgency: float = 0.0
    plan_id: str = ""
    generated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class FloodDetector:
    """Detects context token flooding."""

    def __init__(self, flood_threshold_pct: float = 0.85):
        self._lock = threading.RLock()
        self._threshold_pct = flood_threshold_pct
        self._alert_count: int = 0

    def check(self, context_token_count: int, budget_limit: int) -> FloodAlert:
        """Check if context is approaching flood territory."""
        with self._lock:
            if budget_limit <= 0:
                return FloodAlert(budget_limit=budget_limit)
            pct = context_token_count / budget_limit
            triggered = pct >= self._threshold_pct
            if triggered:
                self._alert_count += 1
            return FloodAlert(
                triggered=triggered,
                current_token_count=context_token_count,
                budget_limit=budget_limit,
                utilization_pct=round(pct, 4),
            )

    def statistics(self) -> dict:
        with self._lock:
            return {"alert_count": self._alert_count}


class EntrenchmentBreaker:
    """Breaks memory entrenchment cycles."""

    def __init__(self, max_cycles: int = 5):
        self._lock = threading.RLock()
        self._max_cycles = max_cycles
        self._broken_count: int = 0

    def break_cycle(self, entrenched_patterns: list[dict]) -> InterventionResult:
        """Attempt to break entrenched memory patterns."""
        with self._lock:
            if not entrenched_patterns:
                return InterventionResult(success=True, detail="No patterns to break")

            broken = 0
            for pattern in entrenched_patterns[: self._max_cycles]:
                pattern["entrenched"] = False
                pattern["broken_at"] = time.time()
                broken += 1
                logger.info("Broke entrenchment: %s", pattern.get("id", "unknown"))

            self._broken_count += broken
            return InterventionResult(
                success=broken > 0,
                broken_patterns=broken,
                detail=f"Broke {broken} of {len(entrenched_patterns)} patterns",
            )

    def statistics(self) -> dict:
        with self._lock:
            return {"broken_count": self._broken_count}


class QSAFMonitor:
    """QSAF five-stage degradation assessor."""

    STAGE_THRESHOLDS: dict[DegradationStage, dict[str, float]] = {
        DegradationStage.CONTEXT_FLOOD: {"context_utilization": 0.85},
        DegradationStage.RESOURCE_STARVATION: {"memory_pressure": 0.90},
        DegradationStage.BEHAVIORAL_DRIFT: {"drift_score": 0.30},
        DegradationStage.MEMORY_ENTRENCHMENT: {"entrenchment_score": 0.50},
        DegradationStage.FUNCTIONAL_OVERRIDE: {"error_rate": 0.15},
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._assessment_count: int = 0
        self._stage_history: list[tuple[float, DegradationStage]] = []

    def assess(self, agent_state: dict) -> DegradationStage:
        """Assess current degradation stage from agent metrics."""
        with self._lock:
            self._assessment_count += 1

            context_util = agent_state.get("context_utilization", 0.0)
            error_rate = agent_state.get("error_rate", 0.0)
            drift_score = agent_state.get("drift_score", 0.0)
            entrenchment = agent_state.get("entrenchment_score", 0.0)
            memory_pressure = agent_state.get("memory_pressure", 0.0)

            if error_rate >= 0.15:
                stage = DegradationStage.FUNCTIONAL_OVERRIDE
            elif entrenchment >= 0.50:
                stage = DegradationStage.MEMORY_ENTRENCHMENT
            elif drift_score >= 0.30:
                stage = DegradationStage.BEHAVIORAL_DRIFT
            elif memory_pressure >= 0.90:
                stage = DegradationStage.RESOURCE_STARVATION
            elif context_util >= 0.85:
                stage = DegradationStage.CONTEXT_FLOOD
            else:
                stage = DegradationStage.NORMAL

            self._stage_history.append((time.time(), stage))
            return stage

    def statistics(self) -> dict:
        with self._lock:
            return {
                "assessment_count": self._assessment_count,
                "current_stage": (
                    self._stage_history[-1][1].name if self._stage_history else "NORMAL"
                ),
            }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def monitor_and_intervene(agent_metrics: dict) -> InterventionPlan:
    """Assess stage and build an intervention plan."""
    monitor = QSAFMonitor()
    stage = monitor.assess(agent_metrics)
    plan = InterventionPlan(current_stage=stage, plan_id=f"ip_{int(time.time())}")

    action_map: dict[DegradationStage, list[InterventionType]] = {
        DegradationStage.CONTEXT_FLOOD: [InterventionType.THROTTLE_CONTEXT],
        DegradationStage.RESOURCE_STARVATION: [InterventionType.RELEASE_RESOURCES],
        DegradationStage.BEHAVIORAL_DRIFT: [InterventionType.RESET_BEHAVIOR],
        DegradationStage.MEMORY_ENTRENCHMENT: [InterventionType.BREAK_CYCLE],
        DegradationStage.FUNCTIONAL_OVERRIDE: [InterventionType.HARD_RESET],
    }
    plan.recommended_actions = action_map.get(stage, [])
    plan.urgency = min(1.0, stage.value / len(DegradationStage))
    return plan


def get_statistics() -> dict:
    """Return aggregated module-level statistics."""
    return {
        "module": "qsaf_degradation_chain",
        "version": "1.0.0",
        "papers": ["QSAF Framework 2026.05"],
        "classes": ["QSAFMonitor", "FloodDetector", "EntrenchmentBreaker"],
    }
