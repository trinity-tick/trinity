"""Order Book — decentralised memory marketplace listing facility.

Maintains an in-memory order book of MemoryAsset listings with support
for listing, delisting, searching, and price queries.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json
import os
from pathlib import Path


# 2026-08-16 修复:订单簿 JSON 持久化 —— API 重启后挂单不再丢失。
_ORDERBOOK_FILE = os.path.join(
    os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity")),
    "memory_market_orderbook.json",
)


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
        self._load()

    def _load(self) -> None:
        """Load persisted orders from JSON file (2026-08-16)."""
        if os.environ.get("TRINITY_TESTING") == "1":
            return  # 测试隔离:不加载真实持久化文件
        try:
            if not os.path.exists(_ORDERBOOK_FILE):
                return
            with open(_ORDERBOOK_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            from trinity.market.memory_asset import MemoryAsset
            for aid, d in (data or {}).items():
                try:
                    asset = MemoryAsset.from_dict(d.get("asset", {}))
                    self._orders[aid] = OrderEntry(
                        asset_id=aid,
                        asset=asset,
                        price=float(d.get("price", 0.0)),
                        currency=d.get("currency", "trust_score"),
                        listed_at=d.get("listed_at", ""),
                        is_active=bool(d.get("is_active", True)),
                    )
                except Exception:
                    continue
        except Exception:
            pass

    def _save(self) -> None:
        """Persist orders to JSON file (2026-08-16)."""
        if os.environ.get("TRINITY_TESTING") == "1":
            return  # 测试隔离:不写真实持久化文件
        try:
            os.makedirs(os.path.dirname(_ORDERBOOK_FILE), exist_ok=True)
            payload = {}
            for aid, e in self._orders.items():
                payload[aid] = {
                    "asset": e.asset.to_dict() if hasattr(e.asset, "to_dict") else {"memory_id": e.asset_id},
                    "price": e.price,
                    "currency": e.currency,
                    "listed_at": e.listed_at,
                    "is_active": e.is_active,
                }
            with open(_ORDERBOOK_FILE, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

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
        self._save()
        return entry

    def delist_asset(self, asset_id: str) -> bool:
        """Remove a listing (soft-delete — marks inactive)."""
        entry = self._orders.get(asset_id)
        if entry is None:
            return False
        entry.is_active = False
        self._save()
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

    def best_ask(self) -> Optional[Dict[str, Any]]:
        """最低价活跃挂单（最优卖价）；无活跃挂单返回 None。

        2026-08-22 收尾（market_sim 冷启动建议③的落地基石）：正式撮合器
        抽离前，先提供最优价原语，买方可按最优卖价出价。
        """
        active = [e for e in self._orders.values() if e.is_active]
        if not active:
            return None
        best = min(active, key=lambda e: e.price)
        return {
            "asset_id": best.asset_id,
            "price": best.price,
            "currency": best.currency,
            "owner_agent": best.asset.owner_agent,
            "listed_at": best.listed_at,
        }
