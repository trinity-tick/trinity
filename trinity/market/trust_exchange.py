"""Trust Exchange — atomic trading engine for the memory market.

Implements buy-side order execution with balance checks, seller
verification, asset availability, and multi-currency support.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .orderbook import OrderBook
from .reputation import ReputationEngine
import json
import os
from pathlib import Path


# 2026-08-16 修复(P1):交易引擎状态 JSON 持久化 —— API 重启后余额/交易历史不再丢失。
_TRUST_EXCHANGE_FILE = os.path.join(
    os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity")),
    "memory_market_trust_exchange.json",
)



# ── Currencies ────────────────────────────────────────────────────────

@dataclass
class TrustBalance:
    """Agent's balance sheet across the three trust currencies."""

    agent_id: str
    trust_score: float = 100.0
    audit_trail: int = 0
    anchor_token: int = 0


class CurrencyLedger:
    """Simple ledger tracking agent balances."""

    def __init__(self):
        self._balances: Dict[str, TrustBalance] = {}
        self._lock = threading.RLock()

    def ensure(self, agent_id: str) -> TrustBalance:
        with self._lock:
            if agent_id not in self._balances:
                self._balances[agent_id] = TrustBalance(agent_id=agent_id)
            return self._balances[agent_id]

    def get_balance(self, agent_id: str, currency: str) -> float:
        bal = self.ensure(agent_id)
        return getattr(bal, currency, 0.0)

    def deduct(self, agent_id: str, currency: str, amount: float) -> bool:
        with self._lock:
            bal = self.ensure(agent_id)
            current = getattr(bal, currency)
            if current < amount:
                return False
            setattr(bal, currency, current - amount)
            return True

    def credit(self, agent_id: str, currency: str, amount: float) -> None:
        with self._lock:
            bal = self.ensure(agent_id)
            setattr(bal, currency, getattr(bal, currency) + amount)


# ── Transaction record ────────────────────────────────────────────────

@dataclass
class Transaction:
    tx_id: str
    buyer_agent: str
    seller_agent: str
    asset_id: str
    price: float
    currency: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = "completed"  # completed | failed


# ── Engine ────────────────────────────────────────────────────────────

