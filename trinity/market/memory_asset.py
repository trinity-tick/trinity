"""Memory Asset — encapsulate a memory as a tradeable asset.

A MemoryAsset wraps a Trinity memory with ownership, pricing, licensing,
and integrity verification metadata, making it suitable for listing on
the memory market order book.
"""

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


# ── Licences ──────────────────────────────────────────────────────────

_MARKET_LICENSES = {
    "CC-BY": "Creative Commons Attribution — free to share and adapt with credit",
    "CC-BY-SA": "Creative Commons Attribution-ShareAlike — same as CC-BY, adaptations must share alike",
    "CC0": "Public Domain Dedication — no rights reserved",
    "TRINITY-SINGLE": "Single-agent non-transferable internal use",
    "TRINITY-SHARED": "Multi-agent shareable within the same tenant",
    "TRINITY-CROSS": "Cross-tenant shareable across the Trinity network",
}


# ── Asset ─────────────────────────────────────────────────────────────

@dataclass
class MemoryAsset:
    """A tradeable memory asset on the Trinity market."""

    memory_id: str
    owner_agent: str
    content_hash: str
    modality: str = "text"         # text | image | code | structured
    tags: List[str] = field(default_factory=list)
    price: float = 0.0
    license: str = "CC-BY"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Optional raw memory reference (not serialised for transport)
    _memory: Optional[Dict[str, Any]] = field(default=None, repr=False)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "owner_agent": self.owner_agent,
            "content_hash": self.content_hash,
            "modality": self.modality,
            "tags": self.tags,
            "price": self.price,
            "license": self.license,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryAsset":
        return cls(
            memory_id=d["memory_id"],
            owner_agent=d.get("owner_agent", ""),
            content_hash=d.get("content_hash", ""),
            modality=d.get("modality", "text"),
            tags=d.get("tags", []),
            price=d.get("price", 0.0),
            license=d.get("license", "CC-BY"),
            created_at=d.get("created_at", ""),
        )


# ── Factory & verification ────────────────────────────────────────────

def _hash_content(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def create_asset(
    memory: Dict[str, Any],
    owner: str,
    price: float = 0.0,
    license: str = "CC-BY",
) -> MemoryAsset:
    """Wrap a Trinity memory dict into a tradeable MemoryAsset.

    Parameters
    ----------
    memory : dict
        Raw memory dict from the Trinity adapter.
    owner : str
        Agent ID of the memory owner.
    price : float
        Listing price (0 = free).
    license : str
        One of the TRINITY-* or Creative Commons licences.

    Returns
    -------
    MemoryAsset
    """
    content = memory.get("content", "")
    return MemoryAsset(
        memory_id=memory.get("memory_id", ""),
        owner_agent=owner,
        content_hash=_hash_content(content),
        modality=memory.get("category", "text"),
        tags=memory.get("tags", []) if isinstance(memory.get("tags"), list) else [],
        price=price,
        license=license,
        _memory=memory,
    )


def verify_asset_integrity(asset: MemoryAsset, memory: Dict[str, Any]) -> bool:
    """Check whether *memory* content matches the asset's content_hash."""
    content = memory.get("content", "")
    return _hash_content(content) == asset.content_hash


def get_asset_metadata(asset_id: str, asset: MemoryAsset) -> Dict[str, Any]:
    """Return human-readable metadata for a listed asset."""
    return {
        "asset_id": asset_id,
        **asset.to_dict(),
        "license_label": _MARKET_LICENSES.get(asset.license, "Unknown"),
    }
