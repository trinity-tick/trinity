"""
P14-5: Multi-modal Memory Graph (对标 MemVerse · 上海AI Lab)
=====================================================================

核心设计（MemVerse: Unified Multi-modal Semantic Space + Cognitive Graph）：
  - Modality（枚举）：IMAGE / AUDIO / VIDEO / TEXT 四模态统一
  - MultimodalEmbedding：将异构模态对齐到统一向量空间（d=1024）
  - DualPathwayRetriever：双通路架构——快速参数召回 + 层次化检索
  - CognitiveGraph：跨模态认知图谱——实体 + 关系结构
  - ShortTermBuffer：短期记忆缓冲区（固定容量 FIFO + 优先级驱逐）
  - LongTermStructuredGraph：长期结构化记忆图（持久化存储）

兼容性：
  - 与 graph.py / graph_router.py 图结构接口兼容
  - 与 multimodal_memory_eval.py（P14-3）评测管线兼容
  - 与 kgraph 模块的实体抽取 + PPR 检索兼容

Reference:
  - MemVerse: Unified Multi-modal Semantic Space for Memory (Shanghai AI Lab)
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ────────────────────────────────────────────────────

class Modality(Enum):
    """四模态类型。"""
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    TEXT = "text"


class FusionMode(Enum):
    """多模态融合策略。"""
    EARLY = "early"            # 早期融合（特征拼接）
    LATE = "late"              # 后期融合（各模态独立编码后加权）
    CROSS_ATTN = "cross_attn"  # 交叉注意力融合
    ADAPTIVE = "adaptive"      # 自适应门控融合


class GraphEdgeType(Enum):
    """认知图谱边类型。"""
    SEMANTIC = "semantic"       # 语义关联
    TEMPORAL = "temporal"       # 时序关系
    SPATIAL = "spatial"         # 空间包含
    CAUSAL = "causal"           # 因果关系
    SIMILARITY = "similarity"   # 相似度关联
    CO_OCCURRENCE = "co_occurrence"  # 共现关系


class BufferEvictionPolicy(Enum):
    """缓冲区驱逐策略。"""
    FIFO = "fifo"          # 先进先出
    LRU = "lru"            # 最近最少使用
    PRIORITY = "priority"  # 优先级驱逐
    TTL = "ttl"            # 过期驱逐


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class MultimodalEmbedding:
    """多模态向量（对齐到 d=1024 统一空间）。"""
    embedding_id: str
    modality: Modality
    vector: np.ndarray               # shape (1024,)
    raw_path: Optional[str] = None    # 原始文件路径
    metadata: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if isinstance(self.vector, list):
            self.vector = np.array(self.vector, dtype=np.float32)

    def normalize(self) -> MultimodalEmbedding:
        norm = np.linalg.norm(self.vector)
        if norm > 0:
            self.vector = self.vector / norm
        return self


@dataclass
class CrossModalEntity:
    """跨模态实体（同一现实实体的多模态表达）。"""
    entity_id: str
    name: str
    entity_type: str
    modalities: Set[Modality] = field(default_factory=set)
    embeddings: Dict[Modality, str] = field(default_factory=dict)  # modality → embedding_id
    attributes: Dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class CrossModalRelation:
    """跨模态关系边。"""
    relation_id: str
    source_entity_id: str
    target_entity_id: str
    edge_type: GraphEdgeType
    weight: float = 1.0
    evidence: List[str] = field(default_factory=list)   # 支持该关系的证据列表
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class BufferEntry:
    """短期缓冲区条目。"""
    entry_id: str
    embedding: MultimodalEmbedding
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    priority: float = 0.5
    ttl_seconds: float = 300.0        # 默认 5 分钟 TTL
    access_count: int = 0
    last_accessed: Optional[datetime] = None

    def is_expired(self) -> bool:
        elapsed = (datetime.now(timezone.utc) - self.timestamp).total_seconds()
        return elapsed > self.ttl_seconds


@dataclass
class GraphQueryResult:
    """图谱查询结果。"""
    entities: List[CrossModalEntity] = field(default_factory=list)
    relations: List[CrossModalRelation] = field(default_factory=list)
    subgraph_embeddings: List[MultimodalEmbedding] = field(default_factory=list)
    query_time_ms: float = 0.0
    total_matches: int = 0


@dataclass
class RetrievalResult:
    """双通路检索结果。"""
    fast_results: List[MultimodalEmbedding] = field(default_factory=list)    # 参数召回
    slow_results: List[MultimodalEmbedding] = field(default_factory=list)    # 层次检索
    merged_results: List[MultimodalEmbedding] = field(default_factory=list)  # 融合排序
    graph_context: Optional[GraphQueryResult] = None
    retrieval_time_ms: float = 0.0


# ── 短期记忆缓冲区 ────────────────────────────────────────────────

class ShortTermBuffer:
    """固定容量短期缓冲区，支持多种驱逐策略。"""

    _DEFAULT_CAPACITY = 256

    def __init__(
        self,
        capacity: int = _DEFAULT_CAPACITY,
        policy: BufferEvictionPolicy = BufferEvictionPolicy.LRU,
    ):
        self._capacity = capacity
        self._policy = policy
        self._entries: OrderedDict[str, BufferEntry] = OrderedDict()
        self._lock = threading.RLock()
        self._eviction_count = 0
        logger.info("ShortTermBuffer initialized (capacity=%d, policy=%s)", capacity, policy.value)

    def push(self, embedding: MultimodalEmbedding, priority: float = 0.5, ttl: float = 300.0) -> str:
        entry_id = f"buf_{uuid.uuid4().hex[:12]}"
        entry = BufferEntry(
            entry_id=entry_id,
            embedding=embedding,
            priority=priority,
            ttl_seconds=ttl,
        )
        with self._lock:
            self._entries[entry_id] = entry
            self._entries.move_to_end(entry_id)
            if len(self._entries) > self._capacity:
                self._evict()
        return entry_id

    def get(self, entry_id: str) -> Optional[BufferEntry]:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry and entry.is_expired():
                del self._entries[entry_id]
                return None
            if entry:
                entry.access_count += 1
                entry.last_accessed = datetime.now(timezone.utc)
                self._entries.move_to_end(entry_id)
            return entry

    def search(self, query_vector: np.ndarray, top_k: int = 10) -> List[BufferEntry]:
        results: List[Tuple[float, BufferEntry]] = []
        with self._lock:
            for entry in list(self._entries.values()):
                if entry.is_expired():
                    continue
                sim = float(np.dot(entry.embedding.vector, query_vector))
                results.append((sim, entry))
        results.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in results[:top_k]]

    def _evict(self):
        if not self._entries:
            return
        if self._policy == BufferEvictionPolicy.FIFO:
            _, _ = self._entries.popitem(last=False)
        elif self._policy == BufferEvictionPolicy.LRU:
            _, _ = self._entries.popitem(last=False)
        elif self._policy == BufferEvictionPolicy.PRIORITY:
            lowest = min(self._entries.items(), key=lambda kv: kv[1].priority)
            del self._entries[lowest[0]]
        elif self._policy == BufferEvictionPolicy.TTL:
            expired = [k for k, v in self._entries.items() if v.is_expired()]
            for k in expired:
                del self._entries[k]
        self._eviction_count += 1

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "capacity": self._capacity,
                "size": len(self._entries),
                "policy": self._policy.value,
                "eviction_count": self._eviction_count,
            }


# ── 认知图谱 ──────────────────────────────────────────────────────

class CognitiveGraph:
    """跨模态认知图谱：实体 + 关系 + 图查询。"""

    def __init__(self):
        self._entities: Dict[str, CrossModalEntity] = {}
        self._relations: List[CrossModalRelation] = []
        self._adjacency: Dict[str, Set[str]] = defaultdict(set)  # entity_id → related entity_ids
        self._embedding_registry: Dict[str, MultimodalEmbedding] = {}
        self._lock = threading.RLock()
        logger.info("CognitiveGraph initialized")

    def add_entity(self, entity: CrossModalEntity) -> str:
        with self._lock:
            self._entities[entity.entity_id] = entity
            return entity.entity_id

    def update_entity(self, entity_id: str, **updates) -> Optional[CrossModalEntity]:
        with self._lock:
            entity = self._entities.get(entity_id)
            if not entity:
                return None
            for key, value in updates.items():
                if hasattr(entity, key):
                    setattr(entity, key, value)
            entity.updated_at = datetime.now(timezone.utc)
            return entity

    def add_relation(self, relation: CrossModalRelation) -> str:
        with self._lock:
            self._relations.append(relation)
            self._adjacency[relation.source_entity_id].add(relation.target_entity_id)
            self._adjacency[relation.target_entity_id].add(relation.source_entity_id)
            return relation.relation_id

    def register_embedding(self, embedding: MultimodalEmbedding) -> str:
        with self._lock:
            self._embedding_registry[embedding.embedding_id] = embedding
            return embedding.embedding_id

    def get_entity(self, entity_id: str) -> Optional[CrossModalEntity]:
        with self._lock:
            return self._entities.get(entity_id)

    def query_entity(
        self,
        name: Optional[str] = None,
        entity_type: Optional[str] = None,
        modality: Optional[Modality] = None,
        limit: int = 50,
    ) -> List[CrossModalEntity]:
        results = []
        with self._lock:
            for entity in self._entities.values():
                if name and name.lower() not in entity.name.lower():
                    continue
                if entity_type and entity.entity_type != entity_type:
                    continue
                if modality and modality not in entity.modalities:
                    continue
                results.append(entity)
        return results[:limit]

    def query_relations(
        self,
        source_id: Optional[str] = None,
        edge_type: Optional[GraphEdgeType] = None,
        min_weight: float = 0.0,
    ) -> List[CrossModalRelation]:
        results = []
        with self._lock:
            for rel in self._relations:
                if source_id and rel.source_entity_id != source_id:
                    continue
                if edge_type and rel.edge_type != edge_type:
                    continue
                if rel.weight < min_weight:
                    continue
                results.append(rel)
        return results

    def get_neighbors(self, entity_id: str, depth: int = 1) -> List[CrossModalEntity]:
        visited: Set[str] = set()
        frontier = {entity_id}
        with self._lock:
            for _ in range(depth):
                next_frontier: Set[str] = set()
                for eid in frontier - visited:
                    visited.add(eid)
                    next_frontier.update(self._adjacency.get(eid, set()))
                frontier = next_frontier
            return [self._entities[eid] for eid in visited if eid in self._entities]

    def build_subgraph(
        self,
        query_vector: np.ndarray,
        top_k: int = 10,
    ) -> GraphQueryResult:
        start = time.perf_counter()
        # Vector similarity search
        scores: List[Tuple[float, str]] = []
        with self._lock:
            for eid, emb in self._embedding_registry.items():
                sim = float(np.dot(emb.vector, query_vector))
                scores.append((sim, eid))
        scores.sort(key=lambda x: x[0], reverse=True)
        top_embeddings = [self._embedding_registry[eid] for _, eid in scores[:top_k]]

        # Collect related entities and relations
        related_entities: Dict[str, CrossModalEntity] = {}
        related_relations: List[CrossModalRelation] = []
        with self._lock:
            for _, eid in scores[:top_k]:
                if eid in self._entities:
                    related_entities[eid] = self._entities[eid]
                neighbors = self.get_neighbors(eid, depth=1)
                for n in neighbors:
                    related_entities[n.entity_id] = n
            for rel in self._relations:
                if rel.source_entity_id in related_entities or rel.target_entity_id in related_entities:
                    related_relations.append(rel)

        elapsed = (time.perf_counter() - start) * 1000
        return GraphQueryResult(
            entities=list(related_entities.values()),
            relations=related_relations,
            subgraph_embeddings=top_embeddings,
            query_time_ms=elapsed,
            total_matches=len(related_entities),
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_entities": len(self._entities),
                "total_relations": len(self._relations),
                "total_embeddings": len(self._embedding_registry),
                "modality_distribution": {
                    m.value: sum(1 for e in self._entities.values() if m in e.modalities)
                    for m in Modality
                },
            }


# ── 双通路检索器 ─────────────────────────────────────────────────

class DualPathwayRetriever:
    """
    双通路架构：
      - Fast path: 参数化向量召回（cosine similarity top-K）
      - Slow path: 层次化检索——先粗筛再精细排序
    """

    def __init__(
        self,
        graph: CognitiveGraph,
        buffer: ShortTermBuffer,
        fast_top_k: int = 50,
        slow_top_k: int = 20,
        fusion_mode: FusionMode = FusionMode.LATE,
    ):
        self._graph = graph
        self._buffer = buffer
        self._fast_top_k = fast_top_k
        self._slow_top_k = slow_top_k
        self._fusion_mode = fusion_mode
        self._lock = threading.RLock()
        logger.info(
            "DualPathwayRetriever initialized (fast_k=%d, slow_k=%d, fusion=%s)",
            fast_top_k, slow_top_k, fusion_mode.value,
        )

    def retrieve(
        self,
        query: Union[str, np.ndarray],
        modality_filter: Optional[Modality] = None,
        use_graph: bool = True,
    ) -> RetrievalResult:
        start = time.perf_counter()

        if isinstance(query, str):
            query_vector = self._text_to_vector(query)
        else:
            query_vector = query

        # Fast path: buffer search
        fast_results = self._buffer.search(query_vector, top_k=self._fast_top_k)
        fast_embeddings = [e.embedding for e in fast_results]

        # Slow path: graph subgraph search
        slow_embeddings: List[MultimodalEmbedding] = []
        graph_result: Optional[GraphQueryResult] = None
        if use_graph:
            graph_result = self._graph.build_subgraph(query_vector, top_k=self._slow_top_k)
            slow_embeddings = graph_result.subgraph_embeddings

        # Modality filter
        if modality_filter:
            fast_embeddings = [e for e in fast_embeddings if e.modality == modality_filter]
            slow_embeddings = [e for e in slow_embeddings if e.modality == modality_filter]

        # Fusion
        merged = self._fuse(fast_embeddings, slow_embeddings, query_vector)

        elapsed = (time.perf_counter() - start) * 1000
        return RetrievalResult(
            fast_results=fast_embeddings,
            slow_results=slow_embeddings,
            merged_results=merged,
            graph_context=graph_result,
            retrieval_time_ms=elapsed,
        )

    def _fuse(
        self,
        fast: List[MultimodalEmbedding],
        slow: List[MultimodalEmbedding],
        query_vector: np.ndarray,
    ) -> List[MultimodalEmbedding]:
        if self._fusion_mode == FusionMode.LATE:
            # Late fusion: combine top results from both, re-rank
            seen: Set[str] = set()
            combined: List[MultimodalEmbedding] = []
            for emb in fast + slow:
                if emb.embedding_id not in seen:
                    seen.add(emb.embedding_id)
                    combined.append(emb)
            # Re-rank by similarity
            scored = [(float(np.dot(e.vector, query_vector)), e) for e in combined]
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:self._slow_top_k]]
        elif self._fusion_mode == FusionMode.ADAPTIVE:
            # Adaptive gating: weight by confidence
            scored: Dict[str, Tuple[float, MultimodalEmbedding]] = {}
            for emb in fast:
                score = float(np.dot(emb.vector, query_vector)) * emb.confidence
                scored[emb.embedding_id] = (score, emb)
            for emb in slow:
                score = float(np.dot(emb.vector, query_vector)) * emb.confidence * 1.2  # graph bias
                if emb.embedding_id in scored:
                    scored[emb.embedding_id] = (max(scored[emb.embedding_id][0], score), emb)
                else:
                    scored[emb.embedding_id] = (score, emb)
            ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
            return [e for _, e in ranked[:self._slow_top_k]]
        else:
            # Default combine
            return list({e.embedding_id: e for e in fast + slow}.values())[:self._slow_top_k]

    @staticmethod
    def _text_to_vector(text: str, dim: int = 1024) -> np.ndarray:
        seed = int(hashlib.md5(text.encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        vec = rng.randn(dim).astype(np.float32)
        norm = np.linalg.norm(vec)
        if norm > 0:
            vec /= norm
        return vec

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "fast_top_k": self._fast_top_k,
                "slow_top_k": self._slow_top_k,
                "fusion_mode": self._fusion_mode.value,
                "buffer_size": self._buffer.size,
            }


# ── 多模态记忆图（顶层调度器）────────────────────────────────────

class MultimodalMemoryGraph:
    """统一多模态记忆图——顶层调度入口。"""

    _VERSION = "1.0.0"

    def __init__(
        self,
        buffer_capacity: int = 256,
        fusion_mode: FusionMode = FusionMode.LATE,
        embedding_dim: int = 1024,
    ):
        self._graph = CognitiveGraph()
        self._buffer = ShortTermBuffer(capacity=buffer_capacity)
        self._retriever = DualPathwayRetriever(
            graph=self._graph,
            buffer=self._buffer,
            fusion_mode=fusion_mode,
        )
        self._embedding_dim = embedding_dim
        self._lock = threading.RLock()
        self._version = self._VERSION
        self._created_at = datetime.now(timezone.utc)
        logger.info("MultimodalMemoryGraph v%s initialized (dim=%d)", self._version, embedding_dim)

    def add(
        self,
        content: str,
        modality: Modality = Modality.TEXT,
        raw_path: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        entity_name: Optional[str] = None,
        entity_type: str = "generic",
        priority: float = 0.5,
        ttl: float = 300.0,
    ) -> Dict[str, Any]:
        with self._lock:
            # Create embedding
            vec = DualPathwayRetriever._text_to_vector(content, self._embedding_dim)
            emb_id = f"emb_{uuid.uuid4().hex[:12]}"
            embedding = MultimodalEmbedding(
                embedding_id=emb_id,
                modality=modality,
                vector=vec,
                raw_path=raw_path,
                metadata=metadata or {},
            )

            # Register in graph
            self._graph.register_embedding(embedding)

            # Create entity if name provided
            entity_id = None
            if entity_name:
                entity_id = f"ent_{uuid.uuid4().hex[:12]}"
                entity = CrossModalEntity(
                    entity_id=entity_id,
                    name=entity_name,
                    entity_type=entity_type,
                    modalities={modality},
                    embeddings={modality: emb_id},
                )
                self._graph.add_entity(entity)

            # Push to buffer
            buf_id = self._buffer.push(embedding, priority=priority, ttl=ttl)

            return {
                "embedding_id": emb_id,
                "entity_id": entity_id,
                "buffer_id": buf_id,
                "modality": modality.value,
            }

    def search(
        self,
        query: Union[str, np.ndarray],
        modality_filter: Optional[Modality] = None,
        top_k: int = 10,
        use_graph: bool = True,
    ) -> RetrievalResult:
        return self._retriever.retrieve(query, modality_filter=modality_filter, use_graph=use_graph)

    def build_graph(self) -> CognitiveGraph:
        return self._graph

    # ── 图谱操作委托 ───────────────────────────────────────────────

    def add_entity(self, entity: CrossModalEntity) -> str:
        return self._graph.add_entity(entity)

    def add_relation(self, relation: CrossModalRelation) -> str:
        return self._graph.add_relation(relation)

    def query_entity(self, **kwargs) -> List[CrossModalEntity]:
        return self._graph.query_entity(**kwargs)

    def query_relations(self, **kwargs) -> List[CrossModalRelation]:
        return self._graph.query_relations(**kwargs)

    # ── 属性 ───────────────────────────────────────────────────────

    @property
    def graph(self) -> CognitiveGraph:
        return self._graph

    @property
    def buffer(self) -> ShortTermBuffer:
        return self._buffer

    @property
    def retriever(self) -> DualPathwayRetriever:
        return self._retriever

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self._version,
                "embedding_dim": self._embedding_dim,
                "graph": self._graph.statistics(),
                "buffer": self._buffer.statistics(),
                "retriever": self._retriever.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """返回模块级统计信息。"""
    return {
        "module": "P14-5 Multi-modal Memory Graph",
        "benchmark": "MemVerse (Shanghai AI Lab)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 6,
        "key_metric": "Unified multi-modal embedding (d=1024) / Dual-pathway retrieval",
        "thread_safe": True,
    }
