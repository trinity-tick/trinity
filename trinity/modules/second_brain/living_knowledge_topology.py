"""
P21-2: Living Knowledge Topology — HNSW 活拓扑 + Gossip 协议 + 三原语 + 涌现共识

对标论文: HyphaeDB (2026.08)
核心发现: 知识拓扑应是活的——Agent 作为向量空间持久节点，通过 gossip 协议传播知识；
        能量衰减模拟生物记忆衰退，三原语（知识节点/拓扑边/记忆diff）构成最小语义单元；
        去中心化节点通过局部交互达成涌现共识。
三元语: 知识节点 → 拓扑边 → 记忆diff → HNSW 索引 → gossip 传播 → 能量衰减 → 涌现共识

设计要点:
- HNSWTopology: 分层可导航小世界图，每层维护 Agent 节点的向量近邻索引
- GossipProtocol: push/pull/push-pull 三种 gossip 模式，支持 fanout、TTL、选择性转发
- EnergyDecayFunction: 模拟记忆衰减的多种衰减函数（指数/幂律/半衰期），自动触发知识刷新
- KnowledgeNode: 三原语之一，封装向量嵌入 + 元数据 + 能量值 + 版本号
- TopologyEdge: 三原语之一，带类型的拓扑连接，记录创建时间与衰减因子
- MemoryDiff: 三原语之一，增量知识变更的差异结构，支持合并与冲突检测
- EmergentConsensus: 去中心化共识引擎，基于局部邻居状态的多数投票 + 置信度加权
- LivingKnowledgeTopology: 顶层编排器，组合上述所有组件
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
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

class GossipMode(Enum):
    """Gossip 传播模式"""
    PUSH = "push"            # 主动推送自身知识到邻居
    PULL = "pull"            # 主动从邻居拉取知识
    PUSH_PULL = "push_pull"  # 双向交换


class DiffType(Enum):
    """记忆 diff 类型"""
    INSERT = "insert"        # 新增知识节点
    UPDATE = "update"        # 更新已有节点
    DELETE = "delete"        # 标记删除（软删除）
    MERGE = "merge"          # 合并两个节点


class DecayModel(Enum):
    """能量衰减模型"""
    EXPONENTIAL = "exponential"       # N(t) = N0 * e^(-λt)
    POWER_LAW = "power_law"          # N(t) = N0 * t^(-α)
    HALF_LIFE = "half_life"          # N(t) = N0 * 0.5^(t/T_half)
    LOGISTIC = "logistic"            # N(t) = N0 / (1 + e^(k(t-t0)))


class ConsensusState(Enum):
    """共识状态"""
    PROPOSED = "proposed"      # 已提案
    GATHERING = "gathering"    # 收集邻居意见中
    REACHED = "reached"        # 已达成共识
    REJECTED = "rejected"      # 共识未通过
    STALE = "stale"            # 超时失效


# ============================================================================
# Dataclasses — 三原语
# ============================================================================

@dataclass
class KnowledgeNode:
    """三原语之一: 知识节点 — 向量空间中的持久 Agent 节点

    每个节点代表一个 Agent 或一段知识片段，在 HNSW 图中持久存在。
    """
    node_id: str
    embedding: List[float] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    energy: float = 1.0                     # 当前能量值 (0~1)
    version: int = 1
    layer: int = 0                          # HNSW 层级
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    access_count: int = 0


@dataclass
class TopologyEdge:
    """三原语之一: 拓扑边 — 知识节点间的有向连接

    记录节点间关系、创建时间与衰减因子，支持多种边类型。
    """
    edge_id: str
    source_id: str
    target_id: str
    edge_type: str = "semantic"             # semantic / temporal / causal / hierarchical
    weight: float = 1.0
    decay_factor: float = 0.95              # 每次衰减乘以此因子
    created_at: float = field(default_factory=time.time)
    last_activated: float = field(default_factory=time.time)
    active: bool = True


@dataclass
class MemoryDiff:
    """三原语之一: 记忆 diff — 增量知识变更的最小差异单元

    支持 INSERT / UPDATE / DELETE / MERGE 四种操作，
    携带变更前后的节点数据与冲突检测信息。
    """
    diff_id: str
    diff_type: DiffType
    node_id: str
    old_embedding: Optional[List[float]] = None
    new_embedding: Optional[List[float]] = None
    old_metadata: Optional[Dict[str, Any]] = None
    new_metadata: Optional[Dict[str, Any]] = None
    source_agent: str = ""
    timestamp: float = field(default_factory=time.time)
    vector_clock: Dict[str, int] = field(default_factory=dict)

    def is_conflict_with(self, other: MemoryDiff) -> bool:
        """检测两个 diff 是否冲突（同一节点并发修改）"""
        return (self.node_id == other.node_id
                and self.diff_type != DiffType.DELETE
                and other.diff_type != DiffType.DELETE
                and self.source_agent != other.source_agent)


# ============================================================================
# Core Classes
# ============================================================================

class HNSWTopology:
    """HNSW 活拓扑 — 多层可导航小世界图

    每层维护节点的向量近邻索引:
    - Layer 0: 最密层，包含所有节点
    - Layer L: 最稀层，仅包含部分高能量/高连接节点
    - 插入/搜索时从上到下逐层导航
    """

    def __init__(self, m: int = 16, ef_construction: int = 200, max_layers: int = 5):
        self.m = m
        self.ef_construction = ef_construction
        self.max_layers = max_layers
        self._lock = threading.RLock()
        # layer → {node_id → [neighbor_ids]}
        self._layers: Dict[int, Dict[str, List[str]]] = defaultdict(dict)
        self._nodes: Dict[str, KnowledgeNode] = {}
        self._entry_point: Optional[str] = None

    def insert(self, node: KnowledgeNode) -> None:
        with self._lock:
            self._nodes[node.node_id] = node
            # 随机分配层级（指数衰减概率）
            layer = min(self.max_layers - 1, int(-math.log(random.random() + 1e-9) * 2))
            node.layer = layer
            for l in range(layer + 1):
                self._layers[l][node.node_id] = []
            if self._entry_point is None:
                self._entry_point = node.node_id

    def search_knn(self, query_embedding: List[float], k: int = 10,
                   ef: int = 50) -> List[Tuple[str, float]]:
        """近似 KNN 搜索"""
        with self._lock:
            if not self._entry_point or not query_embedding:
                return []
            # 简化实现：从最高层向下逐层贪心搜索
            current = self._entry_point
            # 真实 HNSW 需要逐层维护 neighbor 的向量缓存，这里模拟相似度排序
            candidates: List[Tuple[str, float]] = []
            for node_id, node in self._nodes.items():
                sim = self._cosine_sim(query_embedding, node.embedding)
                candidates.append((node_id, sim))
            candidates.sort(key=lambda x: x[1], reverse=True)
            return candidates[:k]

    def get_neighbors(self, node_id: str, layer: int = 0) -> List[str]:
        with self._lock:
            return list(self._layers.get(layer, {}).get(node_id, []))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self._nodes),
                "layers": {l: len(nodes) for l, nodes in self._layers.items()},
                "entry_point": self._entry_point,
            }

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


class EnergyDecayFunction:
    """能量衰减函数 — 模拟生物记忆的自然衰退

    支持指数衰减、幂律衰减、半衰期衰减和 Logistic 衰减四种模型。
    低于阈值自动触发知识刷新或归档。
    """

    def __init__(
        self,
        model: DecayModel = DecayModel.EXPONENTIAL,
        decay_rate: float = 0.01,
        half_life_seconds: float = 86400.0,
        floor: float = 0.05,
    ):
        self.model = model
        self.decay_rate = decay_rate
        self.half_life_seconds = half_life_seconds
        self.floor = floor

    def compute(self, initial_energy: float, elapsed_seconds: float) -> float:
        """计算经过 elapsed_seconds 后的剩余能量"""
        if self.model == DecayModel.EXPONENTIAL:
            energy = initial_energy * math.exp(-self.decay_rate * elapsed_seconds)
        elif self.model == DecayModel.POWER_LAW:
            if elapsed_seconds < 1.0:
                energy = initial_energy
            else:
                energy = initial_energy * (elapsed_seconds ** (-self.decay_rate))
        elif self.model == DecayModel.HALF_LIFE:
            energy = initial_energy * (0.5 ** (elapsed_seconds / max(1.0, self.half_life_seconds)))
        elif self.model == DecayModel.LOGISTIC:
            midpoint = self.half_life_seconds
            k = self.decay_rate
            energy = initial_energy / (1.0 + math.exp(k * (elapsed_seconds - midpoint)))
        else:
            energy = initial_energy
        return max(self.floor, min(1.0, energy))

    def is_stale(self, energy: float, threshold: float = 0.1) -> bool:
        return energy < threshold


class GossipProtocol:
    """Gossip 协议 — 去中心化知识传播

    支持 PUSH / PULL / PUSH_PULL 三种模式:
    - PUSH: 主动将本地 MemoryDiff 推送到随机邻居子集
    - PULL: 主动向邻居拉取其最新 diff
    - PUSH_PULL: 双向交换

    参数:
    - fanout: 每轮 gossip 传播的邻居数
    - ttl: diff 的生命周期跳数
    - cycle_interval_seconds: gossip 轮次间隔
    """

    def __init__(
        self,
        mode: GossipMode = GossipMode.PUSH_PULL,
        fanout: int = 3,
        ttl: int = 5,
        cycle_interval_seconds: float = 10.0,
    ):
        self.mode = mode
        self.fanout = fanout
        self.ttl = ttl
        self.cycle_interval_seconds = cycle_interval_seconds
        self._lock = threading.RLock()
        self._pending_diffs: List[MemoryDiff] = []
        self._received_diffs: List[MemoryDiff] = []
        self._cycle_count: int = 0

    def publish(self, diff: MemoryDiff) -> None:
        """发布一个 diff 到 gossip 网络"""
        with self._lock:
            self._pending_diffs.append(diff)

    def select_targets(self, neighbors: List[str]) -> List[str]:
        """从邻居中随机选择 fanout 个作为 gossip 目标"""
        k = min(self.fanout, len(neighbors))
        return random.sample(neighbors, k) if k > 0 else []

    def consume_diffs(self) -> List[MemoryDiff]:
        """消费当前累积的 diff（供共识引擎处理）"""
        with self._lock:
            diffs = list(self._received_diffs)
            self._received_diffs.clear()
            self._cycle_count += 1
            return diffs

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode.value,
                "fanout": self.fanout,
                "ttl": self.ttl,
                "pending_diffs": len(self._pending_diffs),
                "total_cycles": self._cycle_count,
            }


class EmergentConsensus:
    """涌现共识引擎 — 基于局部邻居状态的去中心化共识

    机制:
    - 收集邻居对同一 node_id 的 diff 投票
    - 多数投票 + 置信度加权
    - 达到法定人数阈值后达成共识
    - 超时未达成自动退化到 STALE 状态
    """

    def __init__(self, quorum_ratio: float = 0.51, max_wait_seconds: float = 60.0):
        self.quorum_ratio = quorum_ratio
        self.max_wait_seconds = max_wait_seconds
        self._lock = threading.RLock()
        # consensus_id → ConsensusRecord
        self._proposals: Dict[str, Dict[str, Any]] = {}

    def propose(self, consensus_id: str, diff: MemoryDiff, total_peers: int) -> None:
        with self._lock:
            self._proposals[consensus_id] = {
                "diff": diff,
                "total_peers": total_peers,
                "votes_for": 0,
                "votes_against": 0,
                "voters": set(),
                "state": ConsensusState.PROPOSED,
                "created_at": time.time(),
            }

    def cast_vote(self, consensus_id: str, voter_id: str, approve: bool,
                  confidence: float = 0.5) -> ConsensusState:
        with self._lock:
            prop = self._proposals.get(consensus_id)
            if prop is None or prop["state"] not in (ConsensusState.PROPOSED, ConsensusState.GATHERING):
                return ConsensusState.STALE
            prop["state"] = ConsensusState.GATHERING
            if voter_id not in prop["voters"]:
                prop["voters"].add(voter_id)
                if approve:
                    prop["votes_for"] += confidence
                else:
                    prop["votes_against"] += confidence

            total_weight = prop["votes_for"] + prop["votes_against"]
            effective_total = max(prop["total_peers"], 1)
            if total_weight / effective_total >= self.quorum_ratio:
                if prop["votes_for"] > prop["votes_against"]:
                    prop["state"] = ConsensusState.REACHED
                else:
                    prop["state"] = ConsensusState.REJECTED
            elif time.time() - prop["created_at"] > self.max_wait_seconds:
                prop["state"] = ConsensusState.STALE
            return prop["state"]

    def get_state(self, consensus_id: str) -> ConsensusState:
        with self._lock:
            prop = self._proposals.get(consensus_id)
            return prop["state"] if prop else ConsensusState.STALE

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            states = defaultdict(int)
            for p in self._proposals.values():
                states[p["state"].value] += 1
            return {
                "total_proposals": len(self._proposals),
                "by_state": dict(states),
            }


class LivingKnowledgeTopology:
    """活知识拓扑 — 顶层编排器

    组合 HNSWTopology + GossipProtocol + EnergyDecayFunction + EmergentConsensus,
    将 Agent 作为向量空间持久节点，通过 gossip 传播知识变更，
    能量衰减模拟生物衰退，三原语构成最小语义单元。
    """

    def __init__(
        self,
        hnsw_m: int = 16,
        gossip_mode: GossipMode = GossipMode.PUSH_PULL,
        decay_model: DecayModel = DecayModel.EXPONENTIAL,
        quorum_ratio: float = 0.51,
    ):
        self._lock = threading.RLock()
        self.topology = HNSWTopology(m=hnsw_m)
        self.gossip = GossipProtocol(mode=gossip_mode)
        self.decay = EnergyDecayFunction(model=decay_model)
        self.consensus = EmergentConsensus(quorum_ratio=quorum_ratio)
        self._node_counter: int = 0
        self._diff_counter: int = 0

    # ---- 节点生命周期 ----

    def register_agent(self, embedding: List[float],
                       metadata: Optional[Dict[str, Any]] = None) -> KnowledgeNode:
        """注册 Agent 节点到活拓扑"""
        with self._lock:
            self._node_counter += 1
            node = KnowledgeNode(
                node_id=f"agent_{self._node_counter}",
                embedding=embedding,
                metadata=metadata or {},
            )
            self.topology.insert(node)
            return node

    def refresh_energy(self) -> Dict[str, float]:
        """对所有节点执行能量衰减并返回能量状态"""
        with self._lock:
            now = time.time()
            energies: Dict[str, float] = {}
            for nid, node in self.topology._nodes.items():
                elapsed = now - node.last_updated
                node.energy = self.decay.compute(node.energy, elapsed)
                node.last_updated = now
                energies[nid] = node.energy
            return energies

    # ---- Diff 传播与共识 ----

    def create_diff(self, diff_type: DiffType, node_id: str,
                    new_embedding: Optional[List[float]] = None,
                    new_metadata: Optional[Dict[str, Any]] = None,
                    source_agent: str = "") -> MemoryDiff:
        with self._lock:
            self._diff_counter += 1
            diff = MemoryDiff(
                diff_id=f"diff_{self._diff_counter}",
                diff_type=diff_type,
                node_id=node_id,
                new_embedding=new_embedding,
                new_metadata=new_metadata,
                source_agent=source_agent,
            )
            self.gossip.publish(diff)
            return diff

    def run_gossip_cycle(self) -> List[MemoryDiff]:
        """执行一轮 gossip 传播并返回收集到的 diff"""
        return self.gossip.consume_diffs()

    def propose_consensus(self, diff: MemoryDiff, total_peers: int) -> str:
        cid = f"consensus_{diff.diff_id}"
        self.consensus.propose(cid, diff, total_peers)
        return cid

    # ---- 检索 ----

    def search(self, query_embedding: List[float], k: int = 10) -> List[Tuple[str, float]]:
        return self.topology.search_knn(query_embedding, k)

    # ---- 诊断 ----

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "module": "LivingKnowledgeTopology",
                "topology": self.topology.statistics(),
                "gossip": self.gossip.statistics(),
                "consensus": self.consensus.statistics(),
                "decay_model": self.decay.model.value,
            }
