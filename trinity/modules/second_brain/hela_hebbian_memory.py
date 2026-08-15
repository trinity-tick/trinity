"""P24: HeLa-Mem Hebbian Memory — 2026.

# status: orphan (2026-08-15 audit, not in runtime path)
Hebbian co-activation dynamics: edges strengthen when nodes fire together,
decay with inactivity. Includes hub detection and reflective distillation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class HebbianEdge:
    """Hebbian edge tracking co-activation between node pairs.

    Hebbian principle: "neurons that fire together, wire together."
    Edge weight grows with repeated co-activation, decays with inactivity.
    """
    source: str
    target: str
    co_activation_count: int = 0
    weight: float = 0.0
    last_activated: float = 0.0

    def strengthen(self, increment: float = 0.1) -> None:
        self.co_activation_count += 1
        self.weight = min(1.0, self.weight + increment)
        self.last_activated = time.time()

    def decay(self, current_time: float, half_life: float = 86400.0) -> None:
        if self.last_activated == 0:
            return
        elapsed = current_time - self.last_activated
        self.weight *= 0.5 ** (elapsed / half_life)


class HebbianDynamicsEngine:
    """Core Hebbian dynamics: update edges from activated node sets.

    For each pair of co-activated nodes, strengthens the edge.
    For edges not in the activated set, applies time-based decay.
    """

    DECAY_HALF_LIFE: float = 86400.0  # 24 hours

    def __init__(self):
        self._edges: dict[tuple[str, str], HebbianEdge] = {}
        self._nodes: set[str] = set()

    @property
    def edges(self) -> dict[tuple[str, str], HebbianEdge]:
        return self._edges

    @property
    def nodes(self) -> set[str]:
        return self._nodes

    def update_edges(self, activated_nodes: list[str]) -> dict[tuple[str, str], float]:
        """Update Hebbian edges from co-activated node set.

        Returns dict of (src,tgt) → new_weight for updated edges.
        """
        now = time.time()
        self._nodes.update(activated_nodes)
        updated: dict[tuple[str, str], float] = {}
        activated_pairs: set[tuple[str, str]] = set()

        for i, src in enumerate(activated_nodes):
            for tgt in activated_nodes[i + 1:]:
                key = (src, tgt)
                activated_pairs.add(key)
                if key not in self._edges:
                    self._edges[key] = HebbianEdge(source=src, target=tgt)
                self._edges[key].strengthen()
                updated[key] = self._edges[key].weight

        for key, edge in self._edges.items():
            if key not in activated_pairs:
                old = edge.weight
                edge.decay(now, self.DECAY_HALF_LIFE)
                if edge.weight != old:
                    updated[key] = edge.weight

        logger.debug("HeLa: %d edges strengthened, total=%d", len(activated_pairs), len(self._edges))
        return updated

    def get_edge(self, source: str, target: str) -> HebbianEdge | None:
        return self._edges.get((source, target))

    def get_node_degree(self, node_id: str) -> int:
        return sum(1 for (s, t) in self._edges if s == node_id or t == node_id)


class HubNodeDetector:
    """Detect hub nodes by degree percentile (top 5%)."""

    def __init__(self, engine: HebbianDynamicsEngine, percentile: float = 95.0):
        self._engine = engine
        self.percentile = percentile

    def detect_hubs(self) -> list[tuple[str, int, float]]:
        """Identify hub nodes exceeding degree threshold.

        Returns list of (node_id, degree, avg_edge_weight) sorted by degree desc.
        """
        degrees: list[tuple[str, int, float]] = []
        for node_id in self._engine.nodes:
            deg = self._engine.get_node_degree(node_id)
            if deg == 0:
                continue
            total_w = 0.0
            ec = 0
            for (s, t), edge in self._engine.edges.items():
                if s == node_id or t == node_id:
                    total_w += edge.weight
                    ec += 1
            avg_w = total_w / max(ec, 1)
            degrees.append((node_id, deg, avg_w))

        if not degrees:
            return []

        degrees.sort(key=lambda x: x[1], reverse=True)
        cutoff = max(1, int(len(degrees) * (1 - self.percentile / 100)))
        hubs = degrees[:cutoff]
        logger.info("HeLa hubs: %d from %d nodes", len(hubs), len(degrees))
        return hubs


class ReflectiveDistiller:
    """Generate reflective summaries from Hebbian edge dynamics.

    Distills session-level edge activation patterns into structured summaries
    identifying strong associations, emerging clusters, and fading connections.
    """

    def __init__(self, engine: HebbianDynamicsEngine):
        self._engine = engine

    def distill(self, session_edges: list[tuple[str, str]]) -> list[dict[str, Any]]:
        now = time.time()
        summaries: list[dict[str, Any]] = []
        strong: list[dict[str, Any]] = []
        emerging: list[dict[str, Any]] = []
        fading: list[dict[str, Any]] = []

        for (src, tgt), edge in self._engine.edges.items():
            info = {"source": src, "target": tgt, "weight": edge.weight,
                    "co_activations": edge.co_activation_count,
                    "last_activated": edge.last_activated}
            if edge.weight > 0.7:
                strong.append(info)
            if edge.co_activation_count <= 3:
                emerging.append(info)
            if edge.weight < 0.2 and (now - edge.last_activated) > 86400:
                fading.append(info)

        if strong:
            summaries.append({"type": "strong_associations", "count": len(strong),
                              "top": sorted(strong, key=lambda x: x["weight"], reverse=True)[:5]})
        if emerging:
            summaries.append({"type": "emerging_patterns", "count": len(emerging),
                              "samples": emerging[:5]})
        if fading:
            summaries.append({"type": "fading_connections", "count": len(fading),
                              "suggestion": "Consider reinforcing or pruning"})
        return summaries


def forward(
    engine: HebbianDynamicsEngine,
    query_nodes: list[str],
    max_depth: int = 3,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Diffusion-based retrieval from Hebbian graph.

    Starting from query_nodes, follows strong Hebbian edges outward,
    collecting associated nodes up to max_depth hops.
    """
    results: dict[str, dict[str, Any]] = {}
    visited: set[str] = set(query_nodes)
    frontier: list[tuple[str, int, float]] = [(n, 0, 1.0) for n in query_nodes]

    for _ in range(max_depth):
        next_frontier: list[tuple[str, int, float]] = []
        for node_id, depth, incoming_weight in frontier:
            for (src, tgt), edge in engine.edges.items():
                neighbor = None
                if src == node_id and tgt not in visited:
                    neighbor = tgt
                elif tgt == node_id and src not in visited:
                    neighbor = src
                if neighbor is None:
                    continue
                cw = incoming_weight * edge.weight
                if cw < 0.1:
                    continue
                visited.add(neighbor)
                results[neighbor] = {
                    "node_id": neighbor, "depth": depth + 1,
                    "association_weight": cw,
                    "co_activations": edge.co_activation_count,
                    "connected_via": node_id,
                }
                next_frontier.append((neighbor, depth + 1, cw))
        frontier = next_frontier
        if not frontier:
            break

    return sorted(results.values(), key=lambda x: x["association_weight"], reverse=True)[:top_k]


print("[P24] HeLa-Mem HebbianMemory initialized — 2026 aligned")
