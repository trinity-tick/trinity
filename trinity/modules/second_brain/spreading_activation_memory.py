"""
# status: orphan (2026-08-15 audit, not in runtime path)
P21-4: Spreading Activation Memory — 扩散激活 + 侧抑制 + 时间衰减 + 三重混合检索

对标论文: Synapse (2026.08)
核心发现: 记忆检索应模拟神经突触的扩散激活机制——活跃节点沿图传播激活信号，
        侧抑制抑制弱相关节点，时间衰减淘汰陈旧记忆；
        三重混合检索（几何嵌入 + 激活图遍历 + 关键词匹配）覆盖不同检索需求。
三元语: 扩散激活 → 侧抑制 → 时间衰减 → 三重混合检索 → 动态图

设计要点:
- SpreadingActivation: 从种子节点沿图边扩散激活信号，模拟神经信号传播
- LateralInhibition: 侧抑制机制，活跃节点抑制邻域内低激活值节点
- TemporalDecay: 时间衰减函数，记忆节点激活值随时间指数/幂律衰减
- DynamicGraph: 可动态增删节点的有向加权图，支持实时更新
- TripleHybridRetriever: 三重混合检索——几何嵌入相似度 + 激活图遍历 + BM25 关键词，RRF 融合排序
- SynapseMemory: 顶层编排器，组合上述组件
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class ActivationMode(Enum):
    """扩散激活模式"""
    BFS = "bfs"                    # 广度优先扩散
    DEPTH_FIRST = "depth_first"    # 深度优先（沿高权重边）
    PULSE = "pulse"               # 脉冲式扩散（阶段性爆发）


class DecayModel(Enum):
    """时间衰减模型"""
    EXPONENTIAL = "exponential"
    POWER_LAW = "power_law"
    LINEAR = "linear"


class RetrievalChannel(Enum):
    """三重检索通道"""
    GEOMETRIC_EMBEDDING = "geometric_embedding"    # 向量嵌入相似度
    ACTIVATION_GRAPH = "activation_graph"           # 激活图遍历
    KEYWORD_BM25 = "keyword_bm25"                  # BM25 关键词匹配


class FusionStrategy(Enum):
    """融合策略"""
    RRF = "rrf"                        # Reciprocal Rank Fusion
    WEIGHTED_SUM = "weighted_sum"      # 加权求和
    CASCADE = "cascade"                # 级联过滤


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ActivationNode:
    """激活节点 — 动态图中的记忆单元"""
    node_id: str
    content: str
    embedding: List[float] = field(default_factory=list)
    activation: float = 0.0               # 当前激活值 (0~1)
    baseline: float = 0.05               # 基线激活值
    threshold: float = 0.1               # 触发扩散的阈值
    decay_rate: float = 0.02             # 每步衰减率
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_activated: float = 0.0
    access_count: int = 0


@dataclass
class GraphEdge:
    """动态图的有向边"""
    edge_id: str
    source_id: str
    target_id: str
    weight: float = 1.0                  # 边权重（影响激活传播强度）
    relation_type: str = "association"   # association / inhibition / temporal
    created_at: float = field(default_factory=time.time)
    co_occurrence: int = 0


@dataclass
class ActivationTrace:
    """单次扩散激活的完整轨迹"""
    trace_id: str
    seed_nodes: List[str]
    steps: List[Dict[str, Any]] = field(default_factory=list)  # [{node_id, activation, step}]
    inhibited_nodes: List[str] = field(default_factory=list)
    total_activated: int = 0
    elapsed_ms: float = 0.0


@dataclass
class HybridHit:
    """混合检索命中项"""
    node_id: str
    content: str
    score: float                          # RRF 融合分数
    channel_scores: Dict[str, float] = field(default_factory=dict)
    rank: int = 0


# ============================================================================
# Core Classes
# ============================================================================

class DynamicGraph:
    """动态图 — 可实时增删节点的有向加权图

    支持:
    - 节点/边的增删
    - 邻接查询
    - 节点激活值读写（线程安全）
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: Dict[str, ActivationNode] = {}
        self._edges: Dict[str, GraphEdge] = {}
        # source_id → [edge_ids]
        self._outgoing: Dict[str, List[str]] = defaultdict(list)
        # target_id → [edge_ids]
        self._incoming: Dict[str, List[str]] = defaultdict(list)
        self._edge_counter: int = 0

    def add_node(self, node: ActivationNode) -> None:
        with self._lock:
            self._nodes[node.node_id] = node

    def remove_node(self, node_id: str) -> None:
        with self._lock:
            self._nodes.pop(node_id, None)
            for eid in list(self._outgoing.get(node_id, [])):
                self._edges.pop(eid, None)
            self._outgoing.pop(node_id, None)
            self._incoming.pop(node_id, None)

    def add_edge(self, source_id: str, target_id: str, weight: float = 1.0,
                 relation_type: str = "association") -> GraphEdge:
        with self._lock:
            self._edge_counter += 1
            edge = GraphEdge(
                edge_id=f"edge_{self._edge_counter}",
                source_id=source_id,
                target_id=target_id,
                weight=weight,
                relation_type=relation_type,
            )
            self._edges[edge.edge_id] = edge
            self._outgoing[source_id].append(edge.edge_id)
            self._incoming[target_id].append(edge.edge_id)
            return edge

    def get_outgoing(self, node_id: str) -> List[Tuple[GraphEdge, str]]:
        """获取节点的所有出边 (edge, target_node_id)"""
        with self._lock:
            result = []
            for eid in self._outgoing.get(node_id, []):
                edge = self._edges.get(eid)
                if edge:
                    result.append((edge, edge.target_id))
            return result

    def get_node(self, node_id: str) -> Optional[ActivationNode]:
        with self._lock:
            return self._nodes.get(node_id)

    def set_activation(self, node_id: str, value: float) -> None:
        with self._lock:
            node = self._nodes.get(node_id)
            if node:
                node.activation = max(0.0, min(1.0, value))

    def all_nodes(self) -> List[ActivationNode]:
        with self._lock:
            return list(self._nodes.values())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self._nodes),
                "total_edges": len(self._edges),
                "avg_activation": round(
                    sum(n.activation for n in self._nodes.values()) / max(1, len(self._nodes)), 4
                ),
            }


