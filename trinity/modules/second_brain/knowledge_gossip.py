"""
P4-1: Gossip Knowledge Propagation Protocol (对标 HyphaeDB)
===========================================================

基于 HNSW 图拓扑结构的 Gossip 知识传播协议。Agent 作为记忆图谱中的持久节点，
知识沿邻居结构自主扩散，支持能量衰减、矛盾检测、模式结晶、共识形成三种涌现行为。

HyphaeDB 三元语：
  1. 知识节点 (KnowledgeNode) — 带向量嵌入的知识单元
  2. 拓扑边 (TopologyEdge) — HNSW 邻居连接的加权有向边
  3. 记忆 diff (MemoryDiff) — 知识增删改的增量描述

设计要点：
  - Gossip 传播：每个节点以概率 p 向 k 个邻居转发 diff，能量按跳数指数衰减
  - 矛盾检测：语义向量余弦相似度 > 阈值但事实冲突时标记为矛盾
  - 模式结晶：多个节点达成相同知识 → 提升置信度 + 抽象层级
  - 共识形成：超半数邻居接受 diff → 共识达成 → 知识提升至更高抽象层

Reference: HyphaeDB (arxiv.org/html/2606.28781, March 2026)
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举与常量 ──────────────────────────────────────────────────────

class KnowledgeLayer(Enum):
    """知识抽象层级（对标 HyphaeDB multilayer hierarchy）。"""
    RAW = 0          # 原始观测
    PATTERN = 1      # 模式识别
    RULE = 2         # 规则抽象
    PRINCIPLE = 3    # 原理层
    CONSENSUS = 4    # 共识层（最高）


class DiffType(Enum):
    """记忆 diff 操作类型。"""
    ADD = auto()
    MODIFY = auto()
    DELETE = auto()
    CONFIRM = auto()   # 接收节点确认采纳


class ConsensusState(Enum):
    """共识形成状态。"""
    PENDING = auto()
    PROPAGATING = auto()
    CONTESTED = auto()     # 存在矛盾
    ACCEPTED = auto()      # 超半数接受
    CRYSTALLIZED = auto()  # 已结晶


# ── 三元语数据结构 ─────────────────────────────────────────────────

@dataclass
class KnowledgeNode:
    """知识节点 — HyphaeDB 三元语之一。

    Args:
        node_id: 全局唯一标识
        content: 知识内容摘要
        embedding: 语义向量（需与 HNSW 索引维度一致）
        layer: 当前抽象层级
        confidence: 置信度 [0, 1]
        energy: 当前剩余传播能量
        created_at: 创建时间戳
        version: 版本号（每次修改递增）
        metadata: 扩展元数据
    """

    node_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    content: str = ""
    embedding: Optional[List[float]] = None
    layer: KnowledgeLayer = KnowledgeLayer.RAW
    confidence: float = 1.0
    energy: float = 1.0
    created_at: float = field(default_factory=time.time)
    version: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TopologyEdge:
    """拓扑边 — HNSW 邻居连接的加权有向边。

    Args:
        source_id: 源节点 ID
        target_id: 目标节点 ID
        weight: 边权重（语义邻近度归一化）
        hop_distance: 图中最短路径跳数
        last_propagated: 上次沿此边传播的时间
        propagation_count: 累计传播次数
    """

    source_id: str
    target_id: str
    weight: float = 0.5
    hop_distance: int = 1
    last_propagated: float = 0.0
    propagation_count: int = 0


@dataclass
class MemoryDiff:
    """记忆 diff — 知识增量描述。

    Args:
        diff_id: 唯一标识
        origin_node_id: 产生此 diff 的源节点
        diff_type: 操作类型
        target_node: 受影响的知识节点（新建时为 None）
        new_content: diff 产生的新内容
        new_embedding: 新语义向量
        timestamp: diff 产生时间
        ttl_hops: 最大传播跳数
        current_hop: 已传播跳数
        energy_decay: 每跳能量衰减因子
        consensus_state: 共识状态
        acceptance_count: 接受此 diff 的节点数
        rejection_count: 拒绝此 diff 的节点数
    """

    diff_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    origin_node_id: str = ""
    diff_type: DiffType = DiffType.ADD
    target_node: Optional[KnowledgeNode] = None
    new_content: str = ""
    new_embedding: Optional[List[float]] = None
    timestamp: float = field(default_factory=time.time)
    ttl_hops: int = 7
    current_hop: int = 0
    energy_decay: float = 0.5          # 每跳能量保留率
    consensus_state: ConsensusState = ConsensusState.PENDING
    acceptance_count: int = 0
    rejection_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ── Gossip 协议引擎 ─────────────────────────────────────────────

class KnowledgeGossipProtocol:
    """Gossip 知识传播协议 — 对标 HyphaeDB。

    核心用法::

        from trinity.modules.second_brain.knowledge_gossip import KnowledgeGossipProtocol

        gossip = KnowledgeGossipProtocol(
            agent_id="agent-001",
            fanout=3,
            energy_decay=0.5,
        )

        # 注册知识节点
        node = KnowledgeNode(content="Q3毛利率22.3%", layer=KnowledgeLayer.RAW)
        gossip.register_node(node)

        # 添加拓扑边（来自 HNSW 索引的邻居关系）
        gossip.add_edge("node_1", "node_2", weight=0.87)

        # 创建并传播 diff
        diff = MemoryDiff(origin_node_id=node.node_id, diff_type=DiffType.ADD,
                          new_content="Q3毛利率22.3%")
        gossip.propagate(diff)

        # 检查涌现行为
        contradictions = gossip.detect_contradictions("Q3毛利率")
        crystals = gossip.list_crystallized()
        consensus = gossip.check_consensus(diff.diff_id)
    """

    # ── 构造函数 ──────────────────────────────────────────────────

    def __init__(
        self,
        agent_id: str = "default-agent",
        fanout: int = 3,
        energy_decay: float = 0.5,
        contradiction_threshold: float = 0.85,
        consensus_ratio: float = 0.5,
        max_hops: int = 7,
        similarity_fn: Optional[Callable[[List[float], List[float]], float]] = None,
    ):
        """初始化 Gossip 协议。

        Args:
            agent_id: 当前 Agent 标识
            fanout: 每轮向多少个邻居转发（k）
            energy_decay: 每跳能量衰减因子 γ ∈ (0, 1]
            contradiction_threshold: 语义相似度阈值，超过则进入矛盾检测
            consensus_ratio: 共识达成所需的最小接受比例
            max_hops: 最大传播跳数
            similarity_fn: 自定义向量相似度函数（默认余弦相似度）
        """
        self.agent_id = agent_id
        self.fanout = fanout
        self.energy_decay = energy_decay
        self.contradiction_threshold = contradiction_threshold
        self.consensus_ratio = consensus_ratio
        self.max_hops = max_hops

        # 内部存储
        self._nodes: Dict[str, KnowledgeNode] = {}
        self._edges: Dict[Tuple[str, str], TopologyEdge] = {}
        self._neighbors: Dict[str, List[str]] = defaultdict(list)
        self._diffs: Dict[str, MemoryDiff] = {}
        self._crystallized: List[KnowledgeNode] = []
        self._lock = threading.RLock()

        # 默认相似度：余弦
        self._similarity_fn = similarity_fn or self._cosine_similarity

    # ── 节点与拓扑管理 ────────────────────────────────────────────

    def register_node(self, node: KnowledgeNode) -> None:
        """注册知识节点（Agent 自身或发现的知识）。"""
        with self._lock:
            self._nodes[node.node_id] = node
            logger.debug("Node registered: %s (layer=%s)", node.node_id, node.layer.name)

    def add_edge(
        self, source_id: str, target_id: str, weight: float = 0.5, hop_distance: int = 1
    ) -> TopologyEdge:
        """添加/更新拓扑边（来自 HNSW 索引的邻居关系）。"""
        with self._lock:
            edge = TopologyEdge(
                source_id=source_id,
                target_id=target_id,
                weight=weight,
                hop_distance=hop_distance,
            )
            self._edges[(source_id, target_id)] = edge
            if target_id not in self._neighbors[source_id]:
                self._neighbors[source_id].append(target_id)
            return edge

    def get_neighbors(self, node_id: str) -> List[str]:
        """获取节点的邻居列表。"""
        return list(self._neighbors.get(node_id, []))

    # ── 核心传播逻辑 ────────────────────────────────────────────

    def propagate(self, diff: MemoryDiff) -> List[str]:
        """沿 HNSW 邻居结构扩散 diff。

        传播规则：
        1. 检查 TTL：current_hop >= ttl_hops → 停止
        2. 计算剩余能量：energy = decay_factor ^ current_hop
        3. 以概率 p = energy 向 fanout 个邻居转发
        4. 邻居收到后调用 _receive_diff 处理

        Returns:
            实际转发到的节点 ID 列表
        """
        with self._lock:
            if diff.current_hop >= diff.ttl_hops:
                logger.debug("Diff %s TTL expired at hop %d", diff.diff_id, diff.current_hop)
                return []

            self._diffs[diff.diff_id] = diff

            # 能量衰减
            energy = diff.energy_decay ** diff.current_hop
            if energy < 0.01:
                return []

            # 选择邻居
            origin = diff.origin_node_id
            candidates = self._neighbors.get(origin, [])
            if not candidates:
                return []

            # 按边权重排序，取 top-fanout
            weighted = sorted(
                [(n, self._edges.get((origin, n), TopologyEdge(origin, n)).weight)
                 for n in candidates],
                key=lambda x: x[1], reverse=True,
            )
            selected = []
            for neighbor_id, edge_weight in weighted[: self.fanout]:
                if random.random() < energy * edge_weight:
                    selected.append(neighbor_id)
                    # 更新边统计
                    if (origin, neighbor_id) in self._edges:
                        edge = self._edges[(origin, neighbor_id)]
                        edge.last_propagated = time.time()
                        edge.propagation_count += 1

            # 向选中的邻居转发
            diff.current_hop += 1
            for neighbor_id in selected:
                self._forward_to_neighbor(neighbor_id, diff)

            logger.info(
                "Diff %s propagated to %d/%d neighbors (hop %d, energy=%.3f)",
                diff.diff_id, len(selected), len(candidates), diff.current_hop, energy,
            )
            return selected

    def _forward_to_neighbor(self, neighbor_id: str, diff: MemoryDiff) -> None:
        """向邻居转发 diff — 邻居本地处理。"""
        neighbor = self._nodes.get(neighbor_id)
        if neighbor is None:
            return

        # 邻居本地决策：接受 / 拒绝 / 争议
        decision = self._local_decision(neighbor, diff)
        if decision == "accept":
            diff.acceptance_count += 1
            diff.consensus_state = ConsensusState.PROPAGATING
        elif decision == "reject":
            diff.rejection_count += 1
        elif decision == "contest":
            diff.consensus_state = ConsensusState.CONTESTED

    def _local_decision(self, node: KnowledgeNode, diff: MemoryDiff) -> str:
        """本地决策逻辑（可被子类覆盖实现更复杂的规则）。

        默认规则：
        - 能量 > 0.3 且无矛盾 → accept
        - 能量 < 0.1 → reject
        - 存在语义冲突 → contest
        """
        energy = diff.energy_decay ** diff.current_hop
        if energy < 0.05:
            return "reject"

        # 简化的矛盾检测：检查 diff 内容与本地节点内容是否冲突
        if node.embedding and diff.new_embedding:
            sim = self._similarity_fn(node.embedding, diff.new_embedding)
            # 高相似但置信度差异大 → 可能存在冲突
            if sim > self.contradiction_threshold and abs(node.confidence - 1.0) > 0.3:
                return "contest"

        if energy > 0.2:
            return "accept"
        return "reject"

    # ── 涌现行为 ──────────────────────────────────────────────────

    def detect_contradictions(self, topic: str) -> List[Tuple[KnowledgeNode, KnowledgeNode, float]]:
        """矛盾检测：发现语义相似但事实冲突的记忆对。

        Args:
            topic: 检测主题关键词（对内容做 substring 过滤）

        Returns:
            [(node_a, node_b, similarity)] 矛盾对列表
        """
        with self._lock:
            candidates = [
                n for n in self._nodes.values()
                if topic.lower() in n.content.lower() and n.embedding is not None
            ]
            contradictions = []
            for i, a in enumerate(candidates):
                for b in candidates[i + 1:]:
                    if a.embedding and b.embedding:
                        sim = self._similarity_fn(a.embedding, b.embedding)
                        # 高相似但内容关键词不同 → 可能是矛盾
                        if sim > self.contradiction_threshold:
                            a_words = set(a.content.lower().split())
                            b_words = set(b.content.lower().split())
                            jaccard = len(a_words & b_words) / max(len(a_words | b_words), 1)
                            if jaccard < 0.5 and abs(a.confidence - b.confidence) > 0.2:
                                contradictions.append((a, b, sim))

            logger.info("Contradiction detection for '%s': %d pairs found", topic, len(contradictions))
            return contradictions

    def check_consensus(self, diff_id: str) -> ConsensusState:
        """检查指定 diff 是否达成共识。

        共识条件：acceptance_count / (acceptance_count + rejection_count + 1) >= consensus_ratio
        """
        with self._lock:
            diff = self._diffs.get(diff_id)
            if diff is None:
                return ConsensusState.PENDING

            total = diff.acceptance_count + diff.rejection_count
            if total == 0:
                return diff.consensus_state

            ratio = diff.acceptance_count / total
            if ratio >= self.consensus_ratio:
                diff.consensus_state = ConsensusState.ACCEPTED

                # 共识达成 → 模式结晶门槛检查
                if self._should_crystallize(diff):
                    diff.consensus_state = ConsensusState.CRYSTALLIZED
                    self._crystallize(diff)

            return diff.consensus_state

    def _should_crystallize(self, diff: MemoryDiff) -> bool:
        """判断是否满足结晶条件：接受节点数 >= 3 且比例 >= 0.67。"""
        return diff.acceptance_count >= 3 and (
            diff.acceptance_count / max(diff.acceptance_count + diff.rejection_count, 1) >= 0.67
        )

    def _crystallize(self, diff: MemoryDiff) -> None:
        """模式结晶：创建/升级为更高抽象层的知识节点。"""
        content = diff.new_content or (diff.target_node.content if diff.target_node else "")
        crystal = KnowledgeNode(
            content=content,
            embedding=diff.new_embedding,
            layer=KnowledgeLayer.PATTERN,
            confidence=min(1.0, diff.acceptance_count / 5.0),
        )
        self._crystallized.append(crystal)
        self._nodes[crystal.node_id] = crystal
        logger.info("Pattern crystallized: %s (%d acceptances)", crystal.node_id, diff.acceptance_count)

    def list_crystallized(self) -> List[KnowledgeNode]:
        """列出所有已结晶的知识节点。"""
        return list(self._crystallized)

    # ── 辅助方法 ───────────────────────────────────────────────────

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """余弦相似度。"""
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(y * y for y in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def promote_node(self, node_id: str, target_layer: KnowledgeLayer) -> bool:
        """将节点提升到更高抽象层级（共识驱动）。"""
        with self._lock:
            node = self._nodes.get(node_id)
            if node is None:
                return False
            if target_layer.value <= node.layer.value:
                return False
            node.layer = target_layer
            logger.info("Node %s promoted to %s", node_id, target_layer.name)
            return True

    def get_node(self, node_id: str) -> Optional[KnowledgeNode]:
        """查询知识节点。"""
        return self._nodes.get(node_id)

    def get_diff(self, diff_id: str) -> Optional[MemoryDiff]:
        """查询记忆 diff。"""
        return self._diffs.get(diff_id)

    def statistics(self) -> Dict[str, Any]:
        """返回协议运行时统计。"""
        with self._lock:
            return {
                "agent_id": self.agent_id,
                "node_count": len(self._nodes),
                "edge_count": len(self._edges),
                "diff_count": len(self._diffs),
                "crystallized_count": len(self._crystallized),
                "consensus_diffs": sum(
                    1 for d in self._diffs.values()
                    if d.consensus_state in (ConsensusState.ACCEPTED, ConsensusState.CRYSTALLIZED)
                ),
                "contested_diffs": sum(
                    1 for d in self._diffs.values() if d.consensus_state == ConsensusState.CONTESTED
                ),
            }
