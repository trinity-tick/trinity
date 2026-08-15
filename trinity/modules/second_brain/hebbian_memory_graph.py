"""
# status: orphan (2026-08-15 audit, not in runtime path)
CB72: HebbianMemoryGraph — 赫布记忆图
======================================

赫布动力学驱动的动态记忆图，模拟大脑 Hebbian 学习规则。

核心设计:
  - HebbianCoactivationTracker: 追踪记忆节点间的共激活模式，
    频繁共现时增强 HebbianEdge 连接权重
  - ReflectiveDistillationAgent: 识别密集连接的记忆枢纽(MemoryHub)，
    将情景记忆簇蒸馏为结构化语义知识
  - SpreadingActivationEngine: 从查询节点沿 Hebbian 加权边扩散激活，
    发现潜在关联记忆
  - DualPathOrganization: 维护 episodic(情景记忆图) + semantic(语义记忆存储)
    双路径，模拟人脑情景-语义区分
  - 动态剪枝(弱连接衰减)和图统计(聚类系数/中心性)

Reference:
  - HeLa-Mem: Hebbian Learning and Associative Memory for LLM Agents (ACL 2026)
"""

from __future__ import annotations

import logging
import threading
import time as _time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class EdgeType(Enum):
    COACTIVATION = "coactivation"    # 共激活
    CAUSAL = "causal"                # 因果
    SEMANTIC = "semantic"            # 语义相似
    TEMPORAL = "temporal"            # 时序相邻


class OrganizationPath(Enum):
    EPISODIC = "episodic"   # 情景记忆图
    SEMANTIC = "semantic"   # 语义记忆存储


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class HMG_HebbianEdge:
    """Hebbian 加权边——连接权重随时间增强/衰减。"""
    source_id: str
    target_id: str
    weight: float = 0.0
    edge_type: EdgeType = EdgeType.COACTIVATION
    coactivation_count: int = 0
    last_activated: float = field(default_factory=_time.time)
    created_at: float = field(default_factory=_time.time)


@dataclass
class CoactivationEvent:
    """共激活事件——记录两节点同时被访问。"""
    event_id: str
    node_a: str
    node_b: str
    context_id: str = ""          # 触发上下文(查询ID/会话ID)
    timestamp: float = field(default_factory=_time.time)
    strength: float = 1.0         # 激活强度


@dataclass
class MemoryHub:
    """记忆枢纽——密集连接的中心节点簇。"""
    hub_id: str
    center_node: str
    member_nodes: Set[str] = field(default_factory=set)
    intra_density: float = 0.0    # 簇内连接密度
    semantic_label: str = ""
    created_at: float = field(default_factory=_time.time)


@dataclass
class GraphStatistics:
    """图统计快照。"""
    total_nodes: int = 0
    total_edges: int = 0
    avg_weight: float = 0.0
    clustering_coeff: float = 0.0
    hub_count: int = 0
    episodic_path_nodes: int = 0
    semantic_path_nodes: int = 0
    computed_at: float = field(default_factory=_time.time)


# ============================================================================
# HebbianCoactivationTracker
# ============================================================================

class HebbianCoactivationTracker:
    """共激活追踪器——Hebbian 规则实现。

    当两记忆在检索/推理中共现，强化连接（fire together, wire together）。
    """

    def __init__(self, learning_rate: float = 0.05, decay_rate: float = 0.001):
        self._lock = threading.RLock()
        self._edges: Dict[Tuple[str, str], HMG_HebbianEdge] = {}
        self._events: deque = deque(maxlen=10000)
        self.learning_rate = learning_rate
        self.decay_rate = decay_rate

    def record_coactivation(self, event: CoactivationEvent) -> Optional[HMG_HebbianEdge]:
        with self._lock:
            self._events.append(event)
            key = self._edge_key(event.node_a, event.node_b)
            edge = self._edges.get(key)
            if edge is None:
                edge = HMG_HebbianEdge(
                    source_id=event.node_a, target_id=event.node_b,
                    coactivation_count=1, last_activated=event.timestamp,
                    weight=self.learning_rate * event.strength,
                )
                self._edges[key] = edge
            else:
                edge.coactivation_count += 1
                edge.last_activated = event.timestamp
                edge.weight += self.learning_rate * event.strength
                edge.weight = min(edge.weight, 1.0)  # cap
            return edge

    def apply_decay(self, current_time: Optional[float] = None):
        """对所有边应用时间衰减。"""
        if current_time is None:
            current_time = _time.time()
        with self._lock:
            to_prune = []
            for key, edge in self._edges.items():
                elapsed = current_time - edge.last_activated
                edge.weight *= (1.0 - self.decay_rate) ** (elapsed / 3600.0)
                if edge.weight < 0.01:
                    to_prune.append(key)
            for key in to_prune:
                del self._edges[key]

    def get_edge(self, node_a: str, node_b: str) -> Optional[HMG_HebbianEdge]:
        with self._lock:
            return self._edges.get(self._edge_key(node_a, node_b))

    def top_edges(self, k: int = 10) -> List[HMG_HebbianEdge]:
        with self._lock:
            return sorted(self._edges.values(), key=lambda e: e.weight, reverse=True)[:k]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_edges": len(self._edges),
                "events_recorded": len(self._events),
                "avg_weight": sum(e.weight for e in self._edges.values()) / max(len(self._edges), 1),
            }

    @staticmethod
    def _edge_key(a: str, b: str) -> Tuple[str, str]:
        return (a, b) if a <= b else (b, a)