class SpreadingActivation:
    """扩散激活 — 从种子节点沿图边传播激活信号

    参数:
    - decay_factor: 每跳传播的衰减因子（0~1）
    - max_hops: 最大传播跳数
    - activation_threshold: 低于此值的节点不继续传播
    - mode: BFS / DEPTH_FIRST / PULSE
    """

    def __init__(
        self,
        decay_factor: float = 0.5,
        max_hops: int = 4,
        activation_threshold: float = 0.05,
        mode: ActivationMode = ActivationMode.BFS,
    ):
        self.decay_factor = decay_factor
        self.max_hops = max_hops
        self.activation_threshold = activation_threshold
        self.mode = mode

    def spread(self, graph: DynamicGraph, seed_ids: List[str]) -> ActivationTrace:
        """执行扩散激活并返回轨迹"""
        t_start = time.time()
        trace = ActivationTrace(
            trace_id=f"trace_{int(t_start * 1000)}",
            seed_nodes=list(seed_ids),
        )
        step_counter = 0

        # 初始化种子节点激活值
        for sid in seed_ids:
            graph.set_activation(sid, 1.0)

        # BFS 扩散
        queue: List[Tuple[str, int, float]] = [(sid, 0, 1.0) for sid in seed_ids]
        visited: Set[str] = set()

        while queue and step_counter < self.max_hops * len(seed_ids) * 10:
            node_id, hop, incoming_activation = queue.pop(0)
            if node_id in visited or hop > self.max_hops:
                continue
            visited.add(node_id)
            step_counter += 1

            current = graph.get_node(node_id)
            if not current:
                continue

            trace.steps.append({
                "node_id": node_id,
                "activation": round(current.activation, 4),
                "hop": hop,
            })

            # 传播到邻居
            neighbors = graph.get_outgoing(node_id)
            for edge, target_id in neighbors:
                propagated = incoming_activation * edge.weight * (self.decay_factor ** hop)
                if propagated < self.activation_threshold:
                    continue
                target = graph.get_node(target_id)
                if target:
                    target.activation = min(1.0, target.activation + propagated)
                queue.append((target_id, hop + 1, propagated))

        trace.total_activated = len(visited)
        trace.elapsed_ms = (time.time() - t_start) * 1000.0
        return trace


class LateralInhibition:
    """侧抑制 — 活跃节点抑制邻域内低激活值节点

    实现 winner-take-all 风格的竞争:
    - 对每个节点，计算其 k 个最强邻居的平均激活值
    - 若自身激活值远低于邻居平均，则被抑制（激活值衰减）
    - 抑制强度与激活值差距成正比
    """

    def __init__(self, inhibition_strength: float = 0.3, k_neighbors: int = 5):
        self.inhibition_strength = inhibition_strength
        self.k_neighbors = k_neighbors

    def apply(self, graph: DynamicGraph) -> List[str]:
        """对图中所有节点施加侧抑制，返回被抑制节点列表"""
        inhibited: List[str] = []
        nodes = graph.all_nodes()

        for node in nodes:
            neighbors = graph.get_outgoing(node.node_id)
            if not neighbors:
                continue
            # 取 Top-K 邻居激活值
            neighbor_activations = []
            for edge, target_id in neighbors:
                target = graph.get_node(target_id)
                if target:
                    neighbor_activations.append(target.activation)
            neighbor_activations.sort(reverse=True)
            top_k_avg = (sum(neighbor_activations[:self.k_neighbors])
                         / max(1, len(neighbor_activations[:self.k_neighbors])))

            if node.activation < top_k_avg * 0.5:
                node.activation = max(0.0, node.activation - self.inhibition_strength * (top_k_avg - node.activation))
                inhibited.append(node.node_id)

        return inhibited


