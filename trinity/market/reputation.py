"""Reputation Engine — decentralised agent reputation system.

Maintains a reputation ledger with multi-factor scoring, endorsements,
reports, and time-decay for inactive agents. Reputation governs
trust-score currency used in market trades.
"""

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import json
import os
from pathlib import Path


# 2026-08-16 修复(P1):信誉账本 JSON 持久化 —— API 重启后信誉状态不再丢失。
_REPUTATION_FILE = os.path.join(
    os.environ.get("TRINITY_HOME", str(Path.home() / ".trinity")),
    "memory_market_reputation.json",
)


def _entry_to_dict(e):
    return {"event_id": e.event_id, "agent_id": e.agent_id, "event_type": e.event_type,
            "from_agent": e.from_agent, "reason": e.reason, "timestamp": e.timestamp}


def _entry_from_dict(d):
    return ReputationEntry(**{k: v for k, v in d.items() if k in ReputationEntry.__dataclass_fields__})



# ── Data structures ───────────────────────────────────────────────────

@dataclass
class ReputationScore:
    agent_id: str
    score: float            # 0.0–1.0
    trade_success_rate: float
    asset_quality: float
    audit_violations: int
    community_rating: float
    endorsements: int
    reports: int
    last_active: str
    computed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "score": round(self.score, 4),
            "trade_success_rate": round(self.trade_success_rate, 4),
            "asset_quality": round(self.asset_quality, 4),
            "audit_violations": self.audit_violations,
            "community_rating": round(self.community_rating, 4),
            "endorsements": self.endorsements,
            "reports": self.reports,
            "last_active": self.last_active,
            "computed_at": self.computed_at,
        }


@dataclass
class ReputationEntry:
    """Single event on the reputation ledger."""

    event_id: str
    agent_id: str
    event_type: str       # endorse | report | trade_success | trade_fail
    from_agent: str       # "" for system events
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── Engine ────────────────────────────────────────────────────────────

