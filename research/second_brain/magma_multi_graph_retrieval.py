"""P24: MAGMA Multi-Graph Retrieval — 2026.07.

# status: orphan (2026-08-15 audit, not in runtime path)
Multi-graph routing across semantic / temporal / causal / entity sub-graphs
with intent-aware strategy selection and adaptive topological traversal.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RetrievalResult:
    """Single retrieval hit across one sub-graph."""
    node_id: str
    content: dict[str, Any]
    graph_type: str
    confidence: float
    hop_distance: int
    traversal_path: list[str] = field(default_factory=list)


class MultiGraphRouter:
    """Manages four sub-graph indices for cross-graph retrieval.

    Sub-graph types:
        - semantic: embedding-based similarity graph
        - temporal: time-ordered event sequences
        - causal: cause-effect relationship chains
        - entity: typed entity-relation knowledge graph
    """

    GRAPH_TYPES = ("semantic", "temporal", "causal", "entity")

    def __init__(self):
        self._lock = threading.RLock()
        self._graphs: dict[str, dict[str, dict[str, Any]]] = {
            gt: {} for gt in self.GRAPH_TYPES
        }
        self._edges: dict[str, dict[tuple[str, str], dict[str, Any]]] = {
            gt: {} for gt in self.GRAPH_TYPES
        }

    def add_node(self, graph_type: str, node_id: str, content: dict[str, Any]) -> None:
        if graph_type not in self.GRAPH_TYPES:
            raise ValueError(f"Unknown graph type: {graph_type}")
        self._graphs[graph_type][node_id] = content

    def add_edge(
        self, graph_type: str, source: str, target: str,
        weight: float = 1.0, metadata: dict[str, Any] | None = None,
    ) -> None:
        if graph_type not in self.GRAPH_TYPES:
            raise ValueError(f"Unknown graph type: {graph_type}")
        self._edges[graph_type][(source, target)] = {
            "weight": weight, "metadata": metadata or {},
        }

    def get_neighbors(self, graph_type: str, node_id: str) -> list[tuple[str, float]]:
        neighbors: list[tuple[str, float]] = []
        for (src, tgt), edge in self._edges.get(graph_type, {}).items():
            if src == node_id:
                neighbors.append((tgt, edge["weight"]))
        return neighbors

    def search_nodes(
        self, graph_type: str,
        query_embedding: list[float] | None = None,
        keyword: str = "",
    ) -> list[tuple[str, float]]:
        results: list[tuple[str, float]] = []
        kw = keyword.lower()
        for node_id, content in self._graphs.get(graph_type, {}).items():
            score = 0.0
            if kw and kw in str(content).lower():
                score += 0.5
            if query_embedding and content.get("embedding"):
                emb = content["embedding"]
                dot = sum(a * b for a, b in zip(query_embedding, emb))
                na = math.sqrt(sum(a * a for a in query_embedding))
                nb = math.sqrt(sum(b * b for b in emb))
                if na > 0 and nb > 0:
                    score += dot / (na * nb)
            if score > 0:
                results.append((node_id, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    @property
    def graph_sizes(self) -> dict[str, int]:
        return {gt: len(self._graphs[gt]) for gt in self.GRAPH_TYPES}


class IntentAwareRouter:
    """Route query to optimal sub-graph(s) based on intent classification.

    Strategies:
        - single: one sub-graph (intent clearly maps)
        - multi_joint: multi-graph parallel search with fusion
    """

    INTENT_MAP: dict[str, list[str]] = {
        "fact_lookup": ["entity", "semantic"],
        "timeline_query": ["temporal"],
        "causal_reasoning": ["causal", "temporal"],
        "relationship_query": ["entity", "semantic", "causal"],
        "general": ["semantic", "entity", "temporal", "causal"],
    }

    def __init__(self, router: MultiGraphRouter):
        self._router = router

    def classify_intent(self, query: str) -> str:
        ql = query.lower()
        if any(kw in ql for kw in ["before", "after", "when", "timeline", "sequence"]):
            return "timeline_query"
        if any(kw in ql for kw in ["why", "cause", "effect", "because", "leads to"]):
            return "causal_reasoning"
        if any(kw in ql for kw in ["related", "connected", "relationship", "link"]):
            return "relationship_query"
        if any(kw in ql for kw in ["what", "who", "where", "define", "which"]):
            return "fact_lookup"
        return "general"

    def select_strategy(self, query: str) -> tuple[str, list[str]]:
        intent = self.classify_intent(query)
        targets = self.INTENT_MAP.get(intent, ["semantic", "entity"])
        strategy = "single" if len(targets) == 1 else "multi_joint"
        logger.debug("MAGMA route: intent=%s strategy=%s targets=%s", intent, strategy, targets)
        return strategy, targets


class AdaptiveTopologicalTraversal:
    """Adaptive hop expansion with confidence-gated traversal.

    Expands from seed nodes outward, stopping when:
        - max_hops reached
        - cumulative confidence drops below threshold
        - no new nodes discovered
    """

    def __init__(
        self, router: MultiGraphRouter,
        confidence_threshold: float = 0.3, max_hops: int = 3,
    ):
        self._router = router
        self.confidence_threshold = confidence_threshold
        self.max_hops = max_hops

    def traverse(
        self, graph_type: str, seed_nodes: list[tuple[str, float]],
    ) -> list[RetrievalResult]:
        results: dict[str, RetrievalResult] = {}
        visited: set[str] = set()
        frontier: list[tuple[str, float, int, list[str]]] = [
            (nid, conf, 0, [nid]) for nid, conf in seed_nodes
        ]

        for hop in range(self.max_hops + 1):
            next_frontier: list[tuple[str, float, int, list[str]]] = []
            for node_id, confidence, h, path in frontier:
                if node_id in visited:
                    continue
                if confidence < self.confidence_threshold:
                    continue
                if h != hop:
                    next_frontier.append((node_id, confidence, h, path))
                    continue
                visited.add(node_id)
                content = self._router._graphs.get(graph_type, {}).get(node_id, {})
                results[node_id] = RetrievalResult(
                    node_id=node_id, content=content, graph_type=graph_type,
                    confidence=confidence, hop_distance=h, traversal_path=path,
                )
                decay = 0.7 ** (h + 1)
                for neighbor, weight in self._router.get_neighbors(graph_type, node_id):
                    if neighbor not in visited:
                        next_frontier.append((
                            neighbor, confidence * weight * decay,
                            h + 1, path + [neighbor],
                        ))
            frontier = next_frontier
            if not frontier:
                break

        logger.debug("MAGMA traversal: %d nodes in %s", len(results), graph_type)
        return sorted(results.values(), key=lambda r: r.confidence, reverse=True)


def route(
    query: str,
    router: MultiGraphRouter,
    top_k: int = 10,
    max_hops: int = 3,
    query_embedding: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Main MAGMA retrieval entry point — cross-graph routing.

    Args:
        query: natural language query
        router: MultiGraphRouter with populated indices
        top_k: max results to return
        max_hops: max traversal depth
        query_embedding: optional embedding for semantic scoring

    Returns:
        list of retrieval results as dicts
    """
    intent_router = IntentAwareRouter(router)
    _strategy, targets = intent_router.select_strategy(query)

    traverser = AdaptiveTopologicalTraversal(
        router, confidence_threshold=0.2, max_hops=max_hops,
    )

    all_results: list[RetrievalResult] = []
    for graph_type in targets:
        seeds = router.search_nodes(graph_type, query_embedding=query_embedding, keyword=query)
        if not seeds:
            continue
        all_results.extend(traverser.traverse(graph_type, seeds[:5]))

    seen: set[str] = set()
    unique: list[RetrievalResult] = []
    for r in sorted(all_results, key=lambda x: x.confidence, reverse=True):
        if r.node_id not in seen:
            seen.add(r.node_id)
            unique.append(r)

    return [
        {"node_id": r.node_id, "content": r.content, "graph_type": r.graph_type,
         "confidence": r.confidence, "hop_distance": r.hop_distance,
         "traversal_path": r.traversal_path}
        for r in unique[:top_k]
    ]


print("[P24] MAGMA MultiGraphRetrieval initialized — 2026.07 aligned")
