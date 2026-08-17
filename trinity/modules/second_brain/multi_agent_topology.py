"""
# status: orphan (2026-08-15 audit, not in runtime path)
P12-1: Multi-Agent Memory Topology Coordinator.

Reference: arXiv 2606.04197 — Network Topology x Memory Depth Interaction.
Design: Models how different network structures (star / fully-connected / ring / random)
        influence consensus speed under varying memory depth, detects the
        "rapid-lock fragmentation plateau" phenomenon, and co-designs memory
        retention policy with communication topology to avoid isolated optimization.

Interface-compatible with: stigmergy_layer.py, enterprise_memory.py
"""

from __future__ import annotations

import dataclasses
import itertools
import logging
import math
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TopologyType(Enum):
    STAR = auto()
    FULLY_CONNECTED = auto()
    RING = auto()
    RANDOM = auto()
    SCALE_FREE = auto()
    SMALL_WORLD = auto()


class ConsensusPhase(Enum):
    PRE_LOCK = auto()       # rapid convergence before plateau
    PLATEAU = auto()        # "rapid-lock fragmentation plateau"
    POST_PLATEAU = auto()   # slow convergence after plateau
    CONVERGED = auto()      # full consensus reached


class RetentionPolicy(Enum):
    KEEP_ALL = auto()
    SLIDING_WINDOW = auto()
    PRIORITY_BASED = auto()
    FADING = auto()
    TOPOLOGY_AWARE = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class AgentNode:
    """A single agent node in the multi-agent topology."""
    agent_id: str
    memory_depth: int          # number of past states retained
    memory_bank: List[np.ndarray] = field(default_factory=list)
    current_belief: np.ndarray = field(default_factory=lambda: np.zeros(10))
    neighbors: List[str] = field(default_factory=list)
    lock_threshold: float = 0.95


@dataclass
class ConsensusTrace:
    """Record of consensus evolution across rounds."""
    round: int
    mean_opinion: np.ndarray
    variance: float
    phase: ConsensusPhase
    plateau_round: int = -1


@dataclass
class TopologyDesign:
    """Output of the co-designer: a recommended topology + retention combo."""
    topology_type: TopologyType
    retention_policy: RetentionPolicy
    expected_consensus_round: int
    fragmentation_risk: float
    topology_params: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# TopologyMemoryModel
# ---------------------------------------------------------------------------

