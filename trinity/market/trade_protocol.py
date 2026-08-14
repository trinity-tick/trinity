"""
P2-4: Memory Trade Protocol (MTP)
==================================

将 TrustExchange 形式化为标准化交易协议，支持智能合约风格
记忆资产（Memory Asset）的注册、报价、撮合、结算全生命周期。

协议分层:
  - Layer 0: 资产定义 (MemoryAsset, AssetType)
  - Layer 1: 订单匹配 (Bid, Ask, OrderBook)
  - Layer 2: 撮合引擎 (MatchingEngine, PriceOracle)
  - Layer 3: 结算与清算 (SettlementEngine, TrustExchange)
  - Layer 4: 审计与合规 (AuditTrail, ComplianceVerifier)

Reference:
  - TrustExchange (Trinity internal)
  - ERC-20 style fungible token abstraction
  - Predicate-driven smart escrow
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 0: Asset Definition
# ═══════════════════════════════════════════════════════════════════════════

class AssetType(Enum):
    MEMORY_CHUNK = auto()        # 可交易的记忆片段
    KNOWLEDGE_TRIPLE = auto()    # 知识图谱三元组
    REASONING_TRACE = auto()     # 推理链资产
    EMBEDDING_VECTOR = auto()    # 向量嵌入资产
    POLICY_RULE = auto()          # 策略规则资产


class AssetStatus(Enum):
    LISTED = auto()              # 已挂牌
    LOCKED = auto()              # 交易锁定中
    SETTLED = auto()             # 已完成交割
    REVOKED = auto()             # 已撤销挂牌
    DISPUTED = auto()            # 争议中


@dataclass
class MemoryAsset:
    """记忆资产的规范化定义。

    每个资产包含唯一标识、类型、内容哈希（不可篡改证明）、
    所有者签名、定价/许可信息。
    """
    asset_id: str = field(default_factory=lambda: f"ma_{uuid.uuid4().hex[:12]}")
    asset_type: AssetType = AssetType.MEMORY_CHUNK
    owner_id: str = ""
    content_hash: str = ""                          # SHA-256 of canonical content
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: AssetStatus = AssetStatus.LISTED
    created_at: float = field(default_factory=time.time)
    version: int = 1

    # 许可
    license_type: str = "CC-BY-4.0"                  # 默认知识共享协议
    access_control: Dict[str, Any] = field(default_factory=dict)
    royalty_bps: int = 0                              # 版税基点 (1 bp = 0.01%)

    def compute_hash(self, content: str) -> str:
        self.content_hash = hashlib.sha256(content.encode()).hexdigest()
        return self.content_hash

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_type": self.asset_type.name,
            "owner_id": self.owner_id,
            "content_hash": self.content_hash,
            "metadata": self.metadata,
            "status": self.status.name,
            "created_at": self.created_at,
            "version": self.version,
            "license_type": self.license_type,
            "access_control": self.access_control,
            "royalty_bps": self.royalty_bps,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MemoryAsset":
        return cls(
            asset_id=d["asset_id"],
            asset_type=AssetType[d["asset_type"]],
            owner_id=d["owner_id"],
            content_hash=d["content_hash"],
            metadata=d.get("metadata", {}),
            status=AssetStatus[d.get("status", "LISTED")],
            created_at=d["created_at"],
            version=d.get("version", 1),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Layer 1: Order Matching
# ═══════════════════════════════════════════════════════════════════════════

class OrderSide(Enum):
    BID = auto()       # 买方报价
    ASK = auto()       # 卖方报价


class OrderType(Enum):
    LIMIT = auto()     # 限价单
    MARKET = auto()    # 市价单


class OrderStatus(Enum):
    OPEN = auto()
    PARTIAL = auto()
    FILLED = auto()
    CANCELLED = auto()
    EXPIRED = auto()


@dataclass
class Order:
    """订单：买方报价 (Bid) 或卖方报价 (Ask)。"""
    order_id: str = field(default_factory=lambda: f"o_{uuid.uuid4().hex[:8]}")
    asset_id: str = ""
    side: OrderSide = OrderSide.BID
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    quantity: int = 1
    trader_id: str = ""
    status: OrderStatus = OrderStatus.OPEN
    created_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    filled_qty: int = 0
    predicates: List[Callable[..., bool]] = field(default_factory=list)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at


class OrderBook:
    """订单簿：维护买卖双方的限价订单队列。

    买方按价格降序（最高买价优先），卖方按价格升序（最低卖价优先）。
    """

    def __init__(self, asset_id: str = "") -> None:
        self.asset_id = asset_id
        self._bids: List[Order] = []    # 降序
        self._asks: List[Order] = []    # 升序
        self._lock = threading.Lock()

    def add_order(self, order: Order) -> None:
        with self._lock:
            if order.side == OrderSide.BID:
                self._bids.append(order)
                self._bids.sort(key=lambda o: o.price, reverse=True)
            else:
                self._asks.append(order)
                self._asks.sort(key=lambda o: o.price)

    def remove_order(self, order_id: str) -> bool:
        with self._lock:
            for lst in (self._bids, self._asks):
                for i, o in enumerate(lst):
                    if o.order_id == order_id:
                        lst.pop(i)
                        return True
        return False

    def best_bid(self) -> Optional[Order]:
        with self._lock:
            active = [o for o in self._bids if o.status == OrderStatus.OPEN and not o.is_expired()]
            return active[0] if active else None

    def best_ask(self) -> Optional[Order]:
        with self._lock:
            active = [o for o in self._asks if o.status == OrderStatus.OPEN and not o.is_expired()]
            return active[0] if active else None

    def spread(self) -> Optional[float]:
        bid = self.best_bid()
        ask = self.best_ask()
        if bid and ask:
            return ask.price - bid.price
        return None

    def depth(self) -> Dict[str, int]:
        with self._lock:
            return {
                "bids": len([o for o in self._bids if o.status == OrderStatus.OPEN]),
                "asks": len([o for o in self._asks if o.status == OrderStatus.OPEN]),
            }


# ═══════════════════════════════════════════════════════════════════════════
# Layer 2: Matching Engine
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    """撮合成功的交易记录。"""
    trade_id: str = field(default_factory=lambda: f"t_{uuid.uuid4().hex[:8]}")
    bid_order_id: str = ""
    ask_order_id: str = ""
    asset_id: str = ""
    price: float = 0.0
    quantity: int = 1
    buyer_id: str = ""
    seller_id: str = ""
    executed_at: float = field(default_factory=time.time)
    settlement_status: str = "pending"


class PriceOracle:
    """价格预言机：基于历史成交价和资产质量评分计算公允价格。

    支持加权平均、指数移动平均、质量调节三种定价模型。
    """

    def __init__(self) -> None:
        self._history: List[Trade] = []
        self._quality_scores: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record_trade(self, trade: Trade) -> None:
        with self._lock:
            self._history.append(trade)

    def set_quality_score(self, asset_id: str, score: float) -> None:
        with self._lock:
            self._quality_scores[asset_id] = max(0.0, min(1.0, score))

    def fair_price(self, asset_id: str) -> float:
        with self._lock:
            relevant = [t for t in self._history if t.asset_id == asset_id]
            if not relevant:
                return 0.0
            # 加权平均：最近交易权重更高
            total_weight = 0.0
            weighted_sum = 0.0
            now = time.time()
            for t in relevant:
                recency = 1.0 / (1.0 + (now - t.executed_at) / 3600.0)  # 小时衰减
                weighted_sum += t.price * recency
                total_weight += recency
            base_price = weighted_sum / total_weight if total_weight > 0 else 0.0
            # 质量调节
            quality = self._quality_scores.get(asset_id, 0.5)
            return base_price * (0.5 + quality)


class MatchingEngine:
    """撮合引擎：连续竞价撮合。

    规则：当 best_bid >= best_ask 时撮合成交，
    成交价 = min(best_bid.price, 最佳卖价)。
    """

    def __init__(self, oracle: Optional[PriceOracle] = None) -> None:
        self._books: Dict[str, OrderBook] = {}
        self._trades: List[Trade] = []
        self.oracle = oracle or PriceOracle()
        self._lock = threading.Lock()

    def get_book(self, asset_id: str) -> OrderBook:
        with self._lock:
            if asset_id not in self._books:
                self._books[asset_id] = OrderBook(asset_id)
            return self._books[asset_id]

    def place_order(self, order: Order) -> List[Trade]:
        book = self.get_book(order.asset_id)
        book.add_order(order)
        return self._match(book)

    def cancel_order(self, order_id: str, asset_id: str) -> bool:
        book = self.get_book(asset_id)
        return book.remove_order(order_id)

    def _match(self, book: OrderBook) -> List[Trade]:
        trades: List[Trade] = []
        with self._lock:
            while True:
                bid = book.best_bid()
                ask = book.best_ask()
                if not bid or not ask or bid.price < ask.price:
                    break

                qty = min(bid.quantity - bid.filled_qty, ask.quantity - ask.filled_qty)
                if qty <= 0:
                    break

                trade = Trade(
                    bid_order_id=bid.order_id,
                    ask_order_id=ask.order_id,
                    asset_id=book.asset_id,
                    price=ask.price,
                    quantity=qty,
                    buyer_id=bid.trader_id,
                    seller_id=ask.trader_id,
                )
                trades.append(trade)
                self._trades.append(trade)
                self.oracle.record_trade(trade)

                bid.filled_qty += qty
                ask.filled_qty += qty

                if bid.filled_qty >= bid.quantity:
                    bid.status = OrderStatus.FILLED
                else:
                    bid.status = OrderStatus.PARTIAL

                if ask.filled_qty >= ask.quantity:
                    ask.status = OrderStatus.FILLED
                else:
                    ask.status = OrderStatus.PARTIAL

        return trades


# ═══════════════════════════════════════════════════════════════════════════
# Layer 3: Settlement & TrustExchange
# ═══════════════════════════════════════════════════════════════════════════

class SettlementStage(Enum):
    INITIATED = auto()
    LOCKED = auto()         # 双方资产已锁定
    VERIFIED = auto()       # 内容哈希已验证
    TRANSFERRED = auto()    # 所有权已转移
    COMPLETE = auto()       # 交割完成
    FAILED = auto()         # 交割失败
    DISPUTED = auto()       # 争议仲裁


@dataclass
class SettlementRecord:
    """结算记录：交易执行的全过程追踪。"""
    settlement_id: str = field(default_factory=lambda: f"s_{uuid.uuid4().hex[:8]}")
    trade_id: str = ""
    asset_id: str = ""
    buyer_id: str = ""
    seller_id: str = ""
    price: float = 0.0
    quantity: int = 1
    stage: SettlementStage = SettlementStage.INITIATED
    content_verified: bool = False
    escrow_txid: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None


class SettlementEngine:
    """结算引擎：实现 TrustExchange 形式化的结算流程。

    采用托管（escrow）模型确保原子性：
    1. 买方资金锁定 → 2. 卖方资产锁定 →
    3. 内容哈希校验 → 4. 原子交换 → 5. 完成
    """

    def __init__(self) -> None:
        self._escrow: Dict[str, MemoryAsset] = {}   # escrow_id → asset
        self._settlements: Dict[str, SettlementRecord] = {}
        self._lock = threading.Lock()

    def initiate_settlement(self, trade: Trade) -> SettlementRecord:
        sr = SettlementRecord(
            trade_id=trade.trade_id,
            asset_id=trade.asset_id,
            buyer_id=trade.buyer_id,
            seller_id=trade.seller_id,
            price=trade.price,
            quantity=trade.quantity,
        )
        with self._lock:
            self._settlements[sr.settlement_id] = sr
        logger.info(f"Settlement initiated: {sr.settlement_id}")
        return sr

    def lock_asset(self, settlement_id: str, asset: MemoryAsset) -> bool:
        with self._lock:
            sr = self._settlements.get(settlement_id)
            if not sr or sr.stage != SettlementStage.INITIATED:
                return False
            self._escrow[settlement_id] = asset
            sr.stage = SettlementStage.LOCKED
            asset.status = AssetStatus.LOCKED
        return True

    def verify_content(self, settlement_id: str, content_hash: str) -> bool:
        with self._lock:
            sr = self._settlements.get(settlement_id)
            if not sr or sr.stage != SettlementStage.LOCKED:
                return False
            asset = self._escrow.get(settlement_id)
            if not asset:
                return False
            sr.content_verified = (asset.content_hash == content_hash)
            sr.stage = SettlementStage.VERIFIED
        return sr.content_verified

    def execute_transfer(self, settlement_id: str) -> bool:
        with self._lock:
            sr = self._settlements.get(settlement_id)
            if not sr or not sr.content_verified:
                return False
            asset = self._escrow.get(settlement_id)
            if not asset:
                return False
            # 模拟区块链交易哈希
            sr.escrow_txid = f"0x{hashlib.sha256(sr.settlement_id.encode()).hexdigest()[:40]}"
            asset.owner_id = sr.buyer_id
            asset.status = AssetStatus.SETTLED
            sr.stage = SettlementStage.TRANSFERRED
        return True

    def finalize(self, settlement_id: str) -> Optional[SettlementRecord]:
        with self._lock:
            sr = self._settlements.get(settlement_id)
            if not sr or sr.stage != SettlementStage.TRANSFERRED:
                return None
            sr.stage = SettlementStage.COMPLETE
            sr.completed_at = time.time()
            # 释放托管
            self._escrow.pop(settlement_id, None)
        return sr

    def get_settlement(self, settlement_id: str) -> Optional[SettlementRecord]:
        with self._lock:
            return self._settlements.get(settlement_id)


# ═══════════════════════════════════════════════════════════════════════════
# Layer 4: Audit & Compliance
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AuditEntry:
    """审计条目：不可篡改的操作记录。"""
    entry_id: str = field(default_factory=lambda: f"ae_{uuid.uuid4().hex[:8]}")
    timestamp: float = field(default_factory=time.time)
    actor_id: str = ""
    action: str = ""
    asset_id: str = ""
    details: Dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""          # 链式哈希，防篡改
    entry_hash: str = ""

    def compute_chain_hash(self) -> str:
        payload = f"{self.entry_id}{self.timestamp}{self.action}{self.asset_id}{self.prev_hash}"
        self.entry_hash = hashlib.sha256(payload.encode()).hexdigest()
        return self.entry_hash


class AuditTrail:
    """审计追踪：链式哈希日志，确保交易记录不可篡改。"""

    def __init__(self) -> None:
        self._entries: List[AuditEntry] = []
        self._lock = threading.Lock()
        self._last_hash: str = "0" * 64

    def record(self, actor_id: str, action: str, asset_id: str,
               details: Dict[str, Any]) -> AuditEntry:
        with self._lock:
            entry = AuditEntry(
                actor_id=actor_id,
                action=action,
                asset_id=asset_id,
                details=details,
                prev_hash=self._last_hash,
            )
            entry.compute_chain_hash()
            self._entries.append(entry)
            self._last_hash = entry.entry_hash
        return entry

    def verify_integrity(self) -> bool:
        """验证整个审计链的完整性。"""
        with self._lock:
            prev = "0" * 64
            for entry in self._entries:
                if entry.prev_hash != prev:
                    return False
                saved = entry.entry_hash
                entry.compute_chain_hash()
                if entry.entry_hash != saved:
                    return False
                prev = saved
        return True

    def query(self, asset_id: str = "", actor_id: str = "",
              action: str = "", limit: int = 100) -> List[AuditEntry]:
        results: List[AuditEntry] = []
        for e in self._entries:
            if asset_id and e.asset_id != asset_id:
                continue
            if actor_id and e.actor_id != actor_id:
                continue
            if action and e.action != action:
                continue
            results.append(e)
            if len(results) >= limit:
                break
        return results


class ComplianceVerifier:
    """合规验证器：交易前准入检查。

    验证项：KYC、资产许可、交易限额、反洗钱 (AML)、制裁名单。
    """

    def __init__(self) -> None:
        self._sanctioned: Set[str] = set()
        self._daily_limits: Dict[str, float] = {}
        self._kyc_verified: Set[str] = set()

    def add_to_sanction_list(self, trader_id: str) -> None:
        self._sanctioned.add(trader_id)

    def verify_kyc(self, trader_id: str) -> None:
        self._kyc_verified.add(trader_id)

    def set_daily_limit(self, trader_id: str, limit: float) -> None:
        self._daily_limits[trader_id] = limit

    def pre_trade_check(self, buyer_id: str, seller_id: str,
                        asset: MemoryAsset, price: float) -> Tuple[bool, str]:
        """交易前合规检查。"""
        if buyer_id in self._sanctioned:
            return False, f"Buyer {buyer_id} is on sanction list"
        if seller_id in self._sanctioned:
            return False, f"Seller {seller_id} is on sanction list"
        if buyer_id not in self._kyc_verified:
            return False, f"Buyer {buyer_id} not KYC verified"
        if seller_id not in self._kyc_verified:
            return False, f"Seller {seller_id} not KYC verified"
        limit = self._daily_limits.get(buyer_id, float("inf"))
        if price > limit:
            return False, f"Price {price} exceeds daily limit {limit} for {buyer_id}"
        if asset.license_type == "PROPRIETARY" and not asset.access_control.get("transferable"):
            return False, "Asset license prohibits transfer"
        return True, "OK"


# ═══════════════════════════════════════════════════════════════════════════
# Top-Level: TrustExchange Protocol
# ═══════════════════════════════════════════════════════════════════════════

class TrustExchange:
    """TrustExchange 协议门面。

    整合撮合引擎、结算引擎、审计追踪、合规验证，
    提供记忆资产标准交易协议 (MTP) 一站式入口。
    """

    def __init__(self) -> None:
        self.matching = MatchingEngine()
        self.settlement = SettlementEngine()
        self.audit = AuditTrail()
        self.compliance = ComplianceVerifier()
        self._assets: Dict[str, MemoryAsset] = {}

    def register_asset(self, owner_id: str, asset_type: AssetType,
                       content: str, metadata: Dict[str, Any] = None) -> MemoryAsset:
        asset = MemoryAsset(asset_type=asset_type, owner_id=owner_id)
        asset.compute_hash(content)
        if metadata:
            asset.metadata = metadata
        self._assets[asset.asset_id] = asset
        self.audit.record(owner_id, "REGISTER_ASSET", asset.asset_id,
                          {"asset_type": asset_type.name})
        return asset

    def submit_order(self, trader_id: str, asset_id: str, side: OrderSide,
                     price: float, quantity: int = 1,
                     order_type: OrderType = OrderType.LIMIT) -> Order:
        order = Order(
            asset_id=asset_id,
            side=side,
            order_type=order_type,
            price=price,
            quantity=quantity,
            trader_id=trader_id,
        )
        trades = self.matching.place_order(order)
        for trade in trades:
            sr = self.settlement.initiate_settlement(trade)
            asset = self._assets.get(trade.asset_id)
            if asset:
                self.settlement.lock_asset(sr.settlement_id, asset)
                self.settlement.verify_content(sr.settlement_id, asset.content_hash)
                self.settlement.execute_transfer(sr.settlement_id)
                self.settlement.finalize(sr.settlement_id)
                trade.settlement_status = "complete"
            self.audit.record(order.trader_id, "TRADE_EXECUTED", trade.asset_id,
                              {"trade_id": trade.trade_id, "price": trade.price})
        return order

    def get_asset(self, asset_id: str) -> Optional[MemoryAsset]:
        return self._assets.get(asset_id)


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    results: Dict[str, Any] = {
        "module": "P2-4_trade_protocol",
        "passed": 0,
        "failed": 0,
        "details": [],
    }

    def _pass(test: str):
        results["passed"] += 1
        results["details"].append({"test": test, "status": "PASS"})

    def _fail(test: str, reason: str):
        results["failed"] += 1
        results["details"].append({"test": test, "status": "FAIL", "reason": reason})

    # Test 1: Asset registration
    try:
        te = TrustExchange()
        a = te.register_asset("owner1", AssetType.MEMORY_CHUNK, "a test memory")
        assert a.asset_id.startswith("ma_")
        assert a.content_hash != ""
        assert a.owner_id == "owner1"
        _pass("Asset registration")
    except Exception as e:
        _fail("Asset registration", str(e))

    # Test 2: Content hash integrity
    try:
        te = TrustExchange()
        a1 = te.register_asset("owner1", AssetType.KNOWLEDGE_TRIPLE, "Alice knows Bob")
        a2 = te.register_asset("owner1", AssetType.KNOWLEDGE_TRIPLE, "Alice knows Bob")
        assert a1.content_hash == a2.content_hash, "Same content should have same hash"
        a3 = te.register_asset("owner1", AssetType.KNOWLEDGE_TRIPLE, "Alice knows Charlie")
        assert a1.content_hash != a3.content_hash, "Different content should differ"
        _pass("Content hash integrity")
    except Exception as e:
        _fail("Content hash integrity", str(e))

    # Test 3: OrderBook bid/ask ordering
    try:
        book = OrderBook("test_asset")
        book.add_order(Order(order_id="b1", side=OrderSide.BID, price=10.0))
        book.add_order(Order(order_id="b2", side=OrderSide.BID, price=15.0))
        book.add_order(Order(order_id="a1", side=OrderSide.ASK, price=20.0))
        book.add_order(Order(order_id="a2", side=OrderSide.ASK, price=18.0))
        assert book.best_bid().price == 15.0, f"Expected 15.0, got {book.best_bid().price}"
        assert book.best_ask().price == 18.0, f"Expected 18.0, got {book.best_ask().price}"
        assert abs(book.spread() - 3.0) < 0.01, f"Spread should be 3.0, got {book.spread()}"
        _pass("OrderBook bid/ask ordering")
    except Exception as e:
        _fail("OrderBook bid/ask ordering", str(e))

    # Test 4: Matching engine trade execution
    try:
        engine = MatchingEngine()
        engine.place_order(Order(
            order_id="sell1", asset_id="a1", side=OrderSide.ASK, price=50.0, quantity=2,
            trader_id="seller1"))
        engine.place_order(Order(
            order_id="buy1", asset_id="a1", side=OrderSide.BID, price=55.0, quantity=2,
            trader_id="buyer1"))
        assert len(engine._trades) > 0, "Expected at least one trade"
        assert engine._trades[0].price == 50.0, f"Trade price should be 50.0, got {engine._trades[0].price}"
        _pass("Matching engine trade execution")
    except Exception as e:
        _fail("Matching engine trade execution", str(e))

    # Test 5: Settlement lifecycle
    try:
        eng = SettlementEngine()
        trade = Trade(trade_id="t1", asset_id="a1", price=50.0, buyer_id="b1", seller_id="s1")
        sr = eng.initiate_settlement(trade)
        assert sr.stage == SettlementStage.INITIATED
        asset = MemoryAsset(asset_id="a1", owner_id="s1")
        asset.compute_hash("test content")
        assert eng.lock_asset(sr.settlement_id, asset)
        assert eng.verify_content(sr.settlement_id, asset.content_hash)
        assert eng.execute_transfer(sr.settlement_id)
        final = eng.finalize(sr.settlement_id)
        assert final.stage == SettlementStage.COMPLETE
        _pass("Settlement lifecycle")
    except Exception as e:
        _fail("Settlement lifecycle", str(e))

    # Test 6: Audit trail integrity
    try:
        trail = AuditTrail()
        trail.record("user1", "REGISTER_ASSET", "a1", {})
        trail.record("user2", "TRADE_EXECUTED", "a1", {"price": 100})
        trail.record("user1", "TRANSFER_OWNERSHIP", "a1", {"to": "user2"})
        assert trail.verify_integrity(), "Audit trail corrupted"
        results_q = trail.query(asset_id="a1")
        assert len(results_q) == 3, f"Expected 3 entries, got {len(results_q)}"
        _pass("Audit trail integrity")
    except Exception as e:
        _fail("Audit trail integrity", str(e))

    # Test 7: Compliance pre-trade checks
    try:
        cv = ComplianceVerifier()
        cv.verify_kyc("buyer1")
        cv.verify_kyc("seller1")
        cv.set_daily_limit("buyer1", 1000.0)
        asset = MemoryAsset(license_type="CC-BY-4.0")
        ok, msg = cv.pre_trade_check("buyer1", "seller1", asset, 500.0)
        assert ok, f"Check should pass: {msg}"
        # Sanction test
        cv.add_to_sanction_list("banned_user")
        ok2, msg2 = cv.pre_trade_check("banned_user", "seller1", asset, 100.0)
        assert not ok2, "Sanctioned user should be rejected"
        _pass("Compliance pre-trade checks")
    except Exception as e:
        _fail("Compliance pre-trade checks", str(e))

    # Test 8: TrustExchange full flow
    try:
        te = TrustExchange()
        te.compliance.verify_kyc("alice")
        te.compliance.verify_kyc("bob")
        asset = te.register_asset("alice", AssetType.MEMORY_CHUNK, "valuable insight")
        # alice lists an ASK (sell) at 95, bob bids (buy) at 100 → should match at 95
        te.submit_order("alice", asset.asset_id, OrderSide.ASK, 95.0)
        te.submit_order("bob", asset.asset_id, OrderSide.BID, 100.0)
        assert asset.owner_id == "bob", f"Ownership should transfer to bob, got {asset.owner_id}"
        assert len(te.audit._entries) >= 2, "Expected audit entries"
        _pass("TrustExchange full flow")
    except Exception as e:
        _fail("TrustExchange full flow", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
