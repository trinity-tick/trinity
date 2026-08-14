"""Evolution Strategies — pluggable strategy registry for self-evolution.

Each strategy defines: trigger condition, action, priority, and maximum
autonomous intervention level.
"""

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
from enum import IntEnum


# ═══════════════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════════════

class InterventionLevel(IntEnum):
    """Maximum autonomous intervention authority."""
    READ_ONLY = 0       # Only observe, never modify
    SUGGEST = 1         # Generate suggestions, wait for approval
    LOW_RISK = 2        # Auto-apply low-risk changes (re-weight, boost)
    STANDARD = 3        # Auto-apply standard changes (merge, deprioritise)
    FULL = 4            # Full autonomy (prune, delete)


@dataclass
class EvolutionStrategy:
    """A registered evolution strategy."""
    name: str
    condition_fn: Callable[[Dict[str, Any]], bool]
    action_fn: Callable[[Dict[str, Any]], Any]
    priority: int                        # higher = executed first
    max_intervention: InterventionLevel
    description: str = ""
    enabled: bool = True


# ═══════════════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════════════

class StrategyRegistry:
    """Pluggable strategy registry for the evolution engine."""

    def __init__(self):
        self._strategies: Dict[str, EvolutionStrategy] = {}

    def register_strategy(
        self,
        name: str,
        condition_fn: Callable[[Dict[str, Any]], bool],
        action_fn: Callable[[Dict[str, Any]], Any],
        priority: int = 0,
        max_intervention: InterventionLevel = InterventionLevel.SUGGEST,
        description: str = "",
    ) -> EvolutionStrategy:
        strategy = EvolutionStrategy(
            name=name,
            condition_fn=condition_fn,
            action_fn=action_fn,
            priority=priority,
            max_intervention=max_intervention,
            description=description,
        )
        self._strategies[name] = strategy
        return strategy

    def get_strategies(self, enabled_only: bool = True) -> List[EvolutionStrategy]:
        strategies = list(self._strategies.values())
        if enabled_only:
            strategies = [s for s in strategies if s.enabled]
        return sorted(strategies, key=lambda s: -s.priority)

    def get_strategy(self, name: str) -> Optional[EvolutionStrategy]:
        return self._strategies.get(name)

    def disable(self, name: str) -> bool:
        s = self._strategies.get(name)
        if s:
            s.enabled = False
            return True
        return False

    def enable(self, name: str) -> bool:
        s = self._strategies.get(name)
        if s:
            s.enabled = True
            return True
        return False


# ═══════════════════════════════════════════════════════════════════════════
# Built-in Strategies — condition functions
# ═══════════════════════════════════════════════════════════════════════════

def _cond_hotspot_boost(ctx: Dict[str, Any]) -> bool:
    """Trigger: ≥1 hotspot detected with burst_factor ≥ 2.0."""
    hotspots = ctx.get("hotspots", [])
    return any(h.burst_factor >= 2.0 for h in hotspots)


def _cond_cold_storage(ctx: Dict[str, Any]) -> bool:
    """Trigger: memories with 0 accesses in last 7 days."""
    patterns = ctx.get("patterns", [])
    return any(p.pattern_type == "forgetting" and len(p.memory_ids) > 0 for p in patterns)


def _cond_dedup_merge(ctx: Dict[str, Any]) -> bool:
    """Trigger: ≥2 merge suggestions with confidence ≥ 0.7."""
    suggestions = ctx.get("suggestions", [])
    merges = [s for s in suggestions if s.get("type") == "merge" and s.get("confidence", 0) >= 0.7]
    return len(merges) >= 2


def _cond_outdated_prune(ctx: Dict[str, Any]) -> bool:
    """Trigger: memories with 0 accesses ≥30 days and 0 references."""
    issues = ctx.get("quality_issues", [])
    return any(i.issue_type == "outdated" for i in issues)


def _cond_feedback_decay(ctx: Dict[str, Any]) -> bool:
    """Trigger: any memory with avg rating < 2.5 and ≥3 feedbacks."""
    issues = ctx.get("quality_issues", [])
    return any(i.issue_type == "low_quality" and i.severity >= 0.5 for i in issues)


def _cond_synergy_synthesis(ctx: Dict[str, Any]) -> bool:
    """Trigger: ≥3 high-frequency co-reference pairs detected."""
    patterns = ctx.get("patterns", [])
    for p in patterns:
        if p.pattern_type == "co_ref":
            pairs = p.metadata.get("pairs", [])
            if len(pairs) >= 3:
                return True
    return False


# ═══════════════════════════════════════════════════════════════════════════
# Factory — create standard strategy set
# ═══════════════════════════════════════════════════════════════════════════

def create_default_strategies() -> StrategyRegistry:
    """Create a registry pre-loaded with the six built-in strategies."""
    registry = StrategyRegistry()

    # Actions are simple echo functions — actual work happens in the engine
    def _act_echo(ctx: Dict[str, Any]) -> Dict[str, Any]:
        return {"applied": True, "context_keys": list(ctx.keys())}

    registry.register_strategy(
        name="hotspot_boost",
        condition_fn=_cond_hotspot_boost,
        action_fn=_act_echo,
        priority=10,
        max_intervention=InterventionLevel.STANDARD,
        description="Boost index priority for hotspot memories",
    )
    registry.register_strategy(
        name="cold_storage",
        condition_fn=_cond_cold_storage,
        action_fn=_act_echo,
        priority=8,
        max_intervention=InterventionLevel.LOW_RISK,
        description="Downgrade cold memories to lower index granularity",
    )
    registry.register_strategy(
        name="dedup_merge",
        condition_fn=_cond_dedup_merge,
        action_fn=_act_echo,
        priority=7,
        max_intervention=InterventionLevel.STANDARD,
        description="Auto-merge high-similarity duplicate memories",
    )
    registry.register_strategy(
        name="outdated_prune",
        condition_fn=_cond_outdated_prune,
        action_fn=_act_echo,
        priority=6,
        max_intervention=InterventionLevel.SUGGEST,
        description="Flag 30-day unaccessed unreferenced memories for pruning",
    )
    registry.register_strategy(
        name="feedback_decay",
        condition_fn=_cond_feedback_decay,
        action_fn=_act_echo,
        priority=5,
        max_intervention=InterventionLevel.LOW_RISK,
        description="Down-weight persistently low-rated memories",
    )
    registry.register_strategy(
        name="synergy_synthesis",
        condition_fn=_cond_synergy_synthesis,
        action_fn=_act_echo,
        priority=4,
        max_intervention=InterventionLevel.SUGGEST,
        description="Synthesise high-value composite memory from co-referenced cluster",
    )

    return registry
