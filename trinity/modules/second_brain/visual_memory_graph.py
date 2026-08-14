"""
P25-4: Visual Memory Graph (VimRAG)
arXiv:2602.12735

Multi-modal visual memory with dynamic DAG, graph-modulated vision encoding,
and topological token allocation for vision-language memory retrieval.
"""

import json
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple


class EdgeType(Enum):
    SPATIAL = "spatial"
    TEMPORAL = "temporal"
    SEMANTIC = "semantic"
    CAUSAL = "causal"
    COMPOSITIONAL = "compositional"


class NodeType(Enum):
    VISUAL_FRAGMENT = "visual_fragment"
    TEXT_ANCHOR = "text_anchor"
    CONTEXT_WINDOW = "context_window"
    CONCEPT_NODE = "concept_node"
    ACTION_NODE = "action_node"


@dataclass
class VisualToken:
    token_id: str
    embedding: List[float]
    weight: float = 1.0
    allocated_budget: int = 0
    position: Tuple[int, int] = (0, 0)

    def topo_score(self) -> float:
        return self.weight * (1 + self.allocated_budget * 0.1)


@dataclass
class GraphEdge:
    edge_id: str
    source_id: str
    target_id: str
    edge_type: EdgeType
    weight: float = 1.0
    attributes: Dict[str, Any] = field(default_factory=dict)
    active: bool = True
    created_at: float = field(default_factory=time.time)

    def effective_weight(self) -> float:
        return self.weight if self.active else 0.0


@dataclass
class GraphNode:
    node_id: str
    node_type: NodeType
    visual_embedding: Optional[List[float]] = None
    text_embedding: Optional[List[float]] = None
    raw_summary: str = ""
    tokens: List[VisualToken] = field(default_factory=list)
    attributes: Dict[str, Any] = field(default_factory=dict)
    importance: float = 0.5
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0

    def multi_modal_embedding(self) -> Optional[List[float]]:
        v = self.visual_embedding or []
        t = self.text_embedding or []
        if v and t:
            dim = min(len(v), len(t))
            return [(v[i] + t[i]) / 2 for i in range(dim)]
        return v or t

    def token_count(self) -> int:
        return len(self.tokens)


class DynamicDAG:
    """Dynamic directed acyclic graph with cycle detection."""
    def __init__(self, max_nodes: int = 10000):
        self.nodes: Dict[str, GraphNode] = {}
        self.edges: Dict[str, GraphEdge] = {}
        self.adj_out: Dict[str, List[str]] = defaultdict(list)
        self.adj_in: Dict[str, List[str]] = defaultdict(list)
        self.max_nodes = max_nodes
        self.edge_counter = 0

    def add_node(self, node: GraphNode) -> bool:
        if len(self.nodes) >= self.max_nodes:
            self._evict_lru()
        self.nodes[node.node_id] = node
        return True

    def add_edge(self, source: str, target: str, edge_type: EdgeType,
                 weight: float = 1.0, **attrs) -> Optional[str]:
        if source not in self.nodes or target not in self.nodes:
            return None
        if self._would_create_cycle(source, target):
            return None
        self.edge_counter += 1
        eid = f"e_{self.edge_counter}_{source[:4]}_{target[:4]}"
        edge = GraphEdge(eid, source, target, edge_type, weight,
                         attributes=attrs)
        self.edges[eid] = edge
        self.adj_out[source].append(eid)
        self.adj_in[target].append(eid)
        return eid

    def topological_sort(self) -> List[str]:
        in_degree = {nid: len(self.adj_in.get(nid, [])) for nid in self.nodes}
        q = deque(nid for nid, deg in in_degree.items() if deg == 0)
        result = []
        while q:
            n = q.popleft()
            result.append(n)
            for eid in self.adj_out.get(n, []):
                edge = self.edges[eid]
                in_degree[edge.target_id] -= 1
                if in_degree[edge.target_id] == 0:
                    q.append(edge.target_id)
        return result

    def get_neighbors(self, node_id: str,
                      edge_types: Optional[List[EdgeType]] = None) -> List[str]:
        neighbors = []
        for eid in self.adj_out.get(node_id, []):
            e = self.edges[eid]
            if edge_types is None or e.edge_type in edge_types:
                neighbors.append(e.target_id)
        return neighbors

    def prune_inactive(self, min_importance: float = 0.1):
        to_remove = [nid for nid, n in self.nodes.items()
                     if n.importance < min_importance and n.access_count < 2]
        for nid in to_remove:
            for eid in self.adj_out.get(nid, []) + self.adj_in.get(nid, []):
                if eid in self.edges:
                    del self.edges[eid]
            self.adj_out.pop(nid, None)
            self.adj_in.pop(nid, None)
            del self.nodes[nid]

    def stats(self) -> Dict[str, Any]:
        return {
            "nodes": len(self.nodes),
            "edges": len(self.edges),
            "max_in_degree": max(
                (len(v) for v in self.adj_in.values()), default=0),
            "max_out_degree": max(
                (len(v) for v in self.adj_out.values()), default=0),
        }

    def _would_create_cycle(self, source: str, target: str) -> bool:
        visited = set()
        q = deque([target])
        while q:
            n = q.popleft()
            if n == source:
                return True
            if n in visited:
                continue
            visited.add(n)
            for eid in self.adj_out.get(n, []):
                q.append(self.edges[eid].target_id)
        return False

    def _evict_lru(self):
        oldest = min(self.nodes.values(),
                     key=lambda n: n.last_accessed)
        self.prune_inactive(1.0)
        if len(self.nodes) > self.max_nodes * 0.9:
            sorted_nodes = sorted(self.nodes.items(),
                                  key=lambda x: (x[1].importance, x[1].last_accessed))
            for nid, _ in sorted_nodes[:int(len(self.nodes)*0.1)]:
                if nid in self.nodes:
                    del self.nodes[nid]