class TemporalDecay:
    """时间衰减 — 记忆节点激活值随时间衰减

    支持指数衰减、幂律衰减和线性衰减。
    每隔 decay_interval_seconds 对所有节点应用一次衰减。
    """

    def __init__(
        self,
        model: DecayModel = DecayModel.EXPONENTIAL,
        rate: float = 0.001,
        half_life_hours: float = 24.0,
    ):
        self.model = model
        self.rate = rate
        self.half_life_hours = half_life_hours

    def decay(self, node: ActivationNode, current_time: Optional[float] = None) -> float:
        """对单个节点应用时间衰减"""
        now = current_time or time.time()
        elapsed = now - max(node.last_activated, node.created_at)
        elapsed_hours = elapsed / 3600.0

        if self.model == DecayModel.EXPONENTIAL:
            decay = math.exp(-self.rate * elapsed_hours)
        elif self.model == DecayModel.POWER_LAW:
            if elapsed_hours < 1.0:
                decay = 1.0
            else:
                decay = elapsed_hours ** (-self.rate)
        elif self.model == DecayModel.LINEAR:
            decay = max(0.0, 1.0 - self.rate * elapsed_hours)
        else:
            decay = 1.0

        node.activation = max(node.baseline, node.activation * decay)
        node.last_activated = now
        return node.activation

    def apply_all(self, graph: DynamicGraph) -> None:
        now = time.time()
        for node in graph.all_nodes():
            self.decay(node, current_time=now)


class TripleHybridRetriever:
    """三重混合检索 — 几何嵌入 + 激活图遍历 + 关键词 BM25，RRF 融合

    三个通道:
    1. GEOMETRIC: 向量嵌入余弦相似度
    2. ACTIVATION_GRAPH: 基于扩散激活分数排序
    3. KEYWORD_BM25: 基于 BM25 词频排序

    融合: Reciprocal Rank Fusion (k=60)
    """

    def __init__(
        self,
        graph: DynamicGraph,
        channel_weights: Optional[Dict[str, float]] = None,
        rrf_k: int = 60,
    ):
        self.graph = graph
        self.channel_weights = channel_weights or {
            "geometric_embedding": 0.4,
            "activation_graph": 0.35,
            "keyword_bm25": 0.25,
        }
        self.rrf_k = rrf_k
        self._spreader = SpreadingActivation()

    def _geometric_search(self, query_embedding: List[float], limit: int = 30) -> List[Tuple[str, float]]:
        """几何嵌入通道"""
        if not query_embedding:
            return []
        results = []
        for node in self.graph.all_nodes():
            if node.embedding:
                sim = self._cosine_sim(query_embedding, node.embedding)
                results.append((node.node_id, sim))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def _activation_search(self, seed_ids: List[str], limit: int = 30) -> List[Tuple[str, float]]:
        """激活图遍历通道"""
        self._spreader.spread(self.graph, seed_ids)
        results = [(n.node_id, n.activation) for n in self.graph.all_nodes()]
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def _keyword_search(self, query: str, limit: int = 30) -> List[Tuple[str, float]]:
        """BM25 关键词通道 (简化 IDF 实现)"""
        if not query.strip():
            return []
        query_terms = query.lower().split()
        # 简易 IDF
        doc_count = max(1, len(self.graph._nodes))
        idf: Dict[str, float] = {}
        for term in query_terms:
            df = sum(1 for n in self.graph.all_nodes() if term in n.content.lower())
            idf[term] = math.log((doc_count - df + 0.5) / (df + 0.5) + 1.0)

        results = []
        for node in self.graph.all_nodes():
            score = 0.0
            content_lower = node.content.lower()
            for term in query_terms:
                tf = content_lower.count(term)
                score += idf.get(term, 0.0) * tf
            if score > 0:
                results.append((node.node_id, score))
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]

    def retrieve(self, query_text: str,
                 query_embedding: Optional[List[float]] = None,
                 seed_ids: Optional[List[str]] = None,
                 top_k: int = 20) -> List[HybridHit]:
        """执行三重混合检索并 RRF 融合"""
        # 三通道并行检索
        geo_results = self._geometric_search(query_embedding or [], limit=top_k * 2)
        act_results = self._activation_search(seed_ids or [], limit=top_k * 2)
        kw_results = self._keyword_search(query_text, limit=top_k * 2)

        # RRF 融合
        rrf_scores: Dict[str, float] = {}
        channel_detail: Dict[str, Dict[str, float]] = defaultdict(dict)

        for rank, (nid, score) in enumerate(geo_results):
            rrf_scores[nid] = rrf_scores.get(nid, 0) + self.channel_weights["geometric_embedding"] / (self.rrf_k + rank + 1)
            channel_detail[nid]["geometric_embedding"] = score

        for rank, (nid, score) in enumerate(act_results):
            rrf_scores[nid] = rrf_scores.get(nid, 0) + self.channel_weights["activation_graph"] / (self.rrf_k + rank + 1)
            channel_detail[nid]["activation_graph"] = score

        for rank, (nid, score) in enumerate(kw_results):
            rrf_scores[nid] = rrf_scores.get(nid, 0) + self.channel_weights["keyword_bm25"] / (self.rrf_k + rank + 1)
            channel_detail[nid]["keyword_bm25"] = score

        # 排序
        sorted_ids = sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        hits = []
        for rank, (nid, score) in enumerate(sorted_ids):
            node = self.graph.get_node(nid)
            hits.append(HybridHit(
                node_id=nid,
                content=node.content if node else "",
                score=score,
                channel_scores=channel_detail.get(nid, {}),
                rank=rank + 1,
            ))
        return hits

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na < 1e-12 or nb < 1e-12:
            return 0.0
        return dot / (na * nb)