class ReputationEngine:
    """Decentralised agent reputation engine.

    Parameters
    ----------
    decay_half_life_days : float
        Inactive agents lose reputation with this half-life (default 30 days).
    """

    def __init__(self, decay_half_life_days: float = 30.0):
        self.decay_half_life_days = decay_half_life_days
        self._ledger: Dict[str, List[ReputationEntry]] = {}
        self._trade_stats: Dict[str, Dict[str, int]] = {}  # agent -> {success, fail}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        """加载持久化信誉状态(P1,2026-08-16)。"""
        if os.environ.get("TRINITY_TESTING") == "1":
            return  # 测试隔离:不加载真实持久化文件
        try:
            if not os.path.exists(_REPUTATION_FILE):
                return
            with open(_REPUTATION_FILE, encoding="utf-8") as fh:
                d = json.load(fh)
            for agent, entries in (d.get("ledger") or {}).items():
                self._ledger[agent] = [_entry_from_dict(e) for e in entries]
            self._trade_stats = d.get("trade_stats") or {}
        except Exception:
            pass

    def _save(self) -> None:
        """持久化信誉状态(P1,2026-08-16)。"""
        if os.environ.get("TRINITY_TESTING") == "1":
            return  # 测试隔离:不写真实持久化文件
        try:
            os.makedirs(os.path.dirname(_REPUTATION_FILE), exist_ok=True)
            with open(_REPUTATION_FILE, "w", encoding="utf-8") as fh:
                json.dump({
                    "ledger": {a: [_entry_to_dict(e) for e in es] for a, es in self._ledger.items()},
                    "trade_stats": self._trade_stats,
                }, fh, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ── Core scoring ──────────────────────────────────────────────────

    def calculate_reputation(self, agent_id: str) -> ReputationScore:
        """Multi-factor reputation score.

        Factors:
          - trade_success_rate (30%)
          - asset_quality (25%)
          - community_rating (25%)
          - audit_penalty (20%, subtractive)
        """
        events = self._ledger.get(agent_id, [])
        trades = self._trade_stats.get(agent_id, {"success": 0, "fail": 0})

        # Trade success rate
        total_trades = trades["success"] + trades["fail"]
        trade_rate = trades["success"] / max(total_trades, 1)

        # Asset quality — endorsements / (endorsements + reports)
        endorsements = sum(1 for e in events if e.event_type == "endorse")
        reports = sum(1 for e in events if e.event_type == "report")
        quality = endorsements / max(endorsements + reports, 1)

        # Community rating — endorsements weighted, reports penalised
        community = max(0.0, (endorsements - reports * 0.5) / max(endorsements + reports, 1))

        # Audit penalty — each violation subtracts
        audit_violations = sum(1 for e in events if e.event_type == "audit_violation")
        audit_penalty = min(audit_violations * 0.1, 0.5)

        # Time decay for inactivity
        activity_bonus = 1.0
        if events:
            last_ts = max(e.timestamp for e in events)
            try:
                last_dt = datetime.fromisoformat(last_ts)
                age_days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400.0
                activity_bonus = 2.0 ** (-age_days / self.decay_half_life_days)
            except Exception:
                pass

        raw = (
            trade_rate * 0.30
            + quality * 0.25
            + community * 0.25
            - audit_penalty
        )
        score = round(max(0.0, min(raw * activity_bonus, 1.0)), 4)

        return ReputationScore(
            agent_id=agent_id,
            score=score,
            trade_success_rate=round(trade_rate, 4),
            asset_quality=round(quality, 4),
            audit_violations=audit_violations,
            community_rating=round(community, 4),
            endorsements=endorsements,
            reports=reports,
            last_active=events[-1].timestamp if events else "",
        )

    # ── Ledger mutations ──────────────────────────────────────────────

    def _record(self, entry: ReputationEntry) -> None:
        with self._lock:
            self._ledger.setdefault(entry.agent_id, []).append(entry)
        self._save()

    def endorse_agent(self, from_agent: str, to_agent: str, reason: str = "") -> ReputationEntry:
        entry = ReputationEntry(
            event_id=f"endorse_{to_agent}_{int(time.time())}",
            agent_id=to_agent,
            event_type="endorse",
            from_agent=from_agent,
            reason=reason,
        )
        self._record(entry)
        return entry

    def report_agent(self, from_agent: str, to_agent: str, reason: str = "") -> ReputationEntry:
        entry = ReputationEntry(
            event_id=f"report_{to_agent}_{int(time.time())}",
            agent_id=to_agent,
            event_type="report",
            from_agent=from_agent,
            reason=reason,
        )
        self._record(entry)
        return entry

    def record_trade_success(self, agent_id: str) -> None:
        with self._lock:
            stats = self._trade_stats.setdefault(agent_id, {"success": 0, "fail": 0})
            stats["success"] += 1
        self._record(ReputationEntry(
            event_id=f"trade_succ_{agent_id}_{int(time.time())}",
            agent_id=agent_id,
            event_type="trade_success",
            from_agent="",
            reason="",
        ))

    def record_trade_fail(self, agent_id: str) -> None:
        with self._lock:
            stats = self._trade_stats.setdefault(agent_id, {"success": 0, "fail": 0})
            stats["fail"] += 1
        self._record(ReputationEntry(
            event_id=f"trade_fail_{agent_id}_{int(time.time())}",
            agent_id=agent_id,
            event_type="trade_fail",
            from_agent="",
            reason="",
        ))

    def record_audit_violation(self, agent_id: str, reason: str = "") -> None:
        self._record(ReputationEntry(
            event_id=f"audit_{agent_id}_{int(time.time())}",
            agent_id=agent_id,
            event_type="audit_violation",
            from_agent="",
            reason=reason,
        ))

    # ── Queries ───────────────────────────────────────────────────────

    def get_reputation_ledger(self, agent_id: str) -> List[Dict[str, Any]]:
        events = self._ledger.get(agent_id, [])
        return [{
            "event_id": e.event_id,
            "event_type": e.event_type,
            "from_agent": e.from_agent,
            "reason": e.reason,
            "timestamp": e.timestamp,
        } for e in events]

    def get_trade_balance(self, agent_id: str) -> Dict[str, int]:
        return dict(self._trade_stats.get(agent_id, {"success": 0, "fail": 0}))
