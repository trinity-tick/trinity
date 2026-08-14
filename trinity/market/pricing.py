"""Pricing Engine — memory valuation model.

Multi-factor pricing that combines rareness, freshness, graph
connectivity, and historical trade data to produce a fair market price.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Pricing factors ───────────────────────────────────────────────────

_FACTOR_WEIGHTS = {
    "rarity": 0.30,
    "freshness": 0.20,
    "connectivity": 0.25,
    "hist_price": 0.25,
}

_MARKET_SEGMENT_DEFAULTS: Dict[str, float] = {
    "text": 0.5,
    "image": 2.0,
    "code": 3.0,
    "structured": 1.5,
}


def _rarity_score(content: str, modality: str, market_data: Optional[List[Dict]] = None) -> float:
    """Heuristic: shorter unique content in sparse modality → higher rarity."""
    if not content:
        return 0.0
    base = min(len(content) / 200.0, 1.0)  # 200-char cap
    # If we have market data, lower rarity when many similar assets
    if market_data and len(market_data) > 0:
        same_modality = [m for m in market_data if m.get("modality") == modality]
        competition = len(same_modality) / max(len(market_data), 1)
        base *= (1.0 - 0.5 * competition)
    return min(base, 1.0)


def _freshness_score(created_at: Optional[str]) -> float:
    """Exponential decay with 7-day half-life."""
    if not created_at:
        return 0.0
    try:
        created = datetime.fromisoformat(created_at)
        age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400.0
        return 2.0 ** (-age_days / 7.0)
    except Exception:
        return 0.5


def _connectivity_score(memory: Dict[str, Any]) -> float:
    """Score based on linked_memory_ids count, capped at 10."""
    linked = memory.get("linked_memory_ids", [])
    if not isinstance(linked, list):
        linked = []
    return min(len(linked) / 10.0, 1.0)


def _hist_price_score(modality: str, hist_trades: Optional[List[Dict]] = None) -> float:
    """Normalised historical trade price for this modality."""
    if not hist_trades:
        return _MARKET_SEGMENT_DEFAULTS.get(modality, 0.5) / 5.0
    # Average historical price capped at 5.0 → normalise to [0,1]
    same = [t for t in hist_trades if t.get("modality") == modality]
    if not same:
        return _MARKET_SEGMENT_DEFAULTS.get(modality, 0.5) / 5.0
    avg_price = sum(t.get("price", 0.0) for t in same) / len(same)
    return min(avg_price / 5.0, 1.0)


# ── Public API ────────────────────────────────────────────────────────

def estimate_value(
    memory: Dict[str, Any],
    market_data: Optional[List[Dict]] = None,
    hist_trades: Optional[List[Dict]] = None,
) -> float:
    """Estimate fair market value for a single memory.

    Returns a float in [0, 5] range (0 = worthless, 5 = premium).
    """
    content = memory.get("content", "")
    modality = memory.get("category", "text")
    created = memory.get("created_at")

    rarity = _rarity_score(content, modality, market_data)
    freshness = _freshness_score(created)
    connectivity = _connectivity_score(memory)
    hist = _hist_price_score(modality, hist_trades)

    raw = (
        rarity * _FACTOR_WEIGHTS["rarity"]
        + freshness * _FACTOR_WEIGHTS["freshness"]
        + connectivity * _FACTOR_WEIGHTS["connectivity"]
        + hist * _FACTOR_WEIGHTS["hist_price"]
    )
    return round(raw * 5.0, 2)


def get_market_price(modality: str, hist_trades: Optional[List[Dict]] = None) -> float:
    """Get average market price for a given modality."""
    same = [t for t in (hist_trades or []) if t.get("modality") == modality]
    if same:
        return round(sum(t.get("price", 0.0) for t in same) / len(same), 2)
    return _MARKET_SEGMENT_DEFAULTS.get(modality, 0.5)
