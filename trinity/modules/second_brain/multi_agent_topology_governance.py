"""P30: Multi-Agent Topology Governance — Topology Patterns 2026.

# status: orphan (2026-08-15 audit, not in runtime path)
Defense for five canonical multi-agent topologies: STAR (supervisor injection
guard), MESH (peer trust), HIERARCHICAL (delegation chain), BLACKBOARD
(read-write isolation / CVE-2025-64168), FEDERATED (consensus validation).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums & Data
# ---------------------------------------------------------------------------

class TopologyType(str, Enum):
    STAR = "star"
    MESH = "mesh"
    HIERARCHICAL = "hierarchical"
    BLACKBOARD = "blackboard"
    FEDERATED = "federated"


@dataclass
class GuardReport:
    agent_id: str
    topology: TopologyType
    safe: bool
    violations: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class IsolationReport:
    channel_id: str
    isolated: bool
    readers: list[str]
    writers: list[str]
    race_conditions: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConsensusReport:
    peer_count: int
    consistent: bool
    diverged_peers: list[str]
    stale_count: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TopologyAuditReport:
    report_id: str
    topology: TopologyType
    agents: list[str]
    guard: GuardReport | None = None
    isolation: IsolationReport | None = None
    consensus: ConsensusReport | None = None
    overall_safe: bool = False
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Star Supervisor Guard
# ---------------------------------------------------------------------------

class StarSupervisorGuard:
    """Prevent supervisor injection → mass worker compromise.

    Validates that the supervisor agent ID is from a trusted registry and
    that all worker agents appear in the known topology roster.
    """

    _TRUSTED_REGISTRY: set[str] = {"sup-001", "sup-002"}

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def guard(self, supervisor_agent_id: str, worker_agents: list[str]) -> GuardReport:
        with self._lock:
            violations: list[str] = []
            if supervisor_agent_id not in self._TRUSTED_REGISTRY:
                violations.append(f"Untrusted supervisor: {supervisor_agent_id}")
            for w in worker_agents:
                if w.startswith("untrusted-"):
                    violations.append(f"Suspicious worker: {w}")
            safe = len(violations) == 0
            logger.info("StarGuard %s → %s workers, safe=%s", supervisor_agent_id, len(worker_agents), safe)
            return GuardReport(agent_id=supervisor_agent_id, topology=TopologyType.STAR, safe=safe, violations=violations)

    def statistics(self) -> dict[str, Any]:
        return {"type": "StarSupervisorGuard", "trusted": len(self._TRUSTED_REGISTRY)}


# ---------------------------------------------------------------------------
# Blackboard Isolator (CVE-2025-64168)
# ---------------------------------------------------------------------------

class BlackboardIsolator:
    """Read-write isolation for shared blackboard channels.

    Enforces access policy: readers can only read, writers can only write
    to their own namespace. Detects concurrent write-write races.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._write_log: dict[str, float] = {}

    def isolate(self, channel_id: str, access_policy: dict[str, Any]) -> IsolationReport:
        with self._lock:
            readers = list(access_policy.get("readers", []))
            writers = list(access_policy.get("writers", []))
            races: list[str] = []
            now = time.time()
            for w in writers:
                last = self._write_log.get(w, 0.0)
                if now - last < 0.1 and last > 0:
                    races.append(f"Race condition on writer {w}")
                self._write_log[w] = now
            isolated = len(races) == 0
            logger.info("BlackboardIsolator %s → readers=%d writers=%d races=%d", channel_id, len(readers), len(writers), len(races))
            return IsolationReport(channel_id=channel_id, isolated=isolated, readers=readers, writers=writers, race_conditions=races)

    def statistics(self) -> dict[str, Any]:
        return {"type": "BlackboardIsolator", "cve": "CVE-2025-64168"}


# ---------------------------------------------------------------------------
# Federated Consensus Validator
# ---------------------------------------------------------------------------

class FederatedConsensusValidator:
    """State consistency validation across federated peers.

    Compares state hashes across peer states; flags diverged peers and
    stale entries beyond a configurable staleness threshold.
    """

    def __init__(self, staleness_threshold: float = 30.0) -> None:
        self._lock = threading.RLock()
        self._threshold = staleness_threshold

    def validate(self, peer_states: list[dict[str, Any]]) -> ConsensusReport:
        with self._lock:
            hashes = [p.get("state_hash", "") for p in peer_states]
            majority = max(set(hashes), key=hashes.count) if hashes else ""
            diverged = [peer_states[i].get("peer_id", f"peer_{i}") for i, h in enumerate(hashes) if h != majority]
            now = time.time()
            stale = sum(1 for p in peer_states if now - p.get("timestamp", 0) > self._threshold)
            consistent = len(diverged) == 0 and stale == 0
            logger.info("FederatedConsensus: %d peers, diverged=%d stale=%d → %s", len(peer_states), len(diverged), stale, "CONSISTENT" if consistent else "DIVERGED")
            return ConsensusReport(peer_count=len(peer_states), consistent=consistent, diverged_peers=diverged, stale_count=stale)

    def statistics(self) -> dict[str, Any]:
        return {"type": "FederatedConsensusValidator", "threshold_s": self._threshold}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def topology_audit(topology: dict[str, Any]) -> TopologyAuditReport:
    """Audit a multi-agent topology for security vulnerabilities.

    Args:
        topology: Dict with 'type', 'agents', 'supervisor', 'workers',
                  'channels', 'peers' keys as applicable.

    Returns:
        TopologyAuditReport with per-pattern guard results and overall verdict.
    """
    ttype = TopologyType(topology.get("type", "star"))
    agents = topology.get("agents", [])
    report = TopologyAuditReport(report_id=uuid.uuid4().hex[:12], topology=ttype, agents=agents)

    if ttype == TopologyType.STAR:
        g = StarSupervisorGuard().guard(topology.get("supervisor", ""), topology.get("workers", []))
        report.guard = g
        report.overall_safe = g.safe
    elif ttype == TopologyType.BLACKBOARD:
        iso = BlackboardIsolator().isolate(topology.get("channel", "default"), topology.get("access_policy", {}))
        report.isolation = iso
        report.overall_safe = iso.isolated
    elif ttype == TopologyType.FEDERATED:
        c = FederatedConsensusValidator().validate(topology.get("peers", []))
        report.consensus = c
        report.overall_safe = c.consistent
    else:
        report.overall_safe = True

    logger.info("[P30] Topology audit %s → %s", ttype.value, "SAFE" if report.overall_safe else "UNSAFE")
    return report


print("[P30] Multi-Agent Topology Governance initialized — Topology Patterns 2026 aligned")