class TopologyMemoryModel:
    """
    Models the interaction between network topology and agent memory depth,
    measuring how quickly (and at what cost) consensus emerges.
    """

    def __init__(
        self,
        num_agents: int = 20,
        topology: TopologyType = TopologyType.STAR,
        base_memory_depth: int = 5,
        random_seed: int = 42,
    ) -> None:
        self.num_agents = num_agents
        self.topology = topology
        self.base_memory_depth = base_memory_depth
        self.rng = np.random.default_rng(random_seed)
        self.agents: Dict[str, AgentNode] = {}
        self._lock = threading.RLock()
        self._consensus_traces: List[ConsensusTrace] = []
        self._build_topology()

    # ---- builders ----

    def _build_topology(self) -> None:
        with self._lock:
            self.agents.clear()
            for i in range(self.num_agents):
                aid = f"agent_{i:03d}"
                belief = self.rng.uniform(0, 1, 10)
                belief /= belief.sum()
                self.agents[aid] = AgentNode(
                    agent_id=aid,
                    memory_depth=self.base_memory_depth,
                    current_belief=belief,
                )
            self._wire_topology()

    def _wire_topology(self) -> None:
        ids = list(self.agents.keys())
        if self.topology == TopologyType.STAR:
            hub = ids[0]
            for aid in ids[1:]:
                self.agents[hub].neighbors.append(aid)
                self.agents[aid].neighbors.append(hub)
        elif self.topology == TopologyType.FULLY_CONNECTED:
            for a, b in itertools.combinations(ids, 2):
                self.agents[a].neighbors.append(b)
                self.agents[b].neighbors.append(a)
        elif self.topology == TopologyType.RING:
            n = len(ids)
            for i, aid in enumerate(ids):
                self.agents[aid].neighbors.append(ids[(i + 1) % n])
                self.agents[aid].neighbors.append(ids[(i - 1) % n])
        elif self.topology == TopologyType.RANDOM:
            for aid in ids:
                # each agent connected to ~log2(N) random peers
                k = max(1, int(math.log2(self.num_agents)))
                peers = self.rng.choice(ids, size=k, replace=False).tolist()
                if aid in peers:
                    peers.remove(aid)
                self.agents[aid].neighbors = peers

    # ---- simulation ----

    def simulate_consensus(self, rounds: int = 50) -> List[ConsensusTrace]:
        """Run multi-round belief propagation and trace consensus."""
        with self._lock:
            self._consensus_traces.clear()
            plateau_round = -1
            struck_plateau = False

            for r in range(rounds):
                # each agent averages neighbor beliefs weighted by memory
                new_beliefs: Dict[str, np.ndarray] = {}
                for aid, agent in self.agents.items():
                    if not agent.neighbors:
                        new_beliefs[aid] = agent.current_belief.copy()
                        continue
                    neighbor_beliefs = np.array([
                        self.agents[nid].current_belief for nid in agent.neighbors
                    ])
                    # memory depth acts as inertia — older agents average more slowly
                    decay = 1.0 / (1.0 + math.log(1 + agent.memory_depth))
                    avg = neighbor_beliefs.mean(axis=0)
                    new_belief = (1 - decay) * agent.current_belief + decay * avg
                    new_beliefs[aid] = new_belief / new_belief.sum()

                for aid, nb in new_beliefs.items():
                    self.agents[aid].current_belief = nb

                # compute variance
                all_beliefs = np.array([ag.current_belief for ag in self.agents.values()])
                variance = float(np.var(all_beliefs))

                phase = self._classify_phase(variance, r, struck_plateau, plateau_round)
                if phase == ConsensusPhase.PLATEAU and not struck_plateau:
                    struck_plateau = True
                    plateau_round = r

                trace = ConsensusTrace(
                    round=r,
                    mean_opinion=all_beliefs.mean(axis=0),
                    variance=variance,
                    phase=phase,
                    plateau_round=plateau_round,
                )
                self._consensus_traces.append(trace)

                if phase == ConsensusPhase.CONVERGED:
                    break

            return self._consensus_traces

    def _classify_phase(
        self, variance: float, current_round: int,
        struck_plateau: bool, plateau_round: int,
    ) -> ConsensusPhase:
        if variance < 1e-6:
            return ConsensusPhase.CONVERGED
        if struck_plateau:
            return ConsensusPhase.POST_PLATEAU
        # detect plateau: variance change < 1% over multiple rounds
        if (
            len(self._consensus_traces) >= 3
            and variance > 1e-4
            and self._consensus_traces[-1].variance > 0
        ):
            prev_var = self._consensus_traces[-1].variance
            rel_change = abs(variance - prev_var) / prev_var
            if rel_change < 0.01:
                return ConsensusPhase.PLATEAU
        return ConsensusPhase.PRE_LOCK

    def statistics(self) -> Dict[str, Any]:
        """Return runtime statistics."""
        var = float(np.var([a.current_belief for a in self.agents.values()]))
        plateau = any(
            t.phase == ConsensusPhase.PLATEAU for t in self._consensus_traces
        )
        return {
            "num_agents": self.num_agents,
            "topology": self.topology.name,
            "consensus_rounds": len(self._consensus_traces),
            "final_variance": var,
            "plateau_detected": plateau,
        }


# ---------------------------------------------------------------------------
# SpeedUnityTradeoff
# ---------------------------------------------------------------------------