class GraphModulatedVisionEncoder:
    """Vision encoding modulated by graph topology."""
    def __init__(self, embedding_dim: int = 768):
        self.embedding_dim = embedding_dim
        self.modulation_strength = 0.3

    def encode(self, visual_features: List[float],
               neighbors: List[GraphNode]) -> List[float]:
        dim = min(self.embedding_dim, len(visual_features))
        result = list(visual_features[:dim])
        if neighbors:
            neighbor_avg = [0.0] * dim
            n_count = 0
            for nb in neighbors:
                if nb.visual_embedding:
                    for i in range(min(dim, len(nb.visual_embedding))):
                        neighbor_avg[i] += nb.visual_embedding[i]
                    n_count += 1
            if n_count > 0:
                for i in range(dim):
                    neighbor_avg[i] /= n_count
                    result[i] = (1 - self.modulation_strength) * result[i] +                                 self.modulation_strength * neighbor_avg[i]
        return result

    def modulate(self, embedding: List[float],
                 graph_importance: float) -> List[float]:
        return [v * (1.0 + graph_importance * 0.1) for v in embedding]


class TopologicalTokenAllocator:
    """Token budget allocation based on topological importance."""
    def __init__(self, total_budget: int = 2048):
        self.total_budget = total_budget
        self.allocated: Dict[str, int] = {}

    def allocate(self, nodes: List[GraphNode],
                 dag: DynamicDAG) -> Dict[str, int]:
        if not nodes:
            return {}
        order = dag.topological_sort()
        ordered_nodes = [dag.nodes[nid] for nid in order if nid in dag.nodes]
        if not ordered_nodes:
            ordered_nodes = nodes
        scores = {}
        for n in ordered_nodes:
            n.access_count += 1
            n.last_accessed = time.time()
            scores[n.node_id] = n.importance * (n.token_count() + 1) *                                 (1 + 0.1 * len(dag.adj_in.get(n.node_id, [])))
        total_score = sum(scores.values()) or 1
        remaining = self.total_budget
        self.allocated.clear()
        for n in ordered_nodes:
            share = max(1, int(self.total_budget * scores[n.node_id] / total_score))
            self.allocated[n.node_id] = min(share, remaining)
            remaining -= self.allocated[n.node_id]
            if remaining <= 0:
                break
        return dict(self.allocated)

    def stats(self) -> Dict[str, Any]:
        return {
            "total_budget": self.total_budget,
            "allocated": sum(self.allocated.values()),
            "nodes_allocated": len(self.allocated),
        }


