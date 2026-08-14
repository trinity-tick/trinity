"""Order Book — decentralised memory marketplace listing facility.

Maintains an in-memory order book of MemoryAsset listings with support
for listing, delisting, searching, and price queries.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


@dataclass
class OrderEntry:
    """A single listing on the order book."""

    asset_id: str
    asset: Any          # MemoryAsset
    price: float
    currency: str       # trust_score | audit_trail | anchor_token
    listed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    is_active: bool = True


class OrderBook:
    """Decentralised listing directory for memory assets.

    Thread-safe by default on CPython (dict mutations are GIL-protected).
    """

    def __init__(self):
        self._orders: Dict[str, OrderEntry] = {}

    # ── CRUD ──────────────────────────────────────────────────────────

    def list_asset(
        self,
        asset: Any,
        price: float = 0.0,
        currency: str = "trust_score",
    ) -> OrderEntry:
        """List a MemoryAsset on the market.

        Raises ValueError if the asset is already listed.
        """
        if asset.memory_id in self._orders and self._orders[asset.memory_id].is_active:
            raise ValueError(f"Asset {asset.memory_id} is already listed")
        entry = OrderEntry(
            asset_id=asset.memory_id,
            asset=asset,
            price=price,
            currency=currency,
        )
        self._orders[asset.memory_id] = entry
        return entry

    def delist_asset(self, asset_id: str) -> bool:
        """Remove a listing (soft-delete — marks inactive)."""
        entry = self._orders.get(asset_id)
        if entry is None:
            return False
        entry.is_active = False
        return True

    # ── Queries ───────────────────────────────────────────────────────

    def search_market(
        self,
        query: str = "",
        modality: Optional[str] = None,
        max_price: Optional[float] = None,
    ) -> List[OrderEntry]:
        """Search active listings by keyword / modality / price ceiling."""
        results: List[OrderEntry] = []
        q = query.lower()
        for entry in self._orders.values():
            if not entry.is_active:
                continue
            if max_price is not None and entry.price > max_price:
                continue
            if modality and entry.asset.modality != modality:
                continue
            if q:
                # match against tags and memory content
                tags_str = " ".join(entry.asset.tags).lower()
                content = ""
                if entry.asset._memory:
                    content = entry.asset._memory.get("content", "").lower()
                if q not in tags_str and q not in content:
                    continue
            results.append(entry)
        return results

    def get_ask(self, asset_id: str) -> Optional[Dict[str, Any]]:
        """Get the current ask price for an asset."""
        entry = self._orders.get(asset_id)
        if entry is None or not entry.is_active:
            return None
        return {
            "asset_id": asset_id,
            "price": entry.price,
            "currency": entry.currency,
            "owner_agent": entry.asset.owner_agent,
            "listed_at": entry.listed_at,
        }

    def get_order_book(self) -> List[Dict[str, Any]]:
        """Return all active listings."""
        return [
            {
                "asset_id": e.asset_id,
                "owner_agent": e.asset.owner_agent,
                "modality": e.asset.modality,
                "tags": e.asset.tags,
                "price": e.price,
                "currency": e.currency,
                "license": e.asset.license,
                "listed_at": e.listed_at,
            }
            for e in self._orders.values()
            if e.is_active
        ]

    def is_listed(self, asset_id: str) -> bool:
        entry = self._orders.get(asset_id)
        return entry is not None and entry.is_active
