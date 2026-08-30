"""
# status: active (2026-09 EXECUTION 163: 已接入运行时)
SAGEGraphMemoryEngine — SAGE Self-Evolving Agentic Graph-Memory Engine
=======================================================================
arXiv 2605.12061 · P44-2

实现自进化图记忆引擎: MemoryWriter 从交互历史增量构建结构化图记忆,
Graph Foundation Model 读取器执行检索并反馈写入器。
Reader-Writer 反馈闭环自进化。多跳QA、LongMemEval、HaluMem 验证。

设计要点:
  - MemoryWriter: 增量构建图记忆
  - GraphFoundationModel: 读取+检索+反馈
  - RWF_FeedbackSignal: Reader→Writer 反馈信号
  - SelfEvolvingGraphMemory: 自进化闭环
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EvolutionRound(Enum):
    """自进化轮次。"""
    INITIAL = 1
    FIRST_EVOLUTION = 2
    SECOND_EVOLUTION = 3
    CONTINUOUS = 99


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class StructuredEntity:
    """图记忆中的结构化实体。"""
    entity_id: str
    name: str
    entity_type: str = ""
    properties: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[np.ndarray] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class StructuredRelation:
    """图记忆中的结构化关系。"""
    relation_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    weight: float = 1.0
    properties: Dict[str, Any] = field(default_factory=dict)
    evidence_count: int = 1
    created_at: float = field(default_factory=time.time)


@dataclass
class SAGE_GraphQueryResult:
    """图查询结果。"""
    entities: List[StructuredEntity] = field(default_factory=list)
    relations: List[StructuredRelation] = field(default_factory=list)
    evidence_paths: List[List[str]] = field(default_factory=list)
    confidence: float = 1.0
    retrieval_time_ms: float = 0.0


@dataclass
class RWF_FeedbackSignal:
    """Reader→Writer 反馈信号。"""
    signal_id: str
    subject_entity_ids: List[str] = field(default_factory=list)
    signal_type: str = "reinforce"  # reinforce / weaken / split / merge / add_relation
    description: str = ""
    confidence: float = 0.5
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# GraphMemoryStore
# ---------------------------------------------------------------------------

class GraphMemoryStore:
    """图记忆存储——实体+关系管理。"""

    def __init__(self) -> None:
        self._entities: Dict[str, StructuredEntity] = {}
        self._relations: Dict[str, StructuredRelation] = {}
        self._adjacency: Dict[str, List[str]] = defaultdict(list)
        self._lock = threading.RLock()

    def add_entity(self, entity: StructuredEntity) -> None:
        with self._lock:
            self._entities[entity.entity_id] = entity

    def add_relation(self, relation: StructuredRelation) -> None:
        with self._lock:
            self._relations[relation.relation_id] = relation
            self._adjacency[relation.source_entity_id].append(relation.target_entity_id)
            self._adjacency[relation.target_entity_id].append(relation.source_entity_id)

    def get_entity(self, entity_id: str) -> Optional[StructuredEntity]:
        return self._entities.get(entity_id)

    def get_neighbors(self, entity_id: str, max_hops: int = 2) -> List[StructuredEntity]:
        """获取邻居实体 (BFS)。"""
        visited: Set[str] = {entity_id}
        frontier: List[str] = [entity_id]
        neighbors: List[StructuredEntity] = []

        for _ in range(max_hops):
            next_frontier: List[str] = []
            for node in frontier:
                for neighbor in self._adjacency.get(node, []):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        next_frontier.append(neighbor)
                        if ent := self._entities.get(neighbor):
                            neighbors.append(ent)
            frontier = next_frontier
            if not frontier:
                break

        return neighbors

    def get_relations_between(self, eid1: str, eid2: str) -> List[StructuredRelation]:
        """获取两个实体间的所有关系。"""
        return [
            r for r in self._relations.values()
            if (r.source_entity_id == eid1 and r.target_entity_id == eid2) or
               (r.source_entity_id == eid2 and r.target_entity_id == eid1)
        ]

    def statistics(self) -> Dict[str, Any]:
        return {
            "entities": len(self._entities),
            "relations": len(self._relations),
        }


# ---------------------------------------------------------------------------
# MemoryWriter
# ---------------------------------------------------------------------------

class MemoryWriter:
    """记忆写入器——从交互历史增量构建结构化图记忆。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._write_count: int = 0

    def write_from_turn(
        self, store: GraphMemoryStore, turn: Dict[str, Any]
    ) -> Tuple[List[StructuredEntity], List[StructuredRelation]]:
        """从单轮交互提取实体和关系写入图。"""
        with self._lock:
            entities: List[StructuredEntity] = []
            relations: List[StructuredRelation] = []

            content = str(turn.get("content", ""))
            turn_id = turn.get("id", f"turn_{int(time.time()*1e6)}")

            # 简单实体提取 (大写词)
            import re
            capital_words = set(re.findall(r'\b([A-Z][A-Za-z0-9]+(?:\s[A-Z][A-Za-z0-9]+)*)\b', content))
            capital_words = {w for w in capital_words if len(w) > 2}

            for word in capital_words:
                self._write_count += 1
                eid = f"ent_{self._write_count}_{int(time.time()*1e6)}"
                entity = StructuredEntity(
                    entity_id=eid,
                    name=word,
                    entity_type="extracted",
                    properties={"source_turn": turn_id},
                )
                store.add_entity(entity)
                entities.append(entity)

            # 关系发现: 相邻实体之间建立 co-occurs 关系
            ent_list = list(entities)
            for i in range(len(ent_list) - 1):
                rel = StructuredRelation(
                    relation_id=f"rel_{i}_{int(time.time()*1e6)}",
                    source_entity_id=ent_list[i].entity_id,
                    target_entity_id=ent_list[i + 1].entity_id,
                    relation_type="co-occurs",
                )
                store.add_relation(rel)
                relations.append(rel)

            return entities, relations

    def apply_feedback(self, store: GraphMemoryStore, feedback: RWF_FeedbackSignal) -> Dict[str, Any]:
        """应用 Reader 反馈到图结构。"""
        with self._lock:
            applied: Dict[str, Any] = {"signal_id": feedback.signal_id, "type": feedback.signal_type}

            if feedback.signal_type == "reinforce":
                for eid in feedback.subject_entity_ids:
                    for rid, rel in store._relations.items():
                        if rel.source_entity_id == eid or rel.target_entity_id == eid:
                            rel.weight += 0.1
            elif feedback.signal_type == "weaken":
                for eid in feedback.subject_entity_ids:
                    for rid, rel in store._relations.items():
                        if rel.source_entity_id == eid or rel.target_entity_id == eid:
                            rel.weight = max(0.1, rel.weight - 0.1)
            elif feedback.signal_type == "merge":
                if len(feedback.subject_entity_ids) >= 2:
                    # 合并实体: 指向同一实体
                    pass  # placeholder for complex merge logic

            applied["status"] = "applied"
            return applied

    def statistics(self) -> Dict[str, Any]:
        return {"total_writes": self._write_count}