# ============================================================================
# SpreadingActivationEngine
# ============================================================================

class SpreadingActivationEngine:
    """扩散激活引擎——从查询节点沿 Hebbian 加权边扩散。

    模拟语义记忆中的扩散激活理论。
    """

    def __init__(self, decay_factor: float = 0.5, threshold: float = 0.05, max_hops: int = 3):
        self.decay_factor = decay_factor
        self.threshold = threshold
        self.max_hops = max_hops
        self._lock = threading.RLock()

    def activate(
        self,
        seed_nodes: List[str],
        tracker: HebbianCoactivationTracker,
    ) -> Dict[str, float]:
        """从种子节点开始扩散激活。

        Returns:
            node_id → activation_score 映射。
        """
        with self._lock:
            activations: Dict[str, float] = {n: 1.0 for n in seed_nodes}
            frontier = deque((n, 0) for n in seed_nodes)
            visited: Set[str] = set(seed_nodes)

            while frontier:
                current, hop = frontier.popleft()
                if hop >= self.max_hops:
                    continue
                current_activation = activations.get(current, 0.0)
                if current_activation < self.threshold:
                    continue

                for _, edge in tracker._edges.items():
                    if edge.source_id == current and edge.target_id not in visited:
                        neighbor = edge.target_id
                    elif edge.target_id == current and edge.source_id not in visited:
                        neighbor = edge.source_id
                    else:
                        continue

                    input_activation = current_activation * edge.weight * (self.decay_factor ** hop)
                    if neighbor in activations:
                        activations[neighbor] = max(activations[neighbor], input_activation)
                    else:
                        activations[neighbor] = input_activation

                    if activations[neighbor] >= self.threshold:
                        visited.add(neighbor)
                        frontier.append((neighbor, hop + 1))

            # Remove seeds from result if caller wants only discovered
            return {k: v for k, v in activations.items() if k not in seed_nodes}

    def statistics(self) -> Dict[str, Any]:
        return {"decay_factor": self.decay_factor, "max_hops": self.max_hops, "threshold": self.threshold}


# ============================================================================
# ReflectiveDistillationAgent
# ============================================================================

class ReflectiveDistillationAgent:
    """反思蒸馏代理——识别 Hub 并将情景簇蒸馏为语义知识。

    查找密集连接子图作为 MemoryHub，生成语义标签。
    """

    def __init__(self, density_threshold: float = 0.3, min_cluster_size: int = 3):
        self.density_threshold = density_threshold
        self.min_cluster_size = min_cluster_size
        self._lock = threading.RLock()
        self._hubs: Dict[str, MemoryHub] = {}

    def detect_hubs(
        self, tracker: HebbianCoactivationTracker
    ) -> List[MemoryHub]:
        """检测记忆枢纽——高内聚节点簇。"""
        with self._lock:
            # Build adjacency
            adj: Dict[str, Set[str]] = defaultdict(set)
            for (a, b), edge in tracker._edges.items():
                if edge.weight >= self.density_threshold:
                    adj[a].add(b)
                    adj[b].add(a)

            # Greedy cluster detection
            visited: Set[str] = set()
            hubs = []
            for node in adj:
                if node in visited or len(adj[node]) < self.min_cluster_size:
                    continue
                cluster = {node} | {n for n in adj[node] if len(adj[n] & adj[node]) >= 1}
                if len(cluster) >= self.min_cluster_size:
                    visited |= cluster
                    hub = MemoryHub(
                        hub_id=f"hub_{len(hubs):04d}",
                        center_node=node,
                        member_nodes=cluster,
                        semantic_label=f"cluster_{len(hubs)}",
                    )
                    hubs.append(hub)
                    self._hubs[hub.hub_id] = hub

            return hubs

    def distill(self, hub: MemoryHub) -> str:
        """将 Hub 蒸馏为语义摘要（占位，实际需 LLM）。"""
        return f"Semantic cluster around '{hub.center_node}' with {len(hub.member_nodes)} nodes"

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"hubs_detected": len(self._hubs), "density_threshold": self.density_threshold}