class MultiModalRetriever:
    """Cross-modal retrieval over visual memory graph."""
    def __init__(self, dag: DynamicDAG, allocator: TopologicalTokenAllocator):
        self.dag = dag
        self.allocator = allocator

    def retrieve_by_query(self, query_embedding: List[float],
                          top_k: int = 5, modality: str = "visual") -> List[GraphNode]:
        candidates = []
        for n in self.dag.nodes.values():
            emb = n.visual_embedding if modality == "visual" else n.text_embedding
            if emb:
                sim = self._cosine_sim(query_embedding, emb)
                score = sim * n.importance * (1 + n.access_count * 0.01)
                candidates.append((score, n))
        candidates.sort(key=lambda x: x[0], reverse=True)
        return [n for _, n in candidates[:top_k]]

    def retrieve_neighborhood(self, node_id: str,
                              depth: int = 2) -> List[GraphNode]:
        visited = set([node_id])
        frontier = [node_id]
        result = []
        for _ in range(depth):
            next_frontier = []
            for nid in frontier:
                if nid in self.dag.nodes:
                    result.append(self.dag.nodes[nid])
                for eid in self.dag.adj_out.get(nid, []):
                    tid = self.dag.edges[eid].target_id
                    if tid not in visited:
                        visited.add(tid)
                        next_frontier.append(tid)
            frontier = next_frontier
        return result

    def retrieve_temporal(self, start_time: float, end_time: float) -> List[GraphNode]:
        return [n for n in self.dag.nodes.values()
                if start_time <= n.attributes.get("timestamp", 0) <= end_time]

    def _cosine_sim(self, a: List[float], b: List[float]) -> float:
        dim = min(len(a), len(b))
        if dim == 0:
            return 0.0
        dot = sum(a[i] * b[i] for i in range(dim))
        na = sum(x*x for x in a[:dim]) ** 0.5
        nb = sum(x*x for x in b[:dim]) ** 0.5
        return dot / max(1e-8, na * nb)


class VisualMemoryGraph:
    """Multi-modal visual memory graph system."""
    def __init__(self, embedding_dim: int = 768, token_budget: int = 2048):
        self.dag = DynamicDAG()
        self.encoder = GraphModulatedVisionEncoder(embedding_dim)
        self.allocator = TopologicalTokenAllocator(token_budget)
        self.retriever = MultiModalRetriever(self.dag, self.allocator)
        self.query_history: List[Dict[str, Any]] = []

    def add_visual_node(self, node_id: str, visual_embedding: List[float],
                        summary: str = "", importance: float = 0.5,
                        **attrs) -> GraphNode:
        node = GraphNode(
            node_id=node_id, node_type=NodeType.VISUAL_FRAGMENT,
            visual_embedding=visual_embedding, raw_summary=summary,
            importance=importance, attributes=attrs)
        node.tokens = [VisualToken(
            token_id=f"vt_{node_id}_{i}",
            embedding=visual_embedding[i:i+64] if i+64<=len(visual_embedding)
            else visual_embedding[i:],
            weight=importance)
            for i in range(0, min(len(visual_embedding), 512), 64)]
        self.dag.add_node(node)
        return node

    def add_text_node(self, node_id: str, text_embedding: List[float],
                      text: str = "", importance: float = 0.5,
                      **attrs) -> GraphNode:
        node = GraphNode(
            node_id=node_id, node_type=NodeType.TEXT_ANCHOR,
            text_embedding=text_embedding, raw_summary=text,
            importance=importance, attributes=attrs)
        self.dag.add_node(node)
        return node

    def link(self, source: str, target: str, edge_type: EdgeType,
             weight: float = 1.0, **attrs) -> Optional[str]:
        return self.dag.add_edge(source, target, edge_type, weight, **attrs)

    def encode_with_context(self, node_id: str,
                            visual_features: List[float]) -> List[float]:
        neighbors = [self.dag.nodes[n] for n in self.dag.get_neighbors(node_id)]
        return self.encoder.encode(visual_features, neighbors)

    def allocate_tokens(self) -> Dict[str, int]:
        return self.allocator.allocate(
            list(self.dag.nodes.values()), self.dag)

    def query(self, query_embedding: List[float], top_k: int = 5,
              modality: str = "visual") -> List[GraphNode]:
        results = self.retriever.retrieve_by_query(query_embedding, top_k, modality)
        self.query_history.append({
            "timestamp": time.time(),
            "top_k": top_k,
            "modality": modality,
            "result_count": len(results),
        })
        return results

    def query_neighborhood(self, node_id: str, depth: int = 2) -> List[GraphNode]:
        return self.retriever.retrieve_neighborhood(node_id, depth)

    def prune(self):
        self.dag.prune_inactive()

    def stats(self) -> Dict[str, Any]:
        return {
            "dag": self.dag.stats(),
            "token_allocator": self.allocator.stats(),
            "total_queries": len(self.query_history),
        }
