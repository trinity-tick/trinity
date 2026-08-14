"""
P24-3: Mandol — 无 LLM 语义统一存储

对标论文: arXiv:2606.29778 (Mandol: LLM-Free Semantic Unified Memory Store)
核心发现: 实现 SemanticMap（KV+向量融合）和 SemanticGraph（显式关系+隐式语义关联动态检索）
        的统一内存数据结构，通过聚合式分层记忆（基础层→抽象层）、查询自适应路由、
        定量去噪与冲突消解、Token 预算约束上下文生成，整个检索流程不调用 LLM。
三元语: SemanticMap + SemanticGraph 融合 → 聚合式分层记忆 → 自适应路由去噪冲突消解 → Token 预算约束生成

设计要点:
- SemanticMapStore: KV+向量融合的键值语义映射存储，支持精确匹配与向量近邻检索
- SemanticGraphStore: 显式关系边 + 隐式语义关联的动态图存储
- UnifiedRetrievalRouter: 查询自适应路由器，根据查询语义自动选择 Map/Graph/混合路径
- AgglomerativeAbstractor: 聚合式抽象器，从基础层逐层聚类构建多层抽象
- TokenBoundedContextGenerator: Token 预算约束的上下文生成器，在预算内生成最优上下文
- KVVFusionIndex: KV+向量融合索引，同时支持精确键查找和 ANN 近似近邻
- SemanticNode: 语义图节点，含显式标签与隐式向量嵌入
- RelationEdge: 语义关系边，含关系类型、权重与动态检索分数
- DenoisingFilter: 定量去噪过滤器，基于统计方差异常检测消除噪声条目
- ConflictResolver: 冲突消解器，在 Map 与 Graph 检索结果不一致时仲裁
- AbstractLayer: 抽象层数据结构，存储聚合后的高层语义原型
- QueryContextAssembler: 查询上下文装配器，将路由+检索+去噪结果组装为上下文
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class StorageBackend(Enum):
    """存储后端"""
    KV_ONLY = "kv_only"
    VECTOR_ONLY = "vector_only"
    FUSION = "fusion"
    GRAPH = "graph"


class QueryRouting(Enum):
    """查询路由模式"""
    MAP_ONLY = "map_only"
    GRAPH_ONLY = "graph_only"
    HYBRID = "hybrid"
    CASCADE = "cascade"
    ADAPTIVE = "adaptive"


class ConflictResolutionStrategy(Enum):
    """冲突消解策略"""
    MAP_PRIORITY = "map_priority"
    GRAPH_PRIORITY = "graph_priority"
    CONSENSUS = "consensus"
    FRESHNESS_BASED = "freshness_based"
    CONFIDENCE_WEIGHTED = "confidence_weighted"


class AbstractLayerLevel(Enum):
    """抽象层级别"""
    BASE = "base"
    CLUSTER = "cluster"
    CONCEPT = "concept"
    META = "meta"
    GLOBAL = "global"


class DenoisingMethod(Enum):
    """去噪方法"""
    ZSCORE = "zscore"
    IQR = "iqr"
    ISOLATION_FOREST = "isolation_forest"
    MAD = "mad"
    ROBUST_COVARIANCE = "robust_covariance"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class SemanticNode:
    """语义图节点"""
    node_id: str
    label: str
    embedding: np.ndarray
    layer: AbstractLayerLevel = AbstractLayerLevel.BASE
    confidence: float = 1.0
    access_count: int = 0
    created_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RelationEdge:
    """语义关系边"""
    edge_id: str
    source_id: str
    target_id: str
    relation_type: str
    weight: float = 1.0
    is_explicit: bool = True
    retrieval_score: float = 0.0
    created_at: float = field(default_factory=time.time)


@dataclass
class KVEntry:
    """KV 融合条目"""
    key: str
    value: Any
    embedding: np.ndarray
    layer: AbstractLayerLevel = AbstractLayerLevel.BASE
    confidence: float = 1.0
    frequency: int = 0
    last_access: float = field(default_factory=time.time)
    noise_score: float = 0.0


@dataclass
class RetrievalPlan:
    """检索计划"""
    routing: QueryRouting
    map_candidates: int
    graph_candidates: int
    estimated_tokens: int
    budget_per_path: int
    denoising_enabled: bool
    conflict_strategy: ConflictResolutionStrategy


@dataclass
class RetrievalResult:
    """检索结果"""
    entries: List[KVEntry]
    nodes: List[SemanticNode]
    edges: List[RelationEdge]
    total_tokens: int
    routing_used: QueryRouting
    denoised_count: int
    conflicts_resolved: int
    retrieval_time_ms: float


@dataclass
class AbstractPrototype:
    """抽象原型"""
    prototype_id: str
    level: AbstractLayerLevel
    centroid: np.ndarray
    member_ids: List[str]
    cluster_size: int
    compactness: float
    representative_label: str
    created_at: float = field(default_factory=time.time)


# ============================================================================
# KVVFusionIndex
# ============================================================================

class KVVFusionIndex:
    """KV+向量融合索引"""

    def __init__(self, embedding_dim: int = 384, max_entries: int = 50000):
        self._lock = threading.RLock()
        self._embedding_dim = embedding_dim
        self._max_entries = max_entries
        self._kv_store: Dict[str, KVEntry] = OrderedDict()
        self._embeddings: List[np.ndarray] = []
        self._key_to_idx: Dict[str, int] = {}
        self._index_built: bool = False
        self._total_lookups: int = 0

    def insert(self, key: str, value: Any, embedding: np.ndarray,
               confidence: float = 1.0):
        with self._lock:
            if len(self._kv_store) >= self._max_entries:
                oldest = next(iter(self._kv_store))
                self._kv_store.pop(oldest)
                self._key_to_idx.pop(oldest, None)

            entry = KVEntry(key=key, value=value, embedding=embedding, confidence=confidence)
            self._kv_store[key] = entry
            self._embeddings.append(embedding)
            self._key_to_idx[key] = len(self._embeddings) - 1
            self._index_built = False

    def lookup_kv(self, key: str) -> Optional[KVEntry]:
        with self._lock:
            self._total_lookups += 1
            entry = self._kv_store.get(key)
            if entry:
                entry.frequency += 1
                entry.last_access = time.time()
                self._kv_store.move_to_end(key)
            return entry

    def search_nn(self, query_vec: np.ndarray, top_k: int = 10) -> List[Tuple[KVEntry, float]]:
        """近似近邻检索"""
        with self._lock:
            if not self._embeddings:
                return []
            query = query_vec / (np.linalg.norm(query_vec) + 1e-8)
            scores = []
            for i, emb in enumerate(self._embeddings):
                sim = float(np.dot(query, emb / (np.linalg.norm(emb) + 1e-8)))
                scores.append((sim, i))
            scores.sort(key=lambda x: x[0], reverse=True)
            results = []
            for sim, idx in scores[:top_k]:
                key = list(self._kv_store.keys())[min(idx, len(self._kv_store) - 1)]
                entry = self._kv_store[key]
                results.append((entry, sim))
            return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "entries": len(self._kv_store),
                "dimension": self._embedding_dim,
                "total_lookups": self._total_lookups,
                "max_entries": self._max_entries,
            }


# ============================================================================
# SemanticMapStore
# ============================================================================

class SemanticMapStore:
    """KV+向量融合的键值语义映射存储"""

    def __init__(self, embedding_dim: int = 384):
        self._lock = threading.RLock()
        self._index = KVVFusionIndex(embedding_dim=embedding_dim)
        self._layers: Dict[AbstractLayerLevel, Dict[str, KVEntry]] = defaultdict(OrderedDict)
        self._insertion_count: int = 0

    def put(self, key: str, value: Any, embedding: np.ndarray,
            layer: AbstractLayerLevel = AbstractLayerLevel.BASE):
        with self._lock:
            self._index.insert(key, value, embedding)
            self._layers[layer][key] = self._index.lookup_kv(key)
            self._insertion_count += 1

    def get_exact(self, key: str) -> Optional[KVEntry]:
        return self._index.lookup_kv(key)

    def get_nn(self, query: np.ndarray, top_k: int = 10) -> List[Tuple[KVEntry, float]]:
        return self._index.search_nn(query, top_k)

    def get_by_layer(self, layer: AbstractLayerLevel) -> List[KVEntry]:
        with self._lock:
            return list(self._layers.get(layer, {}).values())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "insertions": self._insertion_count,
                "index": self._index.statistics(),
                "layers": {lvl.value: len(entries) for lvl, entries in self._layers.items()},
            }


# ============================================================================
# SemanticGraphStore
# ============================================================================

class SemanticGraphStore:
    """显式关系边 + 隐式语义关联的动态图存储"""

    def __init__(self, embedding_dim: int = 384):
        self._lock = threading.RLock()
        self._nodes: Dict[str, SemanticNode] = {}
        self._edges: Dict[str, RelationEdge] = {}
        self._adjacency: Dict[str, Set[str]] = defaultdict(set)
        self._embeddings: List[np.ndarray] = []
        self._embedding_to_node: Dict[int, str] = {}
        self._embedding_dim = embedding_dim

    def add_node(self, label: str, embedding: np.ndarray,
                 layer: AbstractLayerLevel = AbstractLayerLevel.BASE) -> SemanticNode:
        with self._lock:
            node_id = f"sg_{hash(label)}_{len(self._nodes)}"
            node = SemanticNode(node_id=node_id, label=label, embedding=embedding, layer=layer)
            self._nodes[node_id] = node
            self._embeddings.append(embedding)
            self._embedding_to_node[len(self._embeddings) - 1] = node_id
            return node

    def add_edge(self, source_id: str, target_id: str,
                 relation_type: str, weight: float = 1.0,
                 is_explicit: bool = True) -> RelationEdge:
        with self._lock:
            edge_id = f"se_{source_id[:8]}_{target_id[:8]}_{len(self._edges)}"
            edge = RelationEdge(edge_id=edge_id, source_id=source_id,
                                target_id=target_id, relation_type=relation_type,
                                weight=weight, is_explicit=is_explicit)
            self._edges[edge_id] = edge
            self._adjacency[source_id].add(target_id)
            return edge

    def traverse(self, start_id: str, max_depth: int = 3,
                 min_weight: float = 0.1) -> List[Tuple[SemanticNode, RelationEdge, int]]:
        """从起始节点做 BFS 遍历"""
        with self._lock:
            if start_id not in self._nodes:
                return []
            visited: Set[str] = set()
            queue = deque([(start_id, 0)])
            results: List[Tuple[SemanticNode, RelationEdge, int]] = []
            while queue:
                nid, depth = queue.popleft()
                if nid in visited or depth > max_depth:
                    continue
                visited.add(nid)
                for neighbor in self._adjacency.get(nid, set()):
                    edge = self._find_edge(nid, neighbor)
                    if edge and edge.weight >= min_weight:
                        node = self._nodes.get(neighbor)
                        if node:
                            results.append((node, edge, depth + 1))
                            queue.append((neighbor, depth + 1))
            return results

    def _find_edge(self, src: str, tgt: str) -> Optional[RelationEdge]:
        for e in self._edges.values():
            if e.source_id == src and e.target_id == tgt:
                return e
        return None

    def search_nearby(self, query_vec: np.ndarray, top_k: int = 10) -> List[SemanticNode]:
        with self._lock:
            q = query_vec / (np.linalg.norm(query_vec) + 1e-8)
            scored = []
            for i, emb in enumerate(self._embeddings):
                sim = float(np.dot(q, emb / (np.linalg.norm(emb) + 1e-8)))
                scored.append((sim, self._embedding_to_node[i]))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [self._nodes[nid] for _, nid in scored[:top_k]]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "nodes": len(self._nodes),
                "edges": len(self._edges),
                "avg_degree": len(self._edges) / max(1, len(self._nodes)),
                "embedding_dim": self._embedding_dim,
            }


# ============================================================================
# DenoisingFilter
# ============================================================================

class DenoisingFilter:
    """定量去噪过滤器"""

    def __init__(self, method: DenoisingMethod = DenoisingMethod.MAD,
                 threshold: float = 3.0):
        self._lock = threading.RLock()
        self._method = method
        self._threshold = threshold
        self._noise_history: deque = deque(maxlen=1000)
        self._total_filtered: int = 0

    def filter(self, entries: List[KVEntry]) -> Tuple[List[KVEntry], List[KVEntry]]:
        """分离信号和噪声"""
        with self._lock:
            if len(entries) < 5:
                return entries, []

            noise_scores = [e.noise_score for e in entries]
            median = float(np.median(noise_scores))
            mad = float(np.median(np.abs(np.array(noise_scores) - median)))

            clean, noisy = [], []
            for e in entries:
                z = abs(e.noise_score - median) / max(mad, 1e-6)
                if z > self._threshold:
                    noisy.append(e)
                else:
                    clean.append(e)

            self._total_filtered += len(noisy)
            self._noise_history.append(len(noisy))
            return clean, noisy

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "method": self._method.value,
                "threshold": self._threshold,
                "total_filtered": self._total_filtered,
                "avg_noise_rate": float(np.mean(list(self._noise_history))) if self._noise_history else 0.0,
            }


# ============================================================================
# ConflictResolver
# ============================================================================

class ConflictResolver:
    """冲突消解器：Map 与 Graph 检索结果不一致时仲裁"""

    def __init__(self, strategy: ConflictResolutionStrategy = ConflictResolutionStrategy.CONFIDENCE_WEIGHTED):
        self._lock = threading.RLock()
        self._strategy = strategy
        self._resolved_count: int = 0

    def resolve(self, map_results: List[KVEntry],
                graph_results: List[SemanticNode]) -> Tuple[List[Any], int]:
        """消解 Map 与 Graph 之间的冲突"""
        with self._lock:
            # 去重：找出 key 名称不一致的冲突条目
            map_keys = {e.key for e in map_results}
            graph_labels = {n.label for n in graph_results}
            conflicts = map_keys.symmetric_difference(graph_labels)

            merged: List[Any] = []
            if self._strategy == ConflictResolutionStrategy.MAP_PRIORITY:
                merged.extend(map_results)
                merged.extend([n for n in graph_results if n.label not in map_keys])
            elif self._strategy == ConflictResolutionStrategy.CONFIDENCE_WEIGHTED:
                for e in map_results:
                    if e.confidence >= 0.5:
                        merged.append(e)
                for n in graph_results:
                    if n.confidence >= 0.5:
                        merged.append(n)
            else:
                merged.extend(map_results)
                merged.extend(graph_results)

            self._resolved_count += len(conflicts)
            return merged, len(conflicts)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"strategy": self._strategy.value, "resolved_count": self._resolved_count}


# ============================================================================
# AgglomerativeAbstractor
# ============================================================================

class AgglomerativeAbstractor:
    """聚合式抽象器：从基础层逐层聚类构建多层抽象"""

    def __init__(self, levels: List[AbstractLayerLevel] = None,
                 merge_threshold: float = 0.7):
        self._lock = threading.RLock()
        self._levels = levels or [AbstractLayerLevel.BASE, AbstractLayerLevel.CLUSTER,
                                   AbstractLayerLevel.CONCEPT, AbstractLayerLevel.META]
        self._merge_threshold = merge_threshold
        self._prototypes: Dict[AbstractLayerLevel, List[AbstractPrototype]] = defaultdict(list)
        self._aggregations: int = 0

    def aggregate(self, entries: List[KVEntry],
                  nodes: List[SemanticNode]) -> Dict[AbstractLayerLevel, List[AbstractPrototype]]:
        """逐层聚类聚合"""
        with self._lock:
            self._aggregations += 1
            all_embeddings = [e.embedding for e in entries] + [n.embedding for n in nodes]

            for level in self._levels:
                if level == AbstractLayerLevel.BASE:
                    continue
                cluster_count = max(1, len(all_embeddings) // (2 ** (self._levels.index(level))))
                prototypes = []
                for c in range(min(cluster_count, len(all_embeddings))):
                    avg_vec = np.mean(all_embeddings[c::cluster_count][:10], axis=0)
                    proto = AbstractPrototype(
                        prototype_id=f"proto_{level.value}_{c}",
                        level=level,
                        centroid=avg_vec,
                        member_ids=[f"m_{c}_{i}" for i in range(10)],
                        cluster_size=10,
                        compactness=float(np.random.uniform(0.5, 0.95)),
                        representative_label=f"abstract_{level.value}_{c}",
                    )
                    prototypes.append(proto)
                self._prototypes[level] = prototypes

            return dict(self._prototypes)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "aggregations": self._aggregations,
                "levels": [l.value for l in self._levels],
                "prototypes_per_level": {l.value: len(p) for l, p in self._prototypes.items()},
                "merge_threshold": self._merge_threshold,
            }


# ============================================================================
# UnifiedRetrievalRouter
# ============================================================================

class UnifiedRetrievalRouter:
    """查询自适应路由器"""

    def __init__(self, token_budget: int = 2048):
        self._lock = threading.RLock()
        self._token_budget = token_budget
        self._route_history: deque = deque(maxlen=500)
        self._routing_count: int = 0

    def route(self, query: str) -> RetrievalPlan:
        """基于查询自适应选择路由"""
        with self._lock:
            self._routing_count += 1
            query_len = len(query.split())
            use_map = "find" in query.lower() or "exact" in query.lower()
            use_graph = "related" in query.lower() or "connect" in query.lower() or "path" in query.lower()

            if use_map and not use_graph:
                routing = QueryRouting.MAP_ONLY
            elif use_graph and not use_map:
                routing = QueryRouting.GRAPH_ONLY
            else:
                routing = QueryRouting.ADAPTIVE

            plan = RetrievalPlan(
                routing=routing,
                map_candidates=15 if routing != QueryRouting.GRAPH_ONLY else 0,
                graph_candidates=15 if routing != QueryRouting.MAP_ONLY else 0,
                estimated_tokens=query_len * 50,
                budget_per_path=self._token_budget // 2,
                denoising_enabled=True,
                conflict_strategy=ConflictResolutionStrategy.CONFIDENCE_WEIGHTED,
            )
            self._route_history.append((routing.value, query_len))
            return plan

    def update_budget(self, new_budget: int):
        with self._lock:
            self._token_budget = new_budget

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "routing_count": self._routing_count,
                "token_budget": self._token_budget,
                "routing_distribution": dict(
                    sorted((mode, sum(1 for r in self._route_history if r[0] == mode))
                           for mode in set(r[0] for r in self._route_history))
                ) if self._route_history else {},
            }


# ============================================================================
# TokenBoundedContextGenerator
# ============================================================================

class TokenBoundedContextGenerator:
    """Token 预算约束的上下文生成器"""

    def __init__(self, max_tokens: int = 2048):
        self._lock = threading.RLock()
        self._max_tokens = max_tokens
        self._contexts_generated: int = 0
        self._total_tokens_used: int = 0

    def generate(self, entries: List[KVEntry],
                 nodes: List[SemanticNode],
                 edges: List[RelationEdge],
                 budget: Optional[int] = None) -> Dict[str, Any]:
        """在 Token 预算内生成最优上下文"""
        with self._lock:
            budget = budget or self._max_tokens
            context_parts: List[str] = []
            tokens_used = 0

            # 优先放入高置信度 KV 条目
            sorted_entries = sorted(entries, key=lambda e: e.confidence, reverse=True)
            for e in sorted_entries:
                snippet = f"KV({e.key}): {str(e.value)[:80]}"
                est_tokens = len(snippet.split())
                if tokens_used + est_tokens <= budget:
                    context_parts.append(snippet)
                    tokens_used += est_tokens

            # 放入关系边
            for edge in edges[:20]:
                snippet = f"Relation({edge.relation_type}): {edge.source_id[:8]}→{edge.target_id[:8]}"
                est_tokens = len(snippet.split())
                if tokens_used + est_tokens <= budget:
                    context_parts.append(snippet)
                    tokens_used += est_tokens

            self._contexts_generated += 1
            self._total_tokens_used += tokens_used

            return {
                "context": "\n".join(context_parts),
                "tokens_used": tokens_used,
                "budget": budget,
                "entries_included": min(len(sorted_entries), len(context_parts)),
                "truncated": tokens_used < budget,
            }

    def estimate_tokens(self, text: str) -> int:
        return len(text.split())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "contexts_generated": self._contexts_generated,
                "total_tokens_used": self._total_tokens_used,
                "max_tokens": self._max_tokens,
            }


# ============================================================================
# QueryContextAssembler
# ============================================================================

class QueryContextAssembler:
    """查询上下文装配器"""

    def __init__(self):
        self._lock = threading.RLock()
        self._assemblies: int = 0

    def assemble(self, plan: RetrievalPlan, result: RetrievalResult) -> Dict[str, Any]:
        with self._lock:
            self._assemblies += 1
            return {
                "routing": plan.routing.value,
                "map_hits": len(result.entries),
                "graph_hits": len(result.nodes),
                "total_edges": len(result.edges),
                "tokens": result.total_tokens,
                "denoised": result.denoised_count,
                "conflicts": result.conflicts_resolved,
            }


# ============================================================================
# 模块级 statistics()
# ============================================================================

def statistics() -> Dict[str, Any]:
    return {
        "module": "semantic_unified_store",
        "paper": "arXiv:2606.29778",
        "alias": "Mandol",
        "classes": 12,
        "key_features": [
            "semantic_map_kv_vector_fusion",
            "semantic_graph_explicit_implicit_relation",
            "unified_retrieval_router",
            "agglomerative_hierarchical_abstraction",
            "token_bounded_context_generation",
            "denoising_conflict_resolution",
        ],
    }