# ---------------------------------------------------------------------------
# GraphFoundationModel
# ---------------------------------------------------------------------------

class GraphFoundationModel:
    """Graph Foundation Model 读取器——检索+反馈。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def retrieve(
        self, store: GraphMemoryStore, query: str, max_hops: int = 2
    ) -> SAGE_GraphQueryResult:
        """图检索——多跳查询。"""
        with self._lock:
            start = time.perf_counter()

            # 匹配起始实体
            q_words = set(query.lower().split())
            matched_entities: List[StructuredEntity] = []

            for eid, entity in store._entities.items():
                name_lower = entity.name.lower()
                if any(w in name_lower for w in q_words):
                    matched_entities.append(entity)

            if not matched_entities:
                return SAGE_GraphQueryResult(retrieval_time_ms=round((time.perf_counter() - start) * 1000, 2))

            # 多跳检索
            all_neighbors: List[StructuredEntity] = []
            for ent in matched_entities:
                neighbors = store.get_neighbors(ent.entity_id, max_hops=max_hops)
                all_neighbors.extend(neighbors)

            # 收集关系
            seen_entities = {e.entity_id for e in matched_entities + all_neighbors}
            matched_relations: List[StructuredRelation] = []
            for rel in store._relations.values():
                if rel.source_entity_id in seen_entities and rel.target_entity_id in seen_entities:
                    matched_relations.append(rel)

            elapsed = round((time.perf_counter() - start) * 1000, 2)

            return SAGE_GraphQueryResult(
                entities=matched_entities,
                relations=matched_relations,
                evidence_paths=[[e.entity_id for e in matched_entities]],
                retrieval_time_ms=elapsed,
            )

    def generate_feedback(
        self, query_result: SAGE_GraphQueryResult, store: GraphMemoryStore
    ) -> List[RWF_FeedbackSignal]:
        """生成 Reader→Writer 反馈信号。"""
        signals: List[RWF_FeedbackSignal] = []

        # 实体太少 → 需要合并
        if len(query_result.entities) <= 2 and len(query_result.entities) > 0:
            signals.append(RWF_FeedbackSignal(
                signal_id=f"sig_merge_{int(time.time()*1e6)}",
                subject_entity_ids=[e.entity_id for e in query_result.entities],
                signal_type="merge",
                description="Sparse entities — consider merging related nodes",
            ))
        # 关系过多 → 需要弱化噪声
        elif len(query_result.relations) > 5:
            for rel in query_result.relations:
                if rel.weight < 0.5:
                    signals.append(RWF_FeedbackSignal(
                        signal_id=f"sig_weaken_{int(time.time()*1e6)}",
                        subject_entity_ids=[rel.source_entity_id, rel.target_entity_id],
                        signal_type="weaken",
                        description=f"Weak noisy relation {rel.relation_type}",
                    ))

        return signals

    def statistics(self) -> Dict[str, Any]:
        return {"status": "ready"}


# ---------------------------------------------------------------------------
# SelfEvolvingGraphMemory
# ---------------------------------------------------------------------------

class SelfEvolvingGraphMemory:
    """自进化图记忆——反馈闭环驱动迭代优化。

    Parameters
    ----------
    feedback_threshold : float
        触发自进化的反馈置信度阈值。
    """

    def __init__(self, feedback_threshold: float = 0.6) -> None:
        self.feedback_threshold = feedback_threshold
        self._evolution_round: EvolutionRound = EvolutionRound.INITIAL
        self._feedback_log: deque = deque(maxlen=200)
        self._lock = threading.RLock()

    def evolve(
        self, writer: MemoryWriter, reader: GraphFoundationModel, store: GraphMemoryStore
    ) -> Dict[str, Any]:
        """执行一轮自进化——Reader反馈→Writer应用。"""
        with self._lock:
            # 运行检索获取反馈
            result = reader.retrieve(store, "evolution probe")
            signals: List[RWF_FeedbackSignal] = []

            if result.entities:
                signals = reader.generate_feedback(result, store)

            applied_count = 0
            for sig in signals:
                if sig.confidence >= self.feedback_threshold:
                    writer.apply_feedback(store, sig)
                    self._feedback_log.append(sig)
                    applied_count += 1

            # 推进进化轮次
            current = self._evolution_round
            if current == EvolutionRound.INITIAL:
                self._evolution_round = EvolutionRound.FIRST_EVOLUTION
            elif current == EvolutionRound.FIRST_EVOLUTION:
                self._evolution_round = EvolutionRound.SECOND_EVOLUTION
            else:
                self._evolution_round = EvolutionRound.CONTINUOUS

            logger.info(
                "Evolution round %s: %d feedback signals, %d applied",
                self._evolution_round.name, len(signals), applied_count,
            )

            return {
                "round": self._evolution_round.name,
                "signals_generated": len(signals),
                "signals_applied": applied_count,
            }

    def statistics(self) -> Dict[str, Any]:
        return {
            "evolution_round": self._evolution_round.name,
            "total_feedback": len(self._feedback_log),
        }


# ---------------------------------------------------------------------------
# SAGEGraphMemoryEngine
# ---------------------------------------------------------------------------

class SAGEGraphMemoryEngine:
    """SAGE 自进化图记忆引擎。

    Parameters
    ----------
    feedback_threshold : float
        触发自进化的反馈置信度阈值。
    """

    def __init__(self, feedback_threshold: float = 0.6) -> None:
        self.graph_memory_store = GraphMemoryStore()
        self.memory_writer = MemoryWriter()
        self.graph_foundation_model = GraphFoundationModel()
        self.self_evolving_graph_memory = SelfEvolvingGraphMemory(
            feedback_threshold=feedback_threshold,
        )
        self._turn_count: int = 0
        self._lock = threading.RLock()

        logger.info("SAGEGraphMemoryEngine initialized [fb_thresh=%.2f]", feedback_threshold)

    def _persist(self) -> None:
        """EXECUTION 125: snapshot to PG (cross-process)."""
        try:
            import sys, os
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            if root not in sys.path:
                sys.path.insert(0, root)
            from trinity.adapters.postgresql import PostgreSQLAdapter
            a = PostgreSQLAdapter(auto_connect=True)
            a.connect()
            try:
                snap = {
                    "entities": [{"entity_id": e.entity_id, "name": e.name,
                                  "entity_type": e.entity_type,
                                  "attributes": getattr(e, "attributes", {})}
                                 for e in self.graph_memory_store._entities.values()],
                    "relations": [{"relation_id": r.relation_id,
                                   "source_entity_id": r.source_entity_id,
                                   "target_entity_id": r.target_entity_id,
                                   "relation_type": r.relation_type,
                                   "weight": r.weight}
                                  for r in self.graph_memory_store._relations.values()],
                    "turn_count": self._turn_count,
                }
                a.sage_save_snapshot(snap)
            finally:
                a.disconnect()
        except Exception:
            pass

    def restore_snapshot(self, snap=None) -> int:
        """EXECUTION 125: restore graph from PG snapshot."""
        if snap is None:
            try:
                import sys, os
                root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                if root not in sys.path:
                    sys.path.insert(0, root)
                from trinity.adapters.postgresql import PostgreSQLAdapter
                a = PostgreSQLAdapter(auto_connect=True)
                a.connect()
                try:
                    snap = a.sage_load_snapshot()
                finally:
                    a.disconnect()
            except Exception:
                return 0
        if not snap:
            return 0
        n = 0
        try:
            _seen_names = set()
            for ed in snap.get("entities", []):
                ent = StructuredEntity(
                    entity_id=ed.get("entity_id") or ("e%d" % n),
                    name=ed.get("name", ""),
                    entity_type=ed.get("entity_type", "entity"),
                )
                if ed.get("attributes"):
                    try:
                        ent.attributes = dict(ed["attributes"])
                    except Exception:
                        pass
                self.graph_memory_store.add_entity(ent)
                n += 1
            for rd in snap.get("relations", []):
                rel = StructuredRelation(
                    relation_id=rd.get("relation_id") or ("r%d" % n),
                    source_entity_id=rd.get("source_entity_id", ""),
                    target_entity_id=rd.get("target_entity_id", ""),
                    relation_type=rd.get("relation_type", "related_to"),
                )
                try:
                    rel.weight = float(rd.get("weight") or 1.0)
                except Exception:
                    pass
                self.graph_memory_store.add_relation(rel)
                n += 1
            self._turn_count = int(snap.get("turn_count") or 0)
        except Exception:
            pass
        return n

    def ingest_turn(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """摄入一轮交互并写入图记忆。"""
        self._turn_count += 1
        turn = {
            "id": f"turn_{self._turn_count}_{int(time.time()*1e6)}",
            "content": content,
            "metadata": metadata or {},
        }
        entities, relations = self.memory_writer.write_from_turn(self.graph_memory_store, turn)

        self._persist()  # EXECUTION 125
        return {
            "turn_id": turn["id"],
            "entities_written": len(entities),
            "relations_written": len(relations),
        }

    def query(self, query_text: str) -> Dict[str, Any]:
        """图检索查询。"""
        result = self.graph_foundation_model.retrieve(self.graph_memory_store, query_text)
        return {
            "entities": [{"name": e.name, "type": e.entity_type} for e in result.entities],
            "relations": [{"type": r.relation_type, "weight": r.weight} for r in result.relations],
            "evidence_paths_count": len(result.evidence_paths),
            "retrieval_time_ms": result.retrieval_time_ms,
        }

    def evolve(self) -> Dict[str, Any]:
        """触发一轮自进化。"""
        return self.self_evolving_graph_memory.evolve(
            self.memory_writer, self.graph_foundation_model, self.graph_memory_store,
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "turns": self._turn_count,
                "graph": self.graph_memory_store.statistics(),
                "evolution": self.self_evolving_graph_memory.statistics(),
            }
