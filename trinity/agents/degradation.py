"""
Trinity P1-4: Degradation Strategy Framework.
Three-tier fallback: FULL → DEGRADED → MINIMAL
"""

import logging
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class ServiceTier(Enum):
    FULL = "full"           # All channels active
    DEGRADED = "degraded"   # Core channels only (keyword + FAISS)
    MINIMAL = "minimal"     # Keyword-only fallback


class DegradationManager:
    """Three-tier degradation manager with health monitoring."""

    def __init__(self):
        self._health: Dict[str, bool] = {
            "keyword": True,
            "vector": True,
            "second_brain": True,
            "retrieval_v47": True,
            "exabase": True,
            "beamlight": True,
            "aggregator": True,
        }
        self._tier: ServiceTier = ServiceTier.FULL
        self._failure_counts: Dict[str, int] = {}
        self._degradation_history: List[dict] = []
        self._FULL_CHANNELS = {"keyword", "vector", "second_brain", "retrieval_v47", "exabase"}
        self._DEGRADED_CHANNELS = {"keyword", "vector"}
        self._MINIMAL_CHANNELS = {"keyword"}

    def mark_failure(self, channel: str, reason: str = "") -> bool:
        """Mark a channel as failed. Returns True if tier changed."""
        self._health[channel] = False
        self._failure_counts[channel] = self._failure_counts.get(channel, 0) + 1
        self._degradation_history.append({
            "event": "failure", "channel": channel, "reason": reason,
            "failures": self._failure_counts[channel]
        })
        logger.warning("Degradation: %s marked FAILED (x%d) — %s",
                       channel, self._failure_counts[channel], reason)
        return self._recompute_tier()

    def mark_recovery(self, channel: str):
        """Mark a channel as recovered."""
        self._health[channel] = True
        self._degradation_history.append({"event": "recovery", "channel": channel})
        logger.info("Degradation: %s recovered", channel)
        self._recompute_tier()

    def _recompute_tier(self) -> bool:
        """Recompute service tier based on health. Returns True if tier changed."""
        prev_tier = self._tier
        active = {ch for ch, ok in self._health.items() if ok}

        if self._FULL_CHANNELS.issubset(active):
            self._tier = ServiceTier.FULL
        elif self._DEGRADED_CHANNELS.issubset(active):
            self._tier = ServiceTier.DEGRADED
        else:
            self._tier = ServiceTier.MINIMAL

        changed = prev_tier != self._tier
        if changed:
            logger.warning("Degradation: tier changed %s → %s", prev_tier.value, self._tier.value)
        return changed

    @property
    def tier(self) -> ServiceTier:
        return self._tier

    def is_channel_available(self, channel: str) -> bool:
        return self._health.get(channel, False)

    def get_active_channels(self) -> List[str]:
        return [ch for ch, ok in self._health.items() if ok and ch != "aggregator"]

    def statistics(self) -> dict:
        return {
            "tier": self._tier.value,
            "health": dict(self._health),
            "failure_counts": dict(self._failure_counts),
            "degradation_events": len(self._degradation_history),
            "active_channels": self.get_active_channels(),
        }

    def reset(self):
        for k in self._health:
            self._health[k] = True
        self._tier = ServiceTier.FULL
        self._failure_counts.clear()
        self._degradation_history.clear()
