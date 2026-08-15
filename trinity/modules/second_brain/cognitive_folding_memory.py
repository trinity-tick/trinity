"""
# status: orphan (2026-08-15 audit, not in runtime path)
P22-1: Cognitive Folding Memory — 脑启发三层 CLS 主动记忆

对标论文: Cognifold (脑启发认知折叠记忆, 2026.08)
核心发现: 记忆系统应模拟海马-新皮层-前额叶三层协作架构：
        海马事件流驱动装配 → 新皮层相似合并/陈旧衰减 → 前额叶意图涌现。
        概念簇密度超阈值时自动涌现高层意图，驱动主动记忆重组。
三元语: 事件流装配 → 相似合并 → 陈旧衰减 → 联想召回重连 → 概念簇密度 → 意图涌现

设计要点:
- EventStreamAssembler: 从事件流中实时装配记忆片段，构建初始拓扑图
- SimilarityMerger: 基于语义相似度合并冗余记忆节点，防止图膨胀
- StalenessDecayer: 按时间衰减陈旧连接权重，保持图拓扑新鲜度
- AssociativeRecallRewirer: 联想召回时动态重连，增强检索路径
- ConceptClusterDensityDetector: 监控概念簇密度，超阈值时触发意图涌现
- CLSActiveMemoryEngine: 海马/新皮层/前额叶意图三层编排器
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Enums & Constants
# ============================================================================


class BrainLayer(Enum):
    """三层脑区映射"""
    HIPPOCAMPUS = "hippocampus"       # 海马：事件流驱动快速编码
    NEOCORTEX = "neocortex"           # 新皮层：相似合并 + 陈旧衰减
    PREFRONTAL = "prefrontal"         # 前额叶：意图涌现 + 主动重组


class AssemblyPhase(Enum):
    """事件流装配阶段"""
    INGEST = "ingest"                 # 摄入：接收原始事件
    TOKENIZE = "tokenize"             # 分词：提取语义单元
    BIND = "bind"                     # 绑定：关联已有记忆节点
    COMMIT = "commit"                 # 提交：写入图拓扑


class MergeStrategy(Enum):
    """相似合并策略"""
    COSINE_FUSION = "cosine_fusion"         # 余弦相似度加权融合
    JACCARD_INTERSECTION = "jaccard_intersection"  # Jaccard交集合并
    HIERARCHICAL_CLUSTER = "hierarchical_cluster"  # 层次聚类合并
    ENTROPY_THRESHOLD = "entropy_threshold"        # 信息熵阈值合并


class CLSDecayModel(Enum):
    """陈旧衰减模型（CLS 专属，避免与 Synapse DecayModel 冲突）"""
    EXPONENTIAL = "exponential"       # 指数衰减 e^(-λt)
    POWER_LAW = "power_law"           # 幂律衰减 t^(-α)
    LINEAR = "linear"                 # 线性衰减
    EBBINGHAUS = "ebbinghaus"         # 艾宾浩斯遗忘曲线


class IntentSignal(Enum):
    """意图涌现信号类型"""
    DENSITY_SURGE = "density_surge"         # 概念簇密度突破阈值
    RETRIEVAL_FREQUENCY = "retrieval_frequency"  # 检索频率突增
    CROSS_CLUSTER_BRIDGE = "cross_cluster_bridge"  # 跨簇桥接涌现
    NOVELTY_DETECTION = "novelty_detection"        # 新颖性检测触发


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class EventRecord:
    """原始事件记录"""
    event_id: str
    timestamp: float
    raw_content: str
    source: str = "unknown"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryFragment:
    """记忆片段（海马层产出）"""
    fragment_id: str
    tokens: List[str]
    embedding: List[float] = field(default_factory=list)
    bound_nodes: Set[str] = field(default_factory=set)
    confidence: float = 1.0
    created_at: float = field(default_factory=time.time)


@dataclass
class TopologyNode:
    """图拓扑节点"""
    node_id: str
    content_hash: str
    embedding: List[float] = field(default_factory=list)
    layer: BrainLayer = BrainLayer.HIPPOCAMPUS
    activation: float = 1.0
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    merge_count: int = 0


@dataclass
class CLSTopologyEdge:
    """图拓扑边（CLS 专属，避免与 HyphaeDB TopologyEdge 冲突）"""
    edge_id: str
    source_id: str
    target_id: str
    weight: float = 1.0
    relation_type: str = "associative"
    created_at: float = field(default_factory=time.time)
    decay_factor: float = 1.0


@dataclass
class ConceptCluster:
    """概念簇"""
    cluster_id: str
    node_ids: Set[str]
    centroid_embedding: List[float] = field(default_factory=list)
    density: float = 0.0
    member_count: int = 0
    intent_signals: List[IntentSignal] = field(default_factory=list)


@dataclass
class IntentEmergence:
    """意图涌现结果"""
    intent_id: str
    cluster_id: str
    signal_type: IntentSignal
    description: str
    triggered_nodes: List[str]
    confidence: float
    priority: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class CLSStats:
    """CLS 引擎统计"""
    total_events_processed: int = 0
    total_fragments: int = 0
    total_nodes: int = 0
    total_edges: int = 0
    merges_performed: int = 0
    decays_applied: int = 0
    rewires_performed: int = 0
    intents_emerged: int = 0
    clusters_active: int = 0

    def summary(self) -> Dict[str, Any]:
        return {
            "events": self.total_events_processed,
            "fragments": self.total_fragments,
            "nodes": self.total_nodes,
            "edges": self.total_edges,
            "merges": self.merges_performed,
            "decays": self.decays_applied,
            "rewires": self.rewires_performed,
            "intents": self.intents_emerged,
            "clusters": self.clusters_active,
        }


# ============================================================================
# Core Classes
# ============================================================================


class EventStreamAssembler:
    """事件流驱动装配器 — 海马层

    从原始事件流中实时提取语义单元，绑定到已有拓扑节点，
    生成 MemoryFragment 作为海马到新皮层的交接单元。
    """

    def __init__(self, tokenizer_fn: Optional[Callable[[str], List[str]]] = None) -> None:
        self._tokenizer = tokenizer_fn or (lambda s: s.split())
        self._lock = threading.RLock()
        self._buffer: deque[EventRecord] = deque(maxlen=256)
        self._total_processed = 0

    def ingest(self, event: EventRecord) -> MemoryFragment:
        """摄入单个事件并装配为记忆片段"""
        with self._lock:
            self._buffer.append(event)
            self._total_processed += 1
        tokens = self._tokenizer(event.raw_content)
        frag_id = f"frag_{event.event_id}_{len(tokens)}"
        return MemoryFragment(
            fragment_id=frag_id,
            tokens=tokens,
            confidence=0.95,
        )

    def ingest_batch(self, events: List[EventRecord]) -> List[MemoryFragment]:
        """批量摄入事件流"""
        return [self.ingest(e) for e in events]

    @property
    def total_processed(self) -> int:
        return self._total_processed


class SimilarityMerger:
    """相似合并器 — 新皮层

    基于语义向量余弦相似度检测冗余节点，执行加权融合合并，
    减少图拓扑膨胀，保留信息完整性。
    """

    def __init__(
        self,
        threshold: float = 0.85,
        strategy: MergeStrategy = MergeStrategy.COSINE_FUSION,
    ) -> None:
        self._threshold = threshold
        self._strategy = strategy
        self._lock = threading.RLock()
        self._merge_count = 0

    def _cosine_sim(self, a: List[float], b: List[float]) -> float:
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _jaccard_sim(self, a_tokens: Set[str], b_tokens: Set[str]) -> float:
        if not a_tokens or not b_tokens:
            return 0.0
        intersection = len(a_tokens & b_tokens)
        union = len(a_tokens | b_tokens)
        return intersection / union if union > 0 else 0.0

    def should_merge(self, node_a: TopologyNode, node_b: TopologyNode) -> bool:
        """判断两个节点是否应该合并"""
        sim = self._cosine_sim(node_a.embedding, node_b.embedding)
        return sim >= self._threshold

    def merge(self, node_a: TopologyNode, node_b: TopologyNode) -> TopologyNode:
        """执行相似合并，保留 a 作为主节点"""
        with self._lock:
            self._merge_count += 1
        if node_a.embedding and node_b.embedding and len(node_a.embedding) == len(node_b.embedding):
            merged_emb = [(x + y) / 2.0 for x, y in zip(node_a.embedding, node_b.embedding)]
        else:
            merged_emb = node_a.embedding
        node_a.embedding = merged_emb
        node_a.merge_count += 1
        node_a.activation = max(node_a.activation, node_b.activation)
        return node_a

    @property
    def merge_count(self) -> int:
        return self._merge_count


class StalenessDecayer:
    """陈旧衰减器 — 新皮层

    按时间衰减边权重，模拟记忆的自然遗忘曲线，
    支持指数/幂律/线性/艾宾浩斯四种衰减模型。
    """

    def __init__(
        self,
        model: CLSDecayModel = CLSDecayModel.EXPONENTIAL,
        rate: float = 0.01,
        floor: float = 0.01,
    ) -> None:
        self._model = model
        self._rate = rate
        self._floor = floor
        self._lock = threading.RLock()
        self._decay_count = 0

    def decay_weight(self, edge: CLSTopologyEdge, current_time: float) -> float:
        """计算衰减后的边权重"""
        age = max(0.0, current_time - edge.created_at)
        if self._model == CLSDecayModel.EXPONENTIAL:
            factor = math.exp(-self._rate * age)
        elif self._model == CLSDecayModel.POWER_LAW:
            factor = max(0.0, age ** (-self._rate) if age > 0 else 1.0)
        elif self._model == CLSDecayModel.LINEAR:
            factor = max(0.0, 1.0 - self._rate * age)
        elif self._model == CLSDecayModel.EBBINGHAUS:
            k = 0.2
            factor = 1.0 / (1.0 + k * age)
        else:
            factor = 1.0
        return max(self._floor, factor)

    def apply(self, edge: CLSTopologyEdge, current_time: Optional[float] = None) -> CLSTopologyEdge:
        """对单条边施加衰减"""
        now = current_time or time.time()
        with self._lock:
            self._decay_count += 1
        factor = self.decay_weight(edge, now)
        edge.decay_factor = factor
        edge.weight = max(self._floor, edge.weight * factor)
        return edge

    def apply_batch(self, edges: List[CLSTopologyEdge], current_time: Optional[float] = None) -> List[CLSTopologyEdge]:
        return [self.apply(e, current_time) for e in edges]

    @property
    def decay_count(self) -> int:
        return self._decay_count


class AssociativeRecallRewirer:
    """联想召回重连器 — 三层协同

    在联想召回时动态增强关联路径，重新布线以优
    化检索图结构。高频共检索的节点对自动形成权重增强边。
    """

    def __init__(self, boost_factor: float = 1.5, max_rewire_per_cycle: int = 50) -> None:
        self._boost_factor = boost_factor
        self._max_rewire = max_rewire_per_cycle
        self._lock = threading.RLock()
        self._rewire_count = 0
        self._co_access_count: Dict[FrozenSet[str], int] = defaultdict(int)

    def record_co_access(self, node_a: str, node_b: str) -> None:
        """记录一次共检索事件"""
        key = frozenset([node_a, node_b])
        if len(key) == 2:
            with self._lock:
                self._co_access_count[key] += 1

    def rewire(self, edges: Dict[str, CLSTopologyEdge], min_co_access: int = 3) -> List[CLSTopologyEdge]:
        """基于共检索频率重新增强边权重"""
        rewire_targets: List[CLSTopologyEdge] = []
        with self._lock:
            for key, count in self._co_access_count.items():
                if count < min_co_access or self._rewire_count >= self._max_rewire:
                    continue
                nodes = list(key)
                for edge in edges.values():
                    if {edge.source_id, edge.target_id} == set(nodes):
                        edge.weight = min(10.0, edge.weight * self._boost_factor)
                        rewire_targets.append(edge)
                        self._rewire_count += 1
                        break
        return rewire_targets

    @property
    def rewire_count(self) -> int:
        return self._rewire_count


class ConceptClusterDensityDetector:
    """概念簇密度检测器 — 前额叶层

    监控概念簇内部节点密度，当密度突破阈值时自动
    触发意图涌现信号，驱动主动记忆重组。
    """

    def __init__(self, density_threshold: float = 0.60, min_cluster_size: int = 5) -> None:
        self._threshold = density_threshold
        self._min_size = min_cluster_size
        self._lock = threading.RLock()
        self._clusters: Dict[str, ConceptCluster] = {}
        self._intent_count = 0

    def compute_density(self, cluster: ConceptCluster, all_edges: Dict[str, CLSTopologyEdge]) -> float:
        """计算概念簇内部密度 = 实际边数 / 最大可能边数"""
        node_list = list(cluster.node_ids)
        n = len(node_list)
        if n < 2:
            return 0.0
        max_edges = n * (n - 1) / 2
        actual_edges = 0
        node_set = cluster.node_ids
        for edge in all_edges.values():
            if edge.source_id in node_set and edge.target_id in node_set:
                actual_edges += 1
        return actual_edges / max_edges if max_edges > 0 else 0.0

    def detect(self, edges: Dict[str, CLSTopologyEdge]) -> List[IntentEmergence]:
        """检测所有概念簇的密度，超阈值时涌现意图"""
        intents: List[IntentEmergence] = []
        with self._lock:
            for cid, cluster in self._clusters.items():
                if len(cluster.node_ids) < self._min_size:
                    continue
                density = self.compute_density(cluster, edges)
                cluster.density = density
                if density >= self._threshold:
                    self._intent_count += 1
                    intent = IntentEmergence(
                        intent_id=f"intent_{cid}_{self._intent_count}",
                        cluster_id=cid,
                        signal_type=IntentSignal.DENSITY_SURGE,
                        description=f"Cluster {cid} density {density:.3f} exceeds threshold {self._threshold}",
                        triggered_nodes=list(cluster.node_ids),
                        confidence=density,
                    )
                    intents.append(intent)
        return intents

    def register_cluster(self, cluster: ConceptCluster) -> None:
        with self._lock:
            self._clusters[cluster.cluster_id] = cluster

    def remove_cluster(self, cluster_id: str) -> None:
        with self._lock:
            self._clusters.pop(cluster_id, None)

    @property
    def intent_count(self) -> int:
        return self._intent_count

    @property
    def active_clusters(self) -> int:
        return len(self._clusters)


class CLSActiveMemoryEngine:
    """脑启发三层 CLS 主动记忆引擎 — 顶层编排器

    组合海马(EventStreamAssembler)、新皮层(SimilarityMerger + StalenessDecayer)、
    前额叶(ConceptClusterDensityDetector + AssociativeRecallRewirer)三层，
    实现事件驱动的闭环绕记忆管理。
    """

    def __init__(
        self,
        assembler: Optional[EventStreamAssembler] = None,
        merger: Optional[SimilarityMerger] = None,
        decayer: Optional[StalenessDecayer] = None,
        rewirer: Optional[AssociativeRecallRewirer] = None,
        detector: Optional[ConceptClusterDensityDetector] = None,
    ) -> None:
        self.assembler = assembler or EventStreamAssembler()
        self.merger = merger or SimilarityMerger()
        self.decayer = decayer or StalenessDecayer()
        self.rewirer = rewirer or AssociativeRecallRewirer()
        self.detector = detector or ConceptClusterDensityDetector()
        self._lock = threading.RLock()
        self._nodes: Dict[str, TopologyNode] = {}
        self._edges: Dict[str, CLSTopologyEdge] = {}
        self._edge_counter = 0
        self._stats = CLSStats()

    def process_event(self, event: EventRecord) -> MemoryFragment:
        """处理单个事件：海马装配 → 新皮层合并+衰减 → 前额叶检测"""
        fragment = self.assembler.ingest(event)
        node_id = f"node_{fragment.fragment_id}"
        node = TopologyNode(
            node_id=node_id,
            content_hash=str(hash(fragment.fragment_id)),
            embedding=fragment.embedding,
            layer=BrainLayer.HIPPOCAMPUS,
        )
        with self._lock:
            self._nodes[node_id] = node
            self._stats.total_nodes += 1
            self._stats.total_events_processed += 1
        # 新皮层：相似合并检测
        with self._lock:
            for existing_id, existing_node in list(self._nodes.items()):
                if existing_id != node_id and self.merger.should_merge(node, existing_node):
                    self.merger.merge(node, existing_node)
                    self._stats.merges_performed += 1
                    break
        # 新皮层：陈旧衰减
        current_time = time.time()
        with self._lock:
            for edge in list(self._edges.values()):
                self.decayer.apply(edge, current_time)
                self._stats.decays_applied += 1
        # 前额叶：密度检测
        intents = self.detector.detect(self._edges)
        self._stats.intents_emerged += len(intents)
        self._stats.clusters_active = self.detector.active_clusters
        return fragment

    def add_edge(self, source_id: str, target_id: str, weight: float = 1.0) -> CLSTopologyEdge:
        with self._lock:
            self._edge_counter += 1
            edge = CLSTopologyEdge(
                edge_id=f"edge_{self._edge_counter}",
                source_id=source_id,
                target_id=target_id,
                weight=weight,
            )
            self._edges[edge.edge_id] = edge
            self._stats.total_edges += 1
        return edge

    def recall(self, node_id: str, depth: int = 3) -> List[TopologyNode]:
        """联想召回：从指定节点沿边遍历"""
        visited: Set[str] = set()
        result: List[TopologyNode] = []
        queue: deque[Tuple[str, int]] = deque([(node_id, 0)])
        while queue:
            current, d = queue.popleft()
            if current in visited or d > depth:
                continue
            visited.add(current)
            if current in self._nodes:
                result.append(self._nodes[current])
                # 记录共检索
                self.rewirer.record_co_access(node_id, current)
            for edge in self._edges.values():
                if edge.source_id == current and edge.target_id not in visited:
                    queue.append((edge.target_id, d + 1))
                elif edge.target_id == current and edge.source_id not in visited:
                    queue.append((edge.source_id, d + 1))
        # 重连
        rewire_results = self.rewirer.rewire(self._edges)
        self._stats.rewires_performed += len(rewire_results)
        return result

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        return {
            "layer": "CLS_Active_Memory",
            "hippocampus_events": self.assembler.total_processed,
            "neocortex_merges": self.merger.merge_count,
            "neocortex_decays": self.decayer.decay_count,
            "rewires": self.rewirer.rewire_count,
            "prefrontal_intents": self.detector.intent_count,
            "active_clusters": self.detector.active_clusters,
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "stats": self._stats.summary(),
        }


# ============================================================================
# Module-level statistics
# ============================================================================


def statistics() -> Dict[str, Any]:
    """模块级运行时指标"""
    return {
        "module": "cognitive_folding_memory",
        "class_count": 6,
        "brain_layers": 3,
        "supported_decay_models": [m.value for m in CLSDecayModel],
        "supported_merge_strategies": [s.value for s in MergeStrategy],
        "supported_intent_signals": [i.value for i in IntentSignal],
    }
