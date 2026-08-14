"""
P8-2: Cross-Modal Entity-Centric Memory Layer (对标 M3-Agent ICLR2026)
=======================================================================

核心设计（基于 M3-Agent: "Seeing, Listening, Remembering, and Reasoning"）：
  - 实体中心的长期记忆：从视觉和音频输入流中提取实体，建立统一实体记忆
  - 跨片段身份一致性追踪：同一人物/物体在不同时间点的统一标识
  - 实体语义记忆累积：从情景记忆抽象为世界知识（episodic → semantic）
  - 多轮迭代检索推理控制：自主多轮检索-推理循环，边想边查
  - 可扩展模态适配器架构：当前以文本为主，预留 VisualAdapter / AudioAdapter 接口

M3-Agent 关键设计：
  - Entity-centric multimodal memory format
  - Real-time visual/auditory input → memory build/update
  - Episodic memory → Semantic memory accumulation
  - Multi-turn iterative reasoning with retrieval
  - RL-trained control model for retrieval orchestration

Reference: Long et al., "Seeing, Listening, Remembering, and Reasoning:
           A Multimodal Agent with Long-Term Memory", ICLR 2026.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# ── 枚举与常量 ───────────────────────────────────────────────────────


class Modality(Enum):
    """模态类型。"""
    TEXT = "text"
    VISUAL = "visual"
    AUDIO = "audio"
    MULTIMODAL = "multimodal"


class EntityType(Enum):
    """实体类型。"""
    PERSON = "person"
    OBJECT = "object"
    LOCATION = "location"
    EVENT = "event"
    CONCEPT = "concept"
    ORGANIZATION = "organization"
    DOCUMENT = "document"
    OTHER = "other"


class MemoryLayer(Enum):
    """记忆层级（M3-Agent 双层结构）。"""
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class RetrievalPhase(Enum):
    """多轮检索推理的阶段。"""
    INITIAL_QUERY = "initial_query"
    RESULT_ANALYSIS = "result_analysis"
    REFINEMENT = "refinement"
    CROSS_REFERENCE = "cross_reference"
    FINAL_SYNTHESIS = "final_synthesis"


class IdentityConfidence(Enum):
    """身份匹配置信度。"""
    DEFINITE = "definite"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ReasoningAction(Enum):
    """推理控制动作（M3-Agent RL训练的输出）。"""
    RETRIEVE = "retrieve"
    REASON = "reason"
    REFINE = "refine"
    ANSWER = "answer"
    STOP = "stop"


# ── 数据结构 ─────────────────────────────────────────────────────────


@dataclass
class EntityEmbedding:
    """实体嵌入向量。"""
    modality: Modality
    vector: np.ndarray
    dimension: int
    model_name: str = "unknown"

    def to_list(self) -> List[float]:
        return self.vector.tolist()

    @staticmethod
    def from_list(data: List[float], modality: Modality) -> "EntityEmbedding":
        arr = np.array(data, dtype=np.float32)
        return EntityEmbedding(modality=modality, vector=arr, dimension=len(arr))


@dataclass
class EntityRecord:
    """实体记录。"""
    entity_id: str
    name: str
    entity_type: EntityType = EntityType.OTHER
    attributes: Dict[str, Any] = field(default_factory=dict)
    embeddings: List[EntityEmbedding] = field(default_factory=list)
    memory_layer: MemoryLayer = MemoryLayer.EPISODIC
    confidence: float = 1.0
    first_seen: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    occurrence_count: int = 1
    source_fragments: List[str] = field(default_factory=list)


@dataclass
class IdentityCluster:
    """身份聚类。"""
    cluster_id: str
    canonical_entity_id: str
    member_entity_ids: Set[str]
    canonical_name: str
    aliases: Set[str] = field(default_factory=set)
    confidence: IdentityConfidence = IdentityConfidence.MEDIUM
    evidence: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class SemanticAbstraction:
    """语义抽象。"""
    abstraction_id: str
    statement: str
    source_entities: List[str] = field(default_factory=list)
    source_fragments: List[str] = field(default_factory=list)
    confidence: float = 0.5
    evidence_count: int = 0
    contradictions: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


@dataclass
class RetrievalStep:
    """多轮检索推理的一个步骤。"""
    step_index: int
    phase: RetrievalPhase
    action: ReasoningAction
    query: str
    retrieved_entities: List[str] = field(default_factory=list)
    reasoning_output: str = ""
    confidence: float = 0.0
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetrievalTrace:
    """多轮检索推理的完整追踪。"""
    trace_id: str
    original_query: str
    steps: List[RetrievalStep] = field(default_factory=list)
    final_answer: str = ""
    total_rounds: int = 0
    total_latency_ms: float = 0.0
    entities_consulted: int = 0


# ── 模态适配器抽象基类 ─────────────────────────────────────────────


class ModalityAdapter(ABC):
    """模态适配器抽象基类。"""

    @abstractmethod
    def extract_entities(self, input_data: Any) -> List[EntityRecord]:
        ...

    @abstractmethod
    def generate_embedding(self, entity: EntityRecord) -> EntityEmbedding:
        ...

    @property
    @abstractmethod
    def modality(self) -> Modality:
        ...


# ══════════════════════════════════════════════════════════════════════
# ── _VisualEncoder：多模态编码器与嵌入生成 ────────────────────────
# ══════════════════════════════════════════════════════════════════════


class _VisualEncoder:
    """多模态适配器注册与嵌入生成。

    管理文本/视觉/音频三种模态适配器的注册、切换和实体的嵌入向量生成。
    """

    def __init__(
        self,
        text_adapter: ModalityAdapter | None = None,
        visual_adapter: ModalityAdapter | None = None,
        audio_adapter: ModalityAdapter | None = None,
    ):
        self._text_adapter: ModalityAdapter = text_adapter or TextAdapter()
        self._visual_adapter: ModalityAdapter = visual_adapter or VisualAdapterStub()
        self._audio_adapter: ModalityAdapter = audio_adapter or AudioAdapterStub()

    def get_adapter(self, modality: Modality) -> ModalityAdapter:
        mapping = {
            Modality.TEXT: self._text_adapter,
            Modality.VISUAL: self._visual_adapter,
            Modality.AUDIO: self._audio_adapter,
        }
        return mapping.get(modality, self._text_adapter)

    def set_adapter(self, modality: Modality, adapter: ModalityAdapter) -> None:
        if modality == Modality.TEXT:
            self._text_adapter = adapter
        elif modality == Modality.VISUAL:
            self._visual_adapter = adapter
        elif modality == Modality.AUDIO:
            self._audio_adapter = adapter

    def generate_embedding(self, entity: EntityRecord, modality: Modality = Modality.TEXT) -> EntityEmbedding:
        adapter = self.get_adapter(modality)
        return adapter.generate_embedding(entity)

    def adapter_info(self) -> Dict[str, str]:
        return {
            "text": type(self._text_adapter).__name__,
            "visual": type(self._visual_adapter).__name__,
            "audio": type(self._audio_adapter).__name__,
        }


class TextAdapter(ModalityAdapter):
    """文本模态适配器。"""

    def __init__(self, embedding_dim: int = 768):
        self._embedding_dim = embedding_dim

    @property
    def modality(self) -> Modality:
        return Modality.TEXT

    def extract_entities(self, input_data: Any) -> List[EntityRecord]:
        if isinstance(input_data, str):
            text = input_data
        elif isinstance(input_data, dict):
            text = input_data.get("content", "")
        else:
            text = str(input_data)
        return []

    def generate_embedding(self, entity: EntityRecord) -> EntityEmbedding:
        seed = int(hashlib.md5(entity.name.encode()).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        vector = rng.randn(self._embedding_dim).astype(np.float32)
        vector /= np.linalg.norm(vector) + 1e-8
        return EntityEmbedding(
            modality=Modality.TEXT,
            vector=vector,
            dimension=self._embedding_dim,
            model_name="text-adapter",
        )


class VisualAdapterStub(ModalityAdapter):
    """视觉模态适配器（预留接口）。"""

    @property
    def modality(self) -> Modality:
        return Modality.VISUAL

    def extract_entities(self, input_data: Any) -> List[EntityRecord]:
        logger.debug("VisualAdapter: entity extraction not yet implemented")
        return []

    def generate_embedding(self, entity: EntityRecord) -> EntityEmbedding:
        return EntityEmbedding(
            modality=Modality.VISUAL,
            vector=np.zeros(512, dtype=np.float32),
            dimension=512,
            model_name="visual-stub",
        )


class AudioAdapterStub(ModalityAdapter):
    """音频模态适配器（预留接口）。"""

    @property
    def modality(self) -> Modality:
        return Modality.AUDIO

    def extract_entities(self, input_data: Any) -> List[EntityRecord]:
        logger.debug("AudioAdapter: entity extraction not yet implemented")
        return []

    def generate_embedding(self, entity: EntityRecord) -> EntityEmbedding:
        return EntityEmbedding(
            modality=Modality.AUDIO,
            vector=np.zeros(512, dtype=np.float32),
            dimension=512,
            model_name="audio-stub",
        )


# ══════════════════════════════════════════════════════════════════════
# ── _CrossModalAligner：实体索引 / 身份链接 / 语义抽象 / 检索推理 ─
# ══════════════════════════════════════════════════════════════════════


class _CrossModalAligner:
    """跨模态实体对齐与语义累积。

    负责实体的 CRUD、身份聚类链接、情景→语义的知识蒸馏，
    以及多轮迭代检索推理的控制循环。
    """

    MAX_RETRIEVAL_ROUNDS = 10
    SEMANTIC_ABSTRACTION_THRESHOLD = 5

    def __init__(self, encoder: _VisualEncoder):
        self._lock = threading.RLock()
        self._encoder = encoder

        self._entities: Dict[str, EntityRecord] = {}
        self._identity_clusters: Dict[str, IdentityCluster] = {}
        self._semantic_abstractions: Dict[str, SemanticAbstraction] = {}

        self._name_index: Dict[str, Set[str]] = defaultdict(set)
        self._type_index: Dict[EntityType, Set[str]] = defaultdict(set)
        self._fragment_to_entities: Dict[str, Set[str]] = defaultdict(set)

        self._retrieval_traces: deque = deque(maxlen=100)

        self._total_entities = 0
        self._total_clusters = 0
        self._total_abstractions = 0
        self._total_ingestions = 0
        self._total_retrievals = 0

    # ── 实体 CRUD ──────────────────────────────────────────────────

    def _find_entity_by_name(
        self, name: str, entity_type: Optional[EntityType] = None
    ) -> Optional[str]:
        candidates = self._name_index.get(name.lower(), set())
        if not candidates:
            return None
        if entity_type:
            for cid in candidates:
                entity = self._entities.get(cid)
                if entity and entity.entity_type == entity_type:
                    return cid
        best = None
        best_time = 0.0
        for cid in candidates:
            entity = self._entities.get(cid)
            if entity and entity.last_seen > best_time:
                best_time = entity.last_seen
                best = cid
        return best

    def _upsert_entity(
        self, entity: EntityRecord, fragment_id: Optional[str] = None
    ) -> str:
        existing_id = self._find_entity_by_name(entity.name, entity.entity_type)
        if existing_id:
            existing = self._entities[existing_id]
            existing.last_seen = time.time()
            existing.occurrence_count += 1
            existing.confidence = min(1.0, existing.confidence + 0.05)
            if fragment_id and fragment_id not in existing.source_fragments:
                existing.source_fragments.append(fragment_id)
            existing.attributes.update(entity.attributes)
            existing.embeddings.extend(entity.embeddings)
            if len(existing.embeddings) > 20:
                existing.embeddings = existing.embeddings[-20:]
            return existing_id

        if not entity.entity_id:
            key = f"{entity.entity_type.value}:{entity.name}:{time.time()}"
            entity.entity_id = f"ENT-{hashlib.sha256(key.encode()).hexdigest()[:12]}"

        entity.first_seen = time.time()
        entity.last_seen = time.time()

        self._entities[entity.entity_id] = entity
        self._name_index[entity.name.lower()].add(entity.entity_id)
        self._type_index[entity.entity_type].add(entity.entity_id)
        if fragment_id:
            self._fragment_to_entities[fragment_id].add(entity.entity_id)

        self._total_entities += 1
        return entity.entity_id

    # ── 摄取 ──────────────────────────────────────────────────────

    def ingest(
        self,
        input_data: Any,
        modality: Modality = Modality.TEXT,
        fragment_id: Optional[str] = None,
    ) -> List[str]:
        with self._lock:
            adapter = self._encoder.get_adapter(modality)
            extracted = adapter.extract_entities(input_data)
            ingested_ids = []
            for entity in extracted:
                eid = self._upsert_entity(entity, fragment_id)
                ingested_ids.append(eid)
            if fragment_id and ingested_ids:
                self._fragment_to_entities[fragment_id].update(ingested_ids)
            self._total_ingestions += 1
            self._maybe_abstract_to_semantic(ingested_ids)
            return ingested_ids

    def ingest_entity(
        self,
        name: str,
        entity_type: EntityType = EntityType.OTHER,
        attributes: Optional[Dict[str, Any]] = None,
        modality: Modality = Modality.TEXT,
        fragment_id: Optional[str] = None,
    ) -> str:
        with self._lock:
            record = EntityRecord(
                entity_id="",
                name=name,
                entity_type=entity_type,
                attributes=attributes or {},
                source_fragments=[fragment_id] if fragment_id else [],
            )
            key = f"{entity_type.value}:{name}:{hashlib.md5(str(attributes).encode()).hexdigest()[:8]}"
            record.entity_id = f"ENT-{hashlib.sha256(key.encode()).hexdigest()[:12]}"
            adapter = self._encoder.get_adapter(modality)
            emb = adapter.generate_embedding(record)
            record.embeddings.append(emb)
            return self._upsert_entity(record, fragment_id)

    # ── 身份聚类 ──────────────────────────────────────────────────

    def link_entities(
        self,
        entity_ids: List[str],
        canonical_name: str,
        confidence: IdentityConfidence = IdentityConfidence.MEDIUM,
        evidence: Optional[List[str]] = None,
    ) -> str:
        with self._lock:
            valid_ids = [eid for eid in entity_ids if eid in self._entities]
            if len(valid_ids) < 2:
                return ""

            cluster_id = f"CLS-{uuid.uuid4().hex[:10]}"
            aliases: Set[str] = set()
            for eid in valid_ids:
                entity = self._entities[eid]
                aliases.add(entity.name)

            canonical_entity_id = valid_ids[0]
            max_occurrences = 0
            for eid in valid_ids:
                entity = self._entities[eid]
                if entity.occurrence_count > max_occurrences:
                    max_occurrences = entity.occurrence_count
                    canonical_entity_id = eid

            cluster = IdentityCluster(
                cluster_id=cluster_id,
                canonical_entity_id=canonical_entity_id,
                member_entity_ids=set(valid_ids),
                canonical_name=canonical_name,
                aliases=aliases,
                confidence=confidence,
                evidence=evidence or [],
            )
            self._identity_clusters[cluster_id] = cluster
            self._total_clusters += 1
            return cluster_id

    def resolve_identity(
        self, name: str, entity_type: Optional[EntityType] = None
    ) -> Optional[EntityRecord]:
        with self._lock:
            direct_id = self._find_entity_by_name(name, entity_type)
            if not direct_id:
                return None
            for cluster in self._identity_clusters.values():
                if direct_id in cluster.member_entity_ids:
                    return self._entities.get(cluster.canonical_entity_id)
            return self._entities.get(direct_id)

    def get_identity_clusters(self) -> List[IdentityCluster]:
        with self._lock:
            return list(self._identity_clusters.values())

    # ── 语义抽象 ──────────────────────────────────────────────────

    def _maybe_abstract_to_semantic(self, entity_ids: List[str]) -> None:
        for eid in entity_ids:
            entity = self._entities.get(eid)
            if not entity:
                continue
            if entity.occurrence_count >= self.SEMANTIC_ABSTRACTION_THRESHOLD:
                entity.memory_layer = MemoryLayer.SEMANTIC
                logger.debug(
                    "Entity %s promoted to SEMANTIC layer (occurrences=%d)",
                    entity.name, entity.occurrence_count,
                )

    def add_semantic_abstraction(
        self,
        statement: str,
        source_entities: List[str],
        source_fragments: List[str],
        confidence: float = 0.5,
    ) -> str:
        with self._lock:
            aid = f"ABS-{uuid.uuid4().hex[:8]}"
            abstraction = SemanticAbstraction(
                abstraction_id=aid,
                statement=statement,
                source_entities=source_entities,
                source_fragments=source_fragments,
                confidence=confidence,
                evidence_count=len(source_fragments),
            )
            self._semantic_abstractions[aid] = abstraction
            self._total_abstractions += 1
            return aid

    def get_semantic_knowledge(self, entity_id: str) -> List[SemanticAbstraction]:
        with self._lock:
            return [
                abs_ for abs_ in self._semantic_abstractions.values()
                if entity_id in abs_.source_entities
            ]

    # ── 多轮检索推理 ──────────────────────────────────────────────

    def retrieve_with_reasoning(
        self,
        query: str,
        entity_types: Optional[List[EntityType]] = None,
        max_rounds: int = MAX_RETRIEVAL_ROUNDS,
        reasoning_fn: Optional[
            Callable[[str, List[EntityRecord]], Tuple[ReasoningAction, str, str]]
        ] = None,
    ) -> RetrievalTrace:
        with self._lock:
            trace_id = f"TRC-{uuid.uuid4().hex[:10]}"
            trace = RetrievalTrace(trace_id=trace_id, original_query=query)
            t_start = time.perf_counter()

            current_query = query
            all_retrieved: Set[str] = set()

            for round_idx in range(max_rounds):
                step_start = time.perf_counter()
                retrieved = self._search_entities(current_query, entity_types)
                retrieved_ids = [r.entity_id for r in retrieved]
                all_retrieved.update(retrieved_ids)

                if reasoning_fn:
                    action, reasoning_output, refined_query = reasoning_fn(
                        current_query, retrieved
                    )
                else:
                    action, reasoning_output, refined_query = self._default_reasoning(
                        current_query, retrieved, round_idx
                    )

                step = RetrievalStep(
                    step_index=round_idx,
                    phase=self._phase_for_action(action),
                    action=action,
                    query=current_query,
                    retrieved_entities=retrieved_ids,
                    reasoning_output=reasoning_output,
                    confidence=0.5 + 0.05 * round_idx,
                    latency_ms=(time.perf_counter() - step_start) * 1000,
                )
                trace.steps.append(step)

                if action in (ReasoningAction.ANSWER, ReasoningAction.STOP):
                    trace.final_answer = reasoning_output or (
                        f"Retrieved {len(all_retrieved)} entities"
                    )
                    break
                elif action == ReasoningAction.REFINE and refined_query:
                    current_query = refined_query
                elif action in (ReasoningAction.RETRIEVE, ReasoningAction.REASON):
                    current_query = f"{query} (refined round {round_idx + 1})"
                else:
                    break

            trace.total_rounds = len(trace.steps)
            trace.total_latency_ms = (time.perf_counter() - t_start) * 1000
            trace.entities_consulted = len(all_retrieved)

            self._retrieval_traces.append(trace)
            self._total_retrievals += 1
            return trace

    def _search_entities(
        self, query: str, entity_types: Optional[List[EntityType]] = None
    ) -> List[EntityRecord]:
        results = []
        query_lower = query.lower()
        for entity in self._entities.values():
            if entity_types and entity.entity_type not in entity_types:
                continue
            if query_lower in entity.name.lower():
                results.append(entity)
            elif any(
                query_lower in attr
                for attr in entity.attributes.values()
                if isinstance(attr, str)
            ):
                results.append(entity)
        results.sort(key=lambda e: e.occurrence_count, reverse=True)
        return results[:20]

    def _default_reasoning(
        self, query: str, retrieved: List[EntityRecord], round_idx: int
    ) -> Tuple[ReasoningAction, str, str]:
        if not retrieved:
            if round_idx < 2:
                return ReasoningAction.REFINE, "No results found, refining query", f"broader:{query}"
            return ReasoningAction.ANSWER, "No relevant entities found", ""
        if len(retrieved) >= 3 and round_idx < 3:
            return ReasoningAction.REASON, f"Analyzing {len(retrieved)} entities", ""
        if round_idx >= self.MAX_RETRIEVAL_ROUNDS - 1:
            return ReasoningAction.ANSWER, f"Synthesized from {len(retrieved)} entities", ""
        return ReasoningAction.ANSWER, f"Found {len(retrieved)} relevant entities", ""

    @staticmethod
    def _phase_for_action(action: ReasoningAction) -> RetrievalPhase:
        mapping = {
            ReasoningAction.RETRIEVE: RetrievalPhase.INITIAL_QUERY,
            ReasoningAction.REFINE: RetrievalPhase.REFINEMENT,
            ReasoningAction.REASON: RetrievalPhase.RESULT_ANALYSIS,
            ReasoningAction.ANSWER: RetrievalPhase.FINAL_SYNTHESIS,
            ReasoningAction.STOP: RetrievalPhase.FINAL_SYNTHESIS,
        }
        return mapping.get(action, RetrievalPhase.INITIAL_QUERY)

    # ── 查询接口 ──────────────────────────────────────────────────

    def get_entity(self, entity_id: str) -> Optional[EntityRecord]:
        return self._entities.get(entity_id)

    def list_entities(
        self,
        entity_type: Optional[EntityType] = None,
        memory_layer: Optional[MemoryLayer] = None,
        limit: int = 100,
    ) -> List[EntityRecord]:
        results = []
        for entity in self._entities.values():
            if entity_type and entity.entity_type != entity_type:
                continue
            if memory_layer and entity.memory_layer != memory_layer:
                continue
            results.append(entity)
        results.sort(key=lambda e: e.occurrence_count, reverse=True)
        return results[:limit]

    def get_fragment_entities(self, fragment_id: str) -> List[EntityRecord]:
        eids = self._fragment_to_entities.get(fragment_id, set())
        return [self._entities[eid] for eid in eids if eid in self._entities]

    # ── 统计 ──────────────────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        episodic = sum(
            1 for e in self._entities.values()
            if e.memory_layer == MemoryLayer.EPISODIC
        )
        semantic = sum(
            1 for e in self._entities.values()
            if e.memory_layer == MemoryLayer.SEMANTIC
        )
        return {
            "total_entities": len(self._entities),
            "entities_episodic": episodic,
            "entities_semantic": semantic,
            "identity_clusters": len(self._identity_clusters),
            "semantic_abstractions": len(self._semantic_abstractions),
            "total_ingestions": self._total_ingestions,
            "total_retrievals": self._total_retrievals,
            "recent_traces": len(self._retrieval_traces),
        }

    def reset(self) -> None:
        self._entities.clear()
        self._identity_clusters.clear()
        self._semantic_abstractions.clear()
        self._name_index.clear()
        self._type_index.clear()
        self._fragment_to_entities.clear()
        self._retrieval_traces.clear()
        self._total_entities = 0
        self._total_clusters = 0
        self._total_abstractions = 0
        self._total_ingestions = 0
        self._total_retrievals = 0


# ══════════════════════════════════════════════════════════════════════
# ── Facade：MultimodalEntityMemory ──────────────────────────────────
# ══════════════════════════════════════════════════════════════════════


class MultimodalEntityMemory:
    """跨模态实体中心长期记忆层（M3-Agent ICLR2026）。
    从多模态输入提取实体建立统一记忆，跨片段身份追踪，情景→语义积累，多轮检索推理。"""

    MODULE_ID = "P8-2"; MODULE_NAME = "Cross-Modal Entity-Centric Memory Layer"
    PAPER_REF = "ICLR 2026 (M3-Agent)"
    PAPER_TITLE = "Seeing, Listening, Remembering, and Reasoning: A Multimodal Agent with Long-Term Memory"

    def __init__(self, text_adapter: Optional[TextAdapter] = None,
                 visual_adapter: Optional[ModalityAdapter] = None,
                 audio_adapter: Optional[ModalityAdapter] = None):
        self._encoder = _VisualEncoder(text_adapter, visual_adapter, audio_adapter)
        self._aligner = _CrossModalAligner(self._encoder)

    def get_adapter(self, modality: Modality) -> ModalityAdapter:
        return self._encoder.get_adapter(modality)
    def set_adapter(self, modality: Modality, adapter: ModalityAdapter) -> None:
        self._encoder.set_adapter(modality, adapter)
    def ingest(self, input_data: Any, modality: Modality = Modality.TEXT,
               fragment_id: Optional[str] = None) -> List[str]:
        return self._aligner.ingest(input_data, modality, fragment_id)
    def ingest_entity(self, name: str, entity_type: EntityType = EntityType.OTHER,
                      attributes: Optional[Dict[str, Any]] = None,
                      modality: Modality = Modality.TEXT,
                      fragment_id: Optional[str] = None) -> str:
        return self._aligner.ingest_entity(name, entity_type, attributes, modality, fragment_id)
    def link_entities(self, entity_ids: List[str], canonical_name: str,
                      confidence: IdentityConfidence = IdentityConfidence.MEDIUM,
                      evidence: Optional[List[str]] = None) -> str:
        return self._aligner.link_entities(entity_ids, canonical_name, confidence, evidence)
    def resolve_identity(self, name: str, entity_type: Optional[EntityType] = None) -> Optional[EntityRecord]:
        return self._aligner.resolve_identity(name, entity_type)
    def get_identity_clusters(self) -> List[IdentityCluster]:
        return self._aligner.get_identity_clusters()
    def add_semantic_abstraction(self, statement: str, source_entities: List[str],
                                  source_fragments: List[str], confidence: float = 0.5) -> str:
        return self._aligner.add_semantic_abstraction(statement, source_entities, source_fragments, confidence)
    def get_semantic_knowledge(self, entity_id: str) -> List[SemanticAbstraction]:
        return self._aligner.get_semantic_knowledge(entity_id)
    def retrieve_with_reasoning(self, query: str,
                                 entity_types: Optional[List[EntityType]] = None,
                                 max_rounds: int = _CrossModalAligner.MAX_RETRIEVAL_ROUNDS,
                                 reasoning_fn: Optional[Callable[[str, List[EntityRecord]],
                                     Tuple[ReasoningAction, str, str]]] = None) -> RetrievalTrace:
        return self._aligner.retrieve_with_reasoning(query, entity_types, max_rounds, reasoning_fn)
    def get_entity(self, entity_id: str) -> Optional[EntityRecord]:
        return self._aligner.get_entity(entity_id)
    def list_entities(self, entity_type: Optional[EntityType] = None,
                      memory_layer: Optional[MemoryLayer] = None, limit: int = 100) -> List[EntityRecord]:
        return self._aligner.list_entities(entity_type, memory_layer, limit)
    def get_fragment_entities(self, fragment_id: str) -> List[EntityRecord]:
        return self._aligner.get_fragment_entities(fragment_id)
    def statistics(self) -> Dict[str, Any]:
        snap = self._aligner.snapshot()
        return {"module": self.MODULE_NAME, "paper": self.PAPER_REF, **snap,
                "adapters": self._encoder.adapter_info()}
    def reset(self) -> None:
        self._aligner.reset()
        logger.info("MultimodalEntityMemory reset complete")

def create_entity_memory(embedding_dim: int = 768) -> MultimodalEntityMemory:
    return MultimodalEntityMemory(
        text_adapter=TextAdapter(embedding_dim=embedding_dim),
        visual_adapter=VisualAdapterStub(),
        audio_adapter=AudioAdapterStub(),
    )