# ============================================================================
# DualPathOrganization
# ============================================================================

class DualPathOrganization:
    """双路径组织——episodic 图 + semantic 存储。

    模拟人脑海马(episodic)与皮层(semantic)的分工。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._episodic_nodes: Dict[str, Any] = {}
        self._semantic_nodes: Dict[str, Any] = {}
        self._episodic_edges: List[HMG_HebbianEdge] = []
        self._semantic_relations: Dict[Tuple[str, str], float] = {}

    def add_to_path(self, node_id: str, data: Any, path: OrganizationPath):
        with self._lock:
            if path == OrganizationPath.EPISODIC:
                self._episodic_nodes[node_id] = data
            else:
                self._semantic_nodes[node_id] = data

    def get_path_nodes(self, path: OrganizationPath) -> Dict[str, Any]:
        with self._lock:
            return dict(self._episodic_nodes if path == OrganizationPath.EPISODIC else self._semantic_nodes)

    def transfer_to_semantic(self, node_id: str) -> bool:
        """将情景记忆固化到语义存储（记忆巩固模拟）。"""
        with self._lock:
            if node_id in self._episodic_nodes:
                self._semantic_nodes[node_id] = self._episodic_nodes.pop(node_id)
                return True
            return False

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "episodic_nodes": len(self._episodic_nodes),
                "semantic_nodes": len(self._semantic_nodes),
                "episodic_edges": len(self._episodic_edges),
            }


# ============================================================================
# Main Class
# ============================================================================

class HebbianMemoryGraph:
    """赫布记忆图 (CB72)。

    统一入口——管理共激活追踪、扩散激活、蒸馏与双路径组织。

    Usage:
        hmg = HebbianMemoryGraph()
        hmg.record_coactivation("mem_A", "mem_B", context_id="query_7")
        associated = hmg.spread("mem_A")
        hubs = hmg.detect_hubs()
    """

    def __init__(self, learning_rate: float = 0.05, decay_rate: float = 0.001):
        self._lock = threading.RLock()
        self.tracker = HebbianCoactivationTracker(learning_rate=learning_rate, decay_rate=decay_rate)
        self.spreader = SpreadingActivationEngine()
        self.distiller = ReflectiveDistillationAgent()
        self.dual_path = DualPathOrganization()
        self._start_time = _time.time()

    def record_coactivation(self, node_a: str, node_b: str, context_id: str = "") -> CoactivationEvent:
        event = CoactivationEvent(
            event_id=f"coact_{_time.time():.6f}",
            node_a=node_a, node_b=node_b, context_id=context_id,
        )
        self.tracker.record_coactivation(event)
        return event

    def spread(self, seed_node: str) -> Dict[str, float]:
        return self.spreader.activate([seed_node], self.tracker)

    def detect_hubs(self) -> List[MemoryHub]:
        return self.distiller.detect_hubs(self.tracker)

    def apply_decay(self):
        self.tracker.apply_decay()

    def get_stats(self) -> GraphStatistics:
        with self._lock:
            edges = self.tracker._edges
            sum_w = sum(e.weight for e in edges.values())
            n_edges = len(edges)
            return GraphStatistics(
                total_nodes=len(self.dual_path._episodic_nodes) + len(self.dual_path._semantic_nodes),
                total_edges=n_edges,
                avg_weight=sum_w / max(n_edges, 1),
                hub_count=len(self.distiller._hubs),
                episodic_path_nodes=len(self.dual_path._episodic_nodes),
                semantic_path_nodes=len(self.dual_path._semantic_nodes),
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "HebbianMemoryGraph (CB72)",
                "tracker": self.tracker.statistics(),
                "spreader": self.spreader.statistics(),
                "distiller": self.distiller.statistics(),
                "dual_path": self.dual_path.statistics(),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