class TrustExchange:
    """Trust-based trading engine.

    Supports three currency types:
      - trust_score : Reputation-derived trust points (default)
      - audit_trail : Audit trail credits
      - anchor_token : Identity anchor tokens
    """

    VALID_CURRENCIES = {"trust_score", "audit_trail", "anchor_token"}

    def __init__(self, orderbook: OrderBook, reputation: ReputationEngine):
        self.orderbook = orderbook
        self.reputation = reputation
        self.ledger = CurrencyLedger()
        self._history: Dict[str, Transaction] = {}
        self._agent_history: Dict[str, List[str]] = {}  # agent_id -> [tx_id, ...]
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """加载持久化交易状态(P1,2026-08-16)。"""
        if os.environ.get("TRINITY_TESTING") == "1":
            return  # 测试隔离:不加载真实持久化文件
        try:
            if not os.path.exists(_TRUST_EXCHANGE_FILE):
                return
            with open(_TRUST_EXCHANGE_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            for agent, b in (d.get("balances") or {}).items():
                self.ledger._balances[agent] = TrustBalance(**{k: v for k, v in b.items() if k in TrustBalance.__dataclass_fields__})
            for txid, t in (d.get("history") or {}).items():
                self._history[txid] = Transaction(**{k: v for k, v in t.items() if k in Transaction.__dataclass_fields__})
            self._agent_history = d.get("agent_history") or {}
        except Exception:
            pass

    def _save(self) -> None:
        """持久化交易状态(P1,2026-08-16)。"""
        if os.environ.get("TRINITY_TESTING") == "1":
            return  # 测试隔离:不写真实持久化文件
        try:
            os.makedirs(os.path.dirname(_TRUST_EXCHANGE_FILE), exist_ok=True)
            with open(_TRUST_EXCHANGE_FILE, "w", encoding="utf-8") as fh:
                json.dump({
                    "balances": {a: {"agent_id": b.agent_id, "trust_score": b.trust_score,
                                      "audit_trail": b.audit_trail, "anchor_token": b.anchor_token}
                                for a, b in self.ledger._balances.items()},
                    "history": {t.tx_id: {"tx_id": t.tx_id, "buyer_agent": t.buyer_agent, "seller_agent": t.seller_agent,
                                           "asset_id": t.asset_id, "price": t.price, "currency": t.currency,
                                           "timestamp": t.timestamp, "status": t.status}
                                for t in self._history.values()},
                    "agent_history": self._agent_history,
                }, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── Trading ───────────────────────────────────────────────────────

    def buy_asset(
        self,
        buyer_agent: str,
        asset_id: str,
        offer_price: float,
        currency: str = "trust_score",
    ) -> Transaction:
        """Buy a listed asset — validation + atomic trade."""
        if currency not in self.VALID_CURRENCIES:
            raise ValueError(f"Invalid currency '{currency}'. Must be one of {self.VALID_CURRENCIES}")

        # 1. Asset existence & active listing
        entry = self.orderbook._orders.get(asset_id)
        if entry is None or not entry.is_active:
            raise ValueError(f"Asset {asset_id} is not listed or inactive")

        seller_agent = entry.asset.owner_agent
        if buyer_agent == seller_agent:
            raise ValueError("Cannot buy your own asset")

        ask_price = entry.price

        # 2. Price check
        if offer_price < ask_price:
            raise ValueError(
                f"Offer {offer_price} below asking price {ask_price} "
                f"(currency: {currency})"
            )

        # 3. Buyer balance check
        balance = self.ledger.get_balance(buyer_agent, currency)
        if balance < offer_price:
            raise ValueError(
                f"Insufficient {currency} balance: "
                f"have {balance}, need {offer_price}"
            )

        # 4. Execute atomic trade
        tx_id = f"tx_{buyer_agent}_{seller_agent}_{asset_id}_{int(time.time())}"

        with self._lock:
            # Double-check asset still available
            entry = self.orderbook._orders.get(asset_id)
            if entry is None or not entry.is_active:
                raise ValueError(f"Asset {asset_id} was delisted during trade")

            # Deduct buyer, credit seller
            if not self.ledger.deduct(buyer_agent, currency, offer_price):
                raise ValueError("Balance changed during trade — insufficient funds")
            self.ledger.credit(seller_agent, currency, offer_price)

            # Delist (asset consumed)
            self.orderbook.delist_asset(asset_id)

            # Record transaction
            tx = Transaction(
                tx_id=tx_id,
                buyer_agent=buyer_agent,
                seller_agent=seller_agent,
                asset_id=asset_id,
                price=offer_price,
                currency=currency,
            )
            self._history[tx_id] = tx
            self._agent_history.setdefault(buyer_agent, []).append(tx_id)
            self._agent_history.setdefault(seller_agent, []).append(tx_id)

            # Update reputation
            self.reputation.record_trade_success(buyer_agent)
            self.reputation.record_trade_success(seller_agent)

        self._save()
        return tx

    def trade(
        self,
        buyer: str,
        seller: str,
        asset_id: str,
        price: float,
        currency: str = "trust_score",
    ) -> Transaction:
        """Alias for buy_asset — direct trade call."""
        return self.buy_asset(buyer, asset_id, price, currency)

    # ── History ───────────────────────────────────────────────────────

    def get_transaction_history(
        self,
        agent_id: str,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        tx_ids = self._agent_history.get(agent_id, [])
        result = []
        for tx_id in tx_ids[-limit:]:
            tx = self._history.get(tx_id)
            if tx:
                result.append({
                    "tx_id": tx.tx_id,
                    "buyer_agent": tx.buyer_agent,
                    "seller_agent": tx.seller_agent,
                    "asset_id": tx.asset_id,
                    "price": tx.price,
                    "currency": tx.currency,
                    "timestamp": tx.timestamp,
                    "status": tx.status,
                })
        return result

    # ── Balance ───────────────────────────────────────────────────────

    def get_balance(self, agent_id: str) -> Dict[str, Any]:
        bal = self.ledger.ensure(agent_id)
        return {
            "agent_id": bal.agent_id,
            "trust_score": bal.trust_score,
            "audit_trail": bal.audit_trail,
            "anchor_token": bal.anchor_token,
        }
