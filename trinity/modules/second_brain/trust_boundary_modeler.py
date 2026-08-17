"""P30: Trust Boundary Modeler — Agent Trust Boundary Design 2026.

# status: orphan (2026-08-15 audit, not in runtime path)
Models agent trust boundaries with coupling types (tight/loose/blackboard),
attack surface enumeration, trust degradation protocol (degrade→isolate→
recover), and per-boundary firewall decisions (ALLOW/DENY/AUDIT).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data Classes & Enums
# ---------------------------------------------------------------------------

CouplingType = Literal["tight", "loose", "blackboard"]


class FirewallDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    AUDIT = "audit"


@dataclass
class TrustBoundary:
    boundary_id: str
    source_agent: str
    target_agent: str
    coupling_type: CouplingType
    risk_score: float  # 0.0–1.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AttackSurfaceMap:
    map_id: str
    boundaries: list[TrustBoundary]
    entry_points: list[str]
    high_risk_paths: list[str]
    total_risk_score: float = 0.0
    recommendations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class DegradationEvent:
    agent_id: str
    from_level: str
    to_level: str
    reason: str
    recoverable: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProtectionPlan:
    plan_id: str
    agent_graph: dict[str, Any]
    boundaries: list[TrustBoundary]
    attack_surface: AttackSurfaceMap | None = None
    degradation_log: list[DegradationEvent] = field(default_factory=list)
    firewall_rules: dict[str, FirewallDecision] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Attack Surface Modeler
# ---------------------------------------------------------------------------

class AttackSurfaceModeler:
    """Enumerate attack surface from trust boundaries.

    Each boundary is a potential attack vector; high-risk couplings
    (tight + high risk_score) are flagged as entry points. Outputs
    an AttackSurfaceMap with ranked risk paths.
    """

    _RISK_THRESHOLD: float = 0.6

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def model(self, boundaries: list[TrustBoundary]) -> AttackSurfaceMap:
        with self._lock:
            entry_points: list[str] = []
            high_risk: list[str] = []
            total = 0.0
            for b in boundaries:
                total += b.risk_score
                if b.risk_score >= self._RISK_THRESHOLD:
                    entry_points.append(f"{b.source_agent}→{b.target_agent}")
                if b.coupling_type == "tight" and b.risk_score >= self._RISK_THRESHOLD:
                    high_risk.append(f"{b.source_agent}→{b.target_agent} (tight, risk={b.risk_score:.2f})")

            recs: list[str] = []
            if high_risk:
                recs.append(f"Add firewall rules for {len(high_risk)} high-risk tight-coupling boundaries")
            if entry_points:
                recs.append(f"Harden {len(entry_points)} entry point boundaries")

            amap = AttackSurfaceMap(
                map_id=uuid.uuid4().hex[:12], boundaries=boundaries,
                entry_points=entry_points, high_risk_paths=high_risk,
                total_risk_score=round(total, 2), recommendations=recs,
            )
            logger.info("AttackSurfaceModeler: %d boundaries → %d entry points, total risk %.2f", len(boundaries), len(entry_points), total)
            return amap

    def statistics(self) -> dict[str, Any]:
        return {"type": "AttackSurfaceModeler", "risk_threshold": self._RISK_THRESHOLD}


# ---------------------------------------------------------------------------
# Trust Degradation Protocol
# ---------------------------------------------------------------------------

class TrustDegradationProtocol:
    """Automated trust degradation: degrade → isolate → recover.

    Tracks trust levels per agent (trusted/suspicious/isolated). On
    degradation, emits an event; recovery requires explicit signal.
    """

    _LEVELS: list[str] = ["trusted", "suspicious", "isolated"]

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: dict[str, str] = {}
        self._history: list[DegradationEvent] = []

    def degrade(self, agent_id: str, reason: str) -> DegradationEvent:
        with self._lock:
            current = self._state.get(agent_id, "trusted")
            idx = self._LEVELS.index(current) if current in self._LEVELS else 0
            next_idx = min(idx + 1, len(self._LEVELS) - 1)
            new_level = self._LEVELS[next_idx]
            self._state[agent_id] = new_level
            event = DegradationEvent(agent_id=agent_id, from_level=current, to_level=new_level, reason=reason, recoverable=new_level != "isolated")
            self._history.append(event)
            logger.warning("Trust degradation: %s %s→%s (%s)", agent_id, current, new_level, reason)
            return event

    def statistics(self) -> dict[str, Any]:
        return {"type": "TrustDegradationProtocol", "agents": len(self._state), "events": len(self._history)}


# ---------------------------------------------------------------------------
# Boundary Firewall
# ---------------------------------------------------------------------------

class BoundaryFirewall:
    """Per-boundary firewall: ALLOW/DENY/AUDIT decisions.

    Checks request against boundary's coupling_type and risk_score.
    Tight coupling with high risk → DENY; loose/blackboard → AUDIT;
    low-risk tight → ALLOW.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def firewall(self, request: dict[str, Any], boundary: TrustBoundary) -> FirewallDecision:
        with self._lock:
            if boundary.risk_score >= 0.8:
                decision = FirewallDecision.DENY
            elif boundary.coupling_type == "tight" and boundary.risk_score >= 0.5:
                decision = FirewallDecision.AUDIT
            elif boundary.risk_score >= 0.5:
                decision = FirewallDecision.AUDIT
            else:
                decision = FirewallDecision.ALLOW
            logger.info("BoundaryFirewall %s: %s (risk=%.2f, coupling=%s)", boundary.boundary_id, decision.value, boundary.risk_score, boundary.coupling_type)
            return decision

    def statistics(self) -> dict[str, Any]:
        return {"type": "BoundaryFirewall"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def model_and_protect(agent_graph: dict[str, Any]) -> ProtectionPlan:
    """Full trust boundary modeling and protection pipeline.

    Extracts trust boundaries from agent graph, models attack surface,
    applies degradation protocol to high-risk agents, and generates
    per-boundary firewall rules.

    Args:
        agent_graph: Dict with 'agents' and 'edges' lists.

    Returns:
        ProtectionPlan with attack surface map, degradation log, firewall rules.
    """
    agents = agent_graph.get("agents", [])
    edges = agent_graph.get("edges", [])
    boundaries = [
        TrustBoundary(
            boundary_id=f"bnd-{i:03d}",
            source_agent=e.get("source", f"agent_{i}"),
            target_agent=e.get("target", f"agent_{i+1}"),
            coupling_type=e.get("coupling", "loose"),
            risk_score=min(1.0, e.get("risk", 0.3)),
        )
        for i, e in enumerate(edges)
    ]

    modeler = AttackSurfaceModeler()
    amap = modeler.model(boundaries)

    degrader = TrustDegradationProtocol()
    deg_events: list[DegradationEvent] = []
    for b in boundaries:
        if b.risk_score >= 0.7:
            deg_events.append(degrader.degrade(b.source_agent, f"Risk score {b.risk_score:.2f} exceeds threshold"))

    fw = BoundaryFirewall()
    rules: dict[str, FirewallDecision] = {}
    for b in boundaries:
        rules[b.boundary_id] = fw.firewall({}, b)

    plan = ProtectionPlan(
        plan_id=uuid.uuid4().hex[:12], agent_graph=agent_graph,
        boundaries=boundaries, attack_surface=amap,
        degradation_log=deg_events, firewall_rules=rules,
    )
    logger.info("[P30] Trust boundary model+protect: %d boundaries, %d degraded, %d rules", len(boundaries), len(deg_events), len(rules))
    return plan


print("[P30] Trust Boundary Modeler initialized — Agent Trust Boundary Design 2026 aligned")
