"""Trinity Memory Market — Agent-to-Agent Memory Exchange.

Decentralised infrastructure for listing, searching, buying, and selling
memory assets between agents, backed by trust-score currency and a
reputation ledger.

Exports
-------
- MemoryAsset, create_asset, verify_asset_integrity, get_asset_metadata
- OrderBook, OrderEntry
- TrustExchange, CurrencyLedger, Transaction
- ReputationEngine, ReputationScore
- Pricing: estimate_value, get_market_price
"""

from .memory_asset import (
    MemoryAsset,
    create_asset,
    verify_asset_integrity,
    get_asset_metadata,
)
from .orderbook import OrderBook, OrderEntry
from .pricing import estimate_value, get_market_price
from .reputation import ReputationEngine, ReputationScore
from .trust_exchange import TrustExchange, CurrencyLedger, Transaction
from .trade_protocol import (
    MatchingEngine as MatchEngine,
    SettlementEngine,
    AuditTrail,
    ComplianceVerifier,
    PriceOracle,
    Order as ProtocolOrder,
    Trade as ProtocolTrade,
    AssetType as ProtocolAssetType,
    AssetStatus as ProtocolAssetStatus,
    self_test as trade_protocol_self_test,
)

__all__ = [
    # Assets
    "MemoryAsset",
    "create_asset",
    "verify_asset_integrity",
    "get_asset_metadata",
    # Market
    "OrderBook",
    "OrderEntry",
    # Trading
    "TrustExchange",
    "CurrencyLedger",
    "Transaction",
    # Reputation
    "ReputationEngine",
    "ReputationScore",
    # Pricing
    "estimate_value",
    "get_market_price",
    # P2-4 Trade Protocol
    "MatchEngine",
    "SettlementEngine",
    "AuditTrail",
    "ComplianceVerifier",
    "PriceOracle",
    "ProtocolOrder",
    "ProtocolTrade",
    "ProtocolAssetType",
    "ProtocolAssetStatus",
    "trade_protocol_self_test",
]