class SynapseMemory:
    """Synapse 记忆 — 扩散激活记忆顶层编排器

    组合 DynamicGraph + SpreadingActivation + LateralInhibition + TemporalDecay + TripleHybridRetriever,
    模拟神经突触的扩散激活、侧抑制、时间衰减机制，提供三重混合检索。
    """

    def __init__(
        self,
        decay_factor: float = 0.5,
        max_hops: int = 4,
        inhibition_strength: float = 0.3,
        temporal_decay_rate: float = 0.001,
    ):
        self._lock = threading.RLock()
        self.graph = DynamicGraph()
        self.spreader = SpreadingActivation(decay_factor=decay_factor, max_hops=max_hops)
        self.inhibitor = LateralInhibition(inhibition_strength=inhibition_strength)
        self.temporal_decay = TemporalDecay(rate=temporal_decay_rate)
        self.retriever = TripleHybridRetriever(self.graph)
        self._query_count: int = 0

    # ---- 记忆录入 ----

    def add_memory(self, content: str, embedding: Optional[List[float]] = None,
                   node_id: Optional[str] = None) -> ActivationNode:
        with self._lock:
            nid = node_id or f"node_{int(time.time() * 1000)}_{len(self.graph._nodes)}"
            node = ActivationNode(
                node_id=nid,
                content=content,
                embedding=embedding or [],
            )
            self.graph.add_node(node)
            return node

    def add_association(self, source_id: str, target_id: str, weight: float = 1.0) -> GraphEdge:
        with self._lock:
            return self.graph.add_edge(source_id, target_id, weight)

    # ---- 激活与抑制 ----

    def activate(self, seed_ids: List[str]) -> ActivationTrace:
        with self._lock:
            return self.spreader.spread(self.graph, seed_ids)

    def inhibit(self) -> List[str]:
        with self._lock:
            return self.inhibitor.apply(self.graph)

    def apply_temporal_decay(self) -> None:
        with self._lock:
            self.temporal_decay.apply_all(self.graph)

    # ---- 检索 ----

    def query(self, query_text: str, query_embedding: Optional[List[float]] = None,
              top_k: int = 20) -> List[HybridHit]:
        with self._lock:
            self._query_count += 1
            # 先执行扩散激活（以激活值高的节点作为种子）
            active_nodes = sorted(self.graph.all_nodes(), key=lambda n: n.activation, reverse=True)[:5]
            seed_ids = [n.node_id for n in active_nodes if n.activation > n.threshold]
            return self.retriever.retrieve(
                query_text=query_text,
                query_embedding=query_embedding,
                seed_ids=seed_ids,
                top_k=top_k,
            )

    # ---- 诊断 ----

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "module": "SynapseMemory",
                "queries_served": self._query_count,
                "graph": self.graph.statistics(),
                "spreader_config": {
                    "decay_factor": self.spreader.decay_factor,
                    "max_hops": self.spreader.max_hops,
                },
                "inhibitor_config": {
                    "strength": self.inhibitor.inhibition_strength,
                },
            }