class SpeedUnityTradeoff:
    """
    Analyses the speed-unity tradeoff in multi-agent consensus,
    specifically detecting the "rapid-lock fragmentation plateau"
    phenomenon where early rapid convergence stalls into a
    meta-stable fragmented state.
    """

    def __init__(self, convergence_threshold: float = 0.01) -> None:
        self.threshold = convergence_threshold
        self._tradeoff_points: List[Dict[str, float]] = []
        self._lock = threading.RLock()

    def analyse(self, traces: List[ConsensusTrace]) -> Dict[str, Any]:
        """Analyse a consensus trace for the speed-unity tradeoff."""
        with self._lock:
            if not traces:
                return {"error": "empty trace"}

            pre_lock_speed = self._compute_speed(traces, ConsensusPhase.PRE_LOCK)
            plateau_entry = self._find_phase_start(traces, ConsensusPhase.PLATEAU)
            post_plateau_speed = self._compute_speed(traces, ConsensusPhase.POST_PLATEAU)

            fragmentation_index = self._compute_fragmentation(traces)

            point = {
                "pre_lock_speed": pre_lock_speed,
                "plateau_entry_round": plateau_entry,
                "post_plateau_speed": post_plateau_speed,
                "fragmentation_index": fragmentation_index,
                "speed_lost_ratio": (
                    1 - (post_plateau_speed / pre_lock_speed)
                    if pre_lock_speed > 0 else 0.0
                ),
            }
            self._tradeoff_points.append(point)
            return point

    def _compute_speed(self, traces: List[ConsensusTrace], phase: ConsensusPhase) -> float:
        phase_traces = [t for t in traces if t.phase == phase]
        if len(phase_traces) < 2:
            return 0.0
        start_var = phase_traces[0].variance
        end_var = phase_traces[-1].variance
        if start_var == 0:
            return 0.0
        return (start_var - end_var) / (start_var * max(1, len(phase_traces)))

    def _find_phase_start(self, traces: List[ConsensusTrace], phase: ConsensusPhase) -> int:
        for t in traces:
            if t.phase == phase:
                return t.round
        return -1

    def _compute_fragmentation(self, traces: List[ConsensusTrace]) -> float:
        """Measure how fragmented opinions are at the end."""
        if not traces:
            return 0.0
        final = traces[-1]
        # fragmentation is high when variance is stuck > threshold
        if final.variance <= self.threshold:
            return 0.0
        return min(1.0, final.variance / 0.1)  # normalize

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "tradeoff_points_collected": len(self._tradeoff_points),
                "latest": self._tradeoff_points[-1] if self._tradeoff_points else None,
            }


# ---------------------------------------------------------------------------
# FictitiousPlayAdapter
# ---------------------------------------------------------------------------

class FictitiousPlayAdapter:
    """
    Belief-based adaptation mechanism using fictitious play dynamics.
    Unlike reward-based RL, agents update their strategies by observing
    empirical frequency of neighbor actions — a lightweight coordination
    mechanism that naturally aligns with memory depth constraints.
    """

    def __init__(
        self,
        num_agents: int = 20,
        strategy_dim: int = 4,
        learning_rate: float = 0.05,
    ) -> None:
        self.num_agents = num_agents
        self.strategy_dim = strategy_dim
        self.lr = learning_rate
        self._empirical_frequencies: Dict[str, np.ndarray] = {}
        self._strategies: Dict[str, np.ndarray] = {}
        self._lock = threading.RLock()
        self._reset()

    def _reset(self) -> None:
        rng = np.random.default_rng(0)
        for i in range(self.num_agents):
            aid = f"agent_{i:03d}"
            self._empirical_frequencies[aid] = np.zeros(self.strategy_dim)
            strat = rng.uniform(0, 1, self.strategy_dim)
            self._strategies[aid] = strat / strat.sum()

    def observe(self, agent_id: str, neighbor_strategies: List[np.ndarray]) -> None:
        """Update empirical frequency based on observed neighbor strategies."""
        with self._lock:
            if agent_id not in self._empirical_frequencies:
                return
            if not neighbor_strategies:
                return
            avg = np.mean(neighbor_strategies, axis=0)
            freq = self._empirical_frequencies[agent_id]
            self._empirical_frequencies[agent_id] = (1 - self.lr) * freq + self.lr * avg

    def best_response(self, agent_id: str) -> np.ndarray:
        """Compute best-response strategy given observed empirical frequencies."""
        with self._lock:
            freq = self._empirical_frequencies.get(agent_id)
            if freq is None:
                return np.ones(self.strategy_dim) / self.strategy_dim
            # softmax best-response: temperature-scaled
            temp = 0.5
            probs = np.exp(freq / temp)
            return probs / probs.sum()

    def update_strategies(self) -> Dict[str, np.ndarray]:
        """Apply fictitious play update to all agents."""
        with self._lock:
            new_strategies = {}
            for aid in self._strategies:
                br = self.best_response(aid)
                old = self._strategies[aid]
                new_strategies[aid] = (1 - self.lr) * old + self.lr * br
            self._strategies = new_strategies
            return dict(self._strategies)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            strats = np.array(list(self._strategies.values()))
            return {
                "num_agents": self.num_agents,
                "strategy_dim": self.strategy_dim,
                "mean_strategy_entropy": float(
                    -np.mean(np.sum(strats * np.log(strats + 1e-12), axis=1))
                ),
            }


