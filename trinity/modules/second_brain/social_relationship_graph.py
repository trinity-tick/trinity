"""
# status: orphan (2026-08-15 audit, not in runtime path)
P21-6: Social Relationship Graph — Multi-Dimensional Social Network
====================================================================

对标方案：Social Relationship Memory for Long-Term Agent Societies (2026).

设计要点：
  - 友谊/敌对/合作/竞争多维关系网络
  - 关系强度指数衰减与事件驱动增长
  - 交互事件驱动图谱实时更新
  - 社区检测算法（模块度/Louvain）

核心组件：
  - SocialRelationshipGraph:  多维社会关系图谱
  - RelationshipEdge:         关系边（强度/类型/衰减）
  - CommunityDetector:        社区检测器（Louvain 变体）
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class RelationType(Enum):
    """多维关系类型。"""
    FRIENDSHIP = "friendship"        # 友谊
    RIVALRY = "rivalry"              # 敌对/竞争
    COOPERATION = "cooperation"      # 合作
    COMPETITION = "competition"      # 竞争
    MENTORSHIP = "mentorship"        # 指导
    ROMANTIC = "romantic"            # 浪漫


class InteractionIntensity(Enum):
    """交互强度等级。"""
    SUPERFICIAL = "superficial"      # +0.01
    CASUAL = "casual"                # +0.03
    MEANINGFUL = "meaningful"        # +0.07
    DEEP = "deep"                    # +0.12
    TRANSFORMATIVE = "transformative"  # +0.20


class DecayModelType(Enum):
    """关系衰减模型类型。"""
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    SIGMOID = "sigmoid"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class RelationshipEdge:
    """社会关系边。

    维护单向或双向关系强度。
    """
    edge_id: str
    source: str
    target: str
    relation_type: RelationType
    strength: float = 0.0         # 0~1 归一化强度
    interaction_count: int = 0
    last_interaction: float = 0.0
    decay_half_life_days: float = 30.0  # 30 天半衰期
    metadata: Dict[str, Any] = field(default_factory=dict)
    is_mutual: bool = False

    def age_days(self) -> float:
        return (time.time() - self.last_interaction) / 86400.0 if self.last_interaction else 0.0

    def apply_decay(self) -> float:
        """指数衰减：strength * 2^(-days/half_life)。"""
        days = self.age_days()
        if self.last_interaction == 0:
            return self.strength
        decay = 2.0 ** (-days / self.decay_half_life_days)
        return self.strength * decay

    def boost(self, intensity: InteractionIntensity, is_positive: bool = True):
        """事件驱动增长。"""
        increments = {
            InteractionIntensity.SUPERFICIAL: 0.01,
            InteractionIntensity.CASUAL: 0.03,
            InteractionIntensity.MEANINGFUL: 0.07,
            InteractionIntensity.DEEP: 0.12,
            InteractionIntensity.TRANSFORMATIVE: 0.20,
        }
        delta = increments.get(intensity, 0.03)
        if not is_positive:
            delta *= -1.0

        self.strength = max(0.0, min(1.0, self.strength + delta))
        self.interaction_count += 1
        self.last_interaction = time.time()


@dataclass
class SocialNode:
    """社会图谱节点。"""
    agent_id: str
    name: str
    community_id: Optional[int] = None
    degree_centrality: float = 0.0
    betweenness_centrality: float = 0.0
    clustering_coefficient: float = 0.0


@dataclass
class Community:
    """社区聚类结果。"""
    community_id: int
    members: List[str]
    modularity_contribution: float = 0.0
    internal_density: float = 0.0
    dominant_relation: Optional[RelationType] = None


@dataclass
class InteractionEvent:
    """交互事件。"""
    event_id: str
    source: str
    target: str
    relation_type: RelationType
    intensity: InteractionIntensity
    description: str = ""
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Constants
# ============================================================================

INTENSITY_INCREMENT: Dict[InteractionIntensity, float] = {
    InteractionIntensity.SUPERFICIAL: 0.01,
    InteractionIntensity.CASUAL: 0.03,
    InteractionIntensity.MEANINGFUL: 0.07,
    InteractionIntensity.DEEP: 0.12,
    InteractionIntensity.TRANSFORMATIVE: 0.20,
}


# ============================================================================
# Core Components
# ============================================================================

class SocialRelationshipGraph:
    """多维社会关系图谱。

    维护友谊/敌对/合作/竞争的动态关系网络。
    """

    def __init__(self, default_decay_half_life_days: float = 30.0):
        self._lock = threading.RLock()
        self.nodes: Dict[str, SocialNode] = {}
        self.edges: List[RelationshipEdge] = []
        self.adjacency: Dict[str, Dict[str, List[RelationshipEdge]]] = defaultdict(lambda: defaultdict(list))
        self.events: List[InteractionEvent] = []
        self.default_half_life = default_decay_half_life_days

    def add_agent(self, agent_id: str, name: str) -> SocialNode:
        """添加 Agent 节点。"""
        with self._lock:
            node = SocialNode(agent_id=agent_id, name=name)
            self.nodes[agent_id] = node
            return node

    def add_relationship(self, source: str, target: str, relation_type: RelationType,
                         initial_strength: float = 0.1, is_mutual: bool = False) -> RelationshipEdge:
        """添加关系边。"""
        with self._lock:
            edge = RelationshipEdge(
                edge_id=str(uuid.uuid4())[:8],
                source=source,
                target=target,
                relation_type=relation_type,
                strength=initial_strength,
                last_interaction=time.time(),
                decay_half_life_days=self.default_half_life,
                is_mutual=is_mutual,
            )
            self.edges.append(edge)
            self.adjacency[source][target].append(edge)
            if is_mutual:
                reverse_edge = RelationshipEdge(
                    edge_id=str(uuid.uuid4())[:8],
                    source=target,
                    target=source,
                    relation_type=relation_type,
                    strength=initial_strength,
                    last_interaction=time.time(),
                    decay_half_life_days=self.default_half_life,
                    is_mutual=False,
                )
                self.edges.append(reverse_edge)
                self.adjacency[target][source].append(reverse_edge)
            return edge

    def process_event(self, event: InteractionEvent):
        """交互事件驱动图谱更新。"""
        with self._lock:
            self.events.append(event)

            # 查找已有边
            existing = self.get_edges(event.source, event.target)
            if existing:
                for edge in existing:
                    if edge.relation_type == event.relation_type:
                        edge.boost(event.intensity, is_positive=True)
            else:
                self.add_relationship(
                    event.source, event.target, event.relation_type,
                    initial_strength=INTENSITY_INCREMENT.get(event.intensity, 0.05),
                    is_mutual=True,
                )

    def get_edges(self, source: str, target: str) -> List[RelationshipEdge]:
        """获取两节点间所有关系边。"""
        return self.adjacency.get(source, {}).get(target, [])

    def get_relationship_strength(self, source: str, target: str) -> float:
        """获取综合关系强度（所有边强度加权和）。"""
        edges = self.get_edges(source, target)
        if not edges:
            return 0.0
        # 考虑衰减
        return sum(e.apply_decay() for e in edges) / len(edges)

    def decay_all(self):
        """批量衰减所有关系。"""
        with self._lock:
            for edge in self.edges:
                edge.strength = edge.apply_decay()

    def get_strongest_relations(self, agent_id: str, top_k: int = 5) -> List[Tuple[str, float]]:
        """获取最强关系（考虑衰减后的实时强度）。"""
        with self._lock:
            scored = []
            for target_id, edges in self.adjacency.get(agent_id, {}).items():
                strength = sum(e.apply_decay() for e in edges) / max(len(edges), 1)
                scored.append((target_id, round(strength, 4)))
            scored.sort(key=lambda x: x[1], reverse=True)
            return scored[:top_k]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            rel_counts = defaultdict(int)
            for e in self.edges:
                rel_counts[e.relation_type.value] += 1
            return {
                "total_agents": len(self.nodes),
                "total_edges": len(self.edges),
                "total_events": len(self.events),
                "relation_distribution": dict(rel_counts),
                "avg_strength": round(
                    sum(e.strength for e in self.edges) / max(len(self.edges), 1), 4),
            }


class CommunityDetector:
    """社区检测器。

    基于模块度优化的 Louvain 变体。
    """

    def __init__(self, resolution: float = 1.0):
        self._lock = threading.RLock()
        self.resolution = resolution
        self.communities: Dict[int, Community] = {}

    def detect(self, graph: SocialRelationshipGraph) -> List[Community]:
        """检测社区结构。"""
        with self._lock:
            agents = list(graph.nodes.keys())
            n = len(agents)
            if n < 2:
                return []

            # 初始化：每个节点独立社区
            assignments: Dict[str, int] = {aid: i for i, aid in enumerate(agents)}
            self.communities = {
                i: Community(community_id=i, members=[aid])
                for i, aid in enumerate(agents)
            }

            # 简化 Louvain：贪心模块度优化
            m = len(graph.edges) or 1
            changed = True
            iteration = 0

            while changed and iteration < 100:
                changed = False
                iteration += 1

                for agent_id in agents:
                    current_community = assignments[agent_id]
                    best_community = current_community
                    best_delta_q = 0.0

                    # 计算邻居社区
                    neighbor_communities: Set[int] = set()
                    for target_id in graph.adjacency.get(agent_id, {}):
                        if target_id in assignments:
                            neighbor_communities.add(assignments[target_id])

                    for comm_id in neighbor_communities:
                        if comm_id == current_community:
                            continue
                        delta_q = self._delta_modularity(graph, agent_id, current_community, comm_id, assignments, m)
                        if delta_q > best_delta_q:
                            best_delta_q = delta_q
                            best_community = comm_id

                    if best_community != current_community:
                        # 移动节点
                        self.communities[current_community].members.remove(agent_id)
                        self.communities[best_community].members.append(agent_id)
                        assignments[agent_id] = best_community

                        graph.nodes[agent_id].community_id = best_community
                        changed = True

            # 清理空社区
            result = [c for c in self.communities.values() if c.members]
            # 计算内部密度
            for comm in result:
                comm.internal_density = self._internal_density(graph, comm.members)
            return result

    def _delta_modularity(self, graph: SocialRelationshipGraph, agent_id: str,
                          from_comm: int, to_comm: int,
                          assignments: Dict[str, int], m: int) -> float:
        """计算模块度变化。"""
        # 简化计算：基于邻居权重
        ki = sum(
            graph.get_relationship_strength(agent_id, t)
            for t in graph.adjacency.get(agent_id, {})
        )

        ki_in_to = sum(
            graph.get_relationship_strength(agent_id, t)
            for t in graph.adjacency.get(agent_id, {})
            if assignments.get(t) == to_comm
        )

        sigma_tot_to = sum(
            graph.get_relationship_strength(a, b)
            for a in self.communities.get(to_comm, Community(to_comm, [])).members
            for b in graph.adjacency.get(a, {})
        ) or 0.0

        sigma_tot_from = sum(
            graph.get_relationship_strength(a, b)
            for a in self.communities.get(from_comm, Community(from_comm, [])).members
            for b in graph.adjacency.get(a, {})
        ) or 0.0

        return self.resolution * (
            ki_in_to / m - sigma_tot_to * ki / (m * m)
            - (ki_in_to / m - sigma_tot_from * ki / (m * m))
        )

    def _internal_density(self, graph: SocialRelationshipGraph, members: List[str]) -> float:
        """社区内部密度。"""
        if len(members) < 2:
            return 1.0
        total_possible = len(members) * (len(members) - 1)
        actual = sum(
            1 for a in members for b in members
            if a != b and graph.get_edges(a, b)
        )
        return actual / total_possible

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_communities": len(self.communities),
                "avg_size": round(
                    sum(len(c.members) for c in self.communities.values()) /
                    max(len(self.communities), 1), 1),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P21-6 Social Relationship Graph",
        "benchmark": "Multi-Dimensional Social Network — Friendship/Rivalry/Cooperation/Competition",
        "classes": 3,
        "enums": 3,
        "dataclasses": 4,
        "key_pattern": "RelationEdge→Decay(exp)→EventBoost→CommunityDetect(Louvain)→Modularity",
        "key_metric": "4-dim relational network + exponential decay (30d half-life) + Louvain community detection",
        "thread_safe": True,
    }