# ---------------------------------------------------------------------------
# MemoryTopologyCoDesigner
# ---------------------------------------------------------------------------

class MemoryTopologyCoDesigner:
    """
    Co-designs memory retention policy alongside communication topology
    to avoid the pitfall of isolated optimization. Evaluates joint
    (topology, retention) configurations and recommends the Pareto-optimal pair.
    """

    def __init__(self, candidate_topologies: Optional[List[TopologyType]] = None) -> None:
        self.candidate_topologies = candidate_topologies or [
            TopologyType.STAR,
            TopologyType.FULLY_CONNECTED,
            TopologyType.RING,
            TopologyType.RANDOM,
        ]
        self.candidate_policies = [
            RetentionPolicy.KEEP_ALL,
            RetentionPolicy.SLIDING_WINDOW,
            RetentionPolicy.PRIORITY_BASED,
            RetentionPolicy.FADING,
            RetentionPolicy.TOPOLOGY_AWARE,
        ]
        self._designs: List[TopologyDesign] = []
        self._lock = threading.RLock()

    def evaluate_pair(
        self,
        topology_type: TopologyType,
        retention_policy: RetentionPolicy,
        num_agents: int = 20,
        rounds: int = 30,
    ) -> TopologyDesign:
        """Evaluate a single (topology, retention) pair via simulation."""
        memory_depth_map = {
            RetentionPolicy.KEEP_ALL: 20,
            RetentionPolicy.SLIDING_WINDOW: 8,
            RetentionPolicy.PRIORITY_BASED: 12,
            RetentionPolicy.FADING: 6,
            RetentionPolicy.TOPOLOGY_AWARE: 10,
        }
        depth = memory_depth_map.get(retention_policy, 5)

        model = TopologyMemoryModel(
            num_agents=num_agents,
            topology=topology_type,
            base_memory_depth=depth,
        )
        traces = model.simulate_consensus(rounds=rounds)

        analyst = SpeedUnityTradeoff()
        analysis = analyst.analyse(traces)

        design = TopologyDesign(
            topology_type=topology_type,
            retention_policy=retention_policy,
            expected_consensus_round=len(traces),
            fragmentation_risk=analysis.get("fragmentation_index", 0.5),
        )
        return design

    def sweep(self, num_agents: int = 20) -> List[TopologyDesign]:
        """Sweep all (topology, retention) pairs and collect designs."""
        with self._lock:
            self._designs.clear()
            for topo in self.candidate_topologies:
                for pol in self.candidate_policies:
                    design = self.evaluate_pair(topo, pol, num_agents=num_agents)
                    self._designs.append(design)
            return self._designs

    def pareto_frontier(self) -> List[TopologyDesign]:
        """Return Pareto-optimal designs (min rounds, min fragmentation)."""
        if not self._designs:
            self.sweep()
        designs = list(self._designs)
        pareto = []
        for d in designs:
            dominated = False
            for other in designs:
                if other is d:
                    continue
                if (
                    other.expected_consensus_round <= d.expected_consensus_round
                    and other.fragmentation_risk <= d.fragmentation_risk
                    and (
                        other.expected_consensus_round < d.expected_consensus_round
                        or other.fragmentation_risk < d.fragmentation_risk
                    )
                ):
                    dominated = True
                    break
            if not dominated:
                pareto.append(d)
        return pareto

    def recommend(self) -> TopologyDesign:
        """Recommend the best joint design using a composite score."""
        frontier = self.pareto_frontier()
        if not frontier:
            # fallback
            return TopologyDesign(
                topology_type=TopologyType.STAR,
                retention_policy=RetentionPolicy.TOPOLOGY_AWARE,
                expected_consensus_round=999,
                fragmentation_risk=1.0,
            )
        # composite: minimize both, equal weight
        scores = []
        for d in frontier:
            max_r = max(1, max(f.expected_consensus_round for f in frontier))
            max_f = max(0.001, max(f.fragmentation_risk for f in frontier))
            score = 0.5 * (d.expected_consensus_round / max_r) + 0.5 * (d.fragmentation_risk / max_f)
            scores.append(score)
        return frontier[int(np.argmin(scores))]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            frontier = self.pareto_frontier()
            return {
                "total_pairs_evaluated": len(self._designs),
                "pareto_frontier_size": len(frontier),
                "recommended": dataclasses.asdict(self.recommend()) if self._designs else None,
            }
