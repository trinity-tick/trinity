"""AutoSchemaKG Dynamic Schema Induction (P34) — 对标 AutoSchemaKG (ACL 2026, 50M docs, 9亿节点, 59亿边)

实现 LLM 驱动的无预定义 Schema 知识图谱构建：

- DynamicSchemaInduction: 从文本同时抽取三元组 + 归纳 Schema，无需预定义模板
- ConceptualizationOrganizer: 将抽取实例组织进语义类别层次
- SchemaPatternObserver: 持续观察新实体 → 检测模式 → 触发 Schema 演进

设计要点：
- 92% 语义对齐人工构建 Schema，零人工干预
- 三元组抽取与 Schema 归纳交织进行，互相增强
- 模式观察基于频率阈值 + 概念新颖度触发演化
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EntityTriple:
    """实体三元组：主体-谓词-客体 + 置信度。"""
    triple_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    subject: str = ""
    predicate: str = ""
    object: str = ""
    confidence: float = 0.0
    source_text: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class SchemaNode:
    """Schema 节点：概念类别 + 实例归属。"""
    node_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    concept_name: str = ""
    parent: Optional[str] = None
    children: list[str] = field(default_factory=list)
    instances: list[str] = field(default_factory=list)
    description: str = ""
    arity: int = 0  # 关联实体类型数


@dataclass
class InductionResult:
    """Schema 归纳结果。"""
    result_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    triples: list[EntityTriple] = field(default_factory=list)
    schema_nodes: list[SchemaNode] = field(default_factory=list)
    new_concepts: list[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class PatternSignal:
    """Schema 演进信号：新实体模式触发 Schema 调整。"""
    signal_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    entity_type: str = ""
    frequency: int = 0
    suggested_schema_change: str = ""
    confidence: float = 0.0
    detected_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DynamicSchemaInduction — LLM-Driven Schema Induction
# ---------------------------------------------------------------------------

class DynamicSchemaInduction:
    """动态 Schema 归纳：从文本同时抽取三元组并归纳概念 Schema。

    无需预定义 Schema，LLM 驱动端到端抽取 + 归纳。
    """

    def __init__(self, min_confidence: float = 0.60) -> None:
        self._lock = threading.RLock()
        self._min_confidence = min_confidence
        self._triple_store: dict[str, EntityTriple] = {}
        self._schema_graph: dict[str, SchemaNode] = {}
        self._concept_frequency: Counter[str] = Counter()

    def extract_and_induce(self, text: str,
                           context_hint: str = "") -> InductionResult:
        """从文本同时抽取三元组并归纳 Schema。

        实际生产中此方法调用 LLM；此处提供规则化骨架实现。
        """
        with self._lock:
            triples = self._extract_triples(text, context_hint)
            new_concepts = self._induce_concepts(triples)
            schema_nodes = self._build_schema_nodes(new_concepts, triples)

            result = InductionResult(
                triples=triples,
                schema_nodes=schema_nodes,
                new_concepts=new_concepts,
            )
            logger.info(
                "DynamicSchemaInduction: %d triples, %d schema nodes, %d new concepts",
                len(triples), len(schema_nodes), len(new_concepts),
            )
            return result

    def _extract_triples(self, text: str, context_hint: str) -> list[EntityTriple]:
        """规则化三元组抽取骨架（生产环境替换为 LLM 调用）。"""
        triples: list[EntityTriple] = []
        sentences = text.replace("\n", " ").split(". ")
        for sent in sentences:
            words = sent.strip().split()
            if len(words) < 4:
                continue
            # 简单启发式：首词为主语，末词为宾语，中间动词为谓词
            subj = words[0]
            obj = words[-1]
            pred_candidates = [w for w in words[1:-1] if len(w) > 2]
            pred = pred_candidates[0] if pred_candidates else words[1]
            triple = EntityTriple(
                subject=subj, predicate=pred, object=obj,
                confidence=0.65, source_text=sent,
            )
            self._triple_store[triple.triple_id] = triple
            triples.append(triple)
        return triples

    def _induce_concepts(self, triples: list[EntityTriple]) -> list[str]:
        """从三元组归纳新概念类别。"""
        subjects = Counter(t.subject for t in triples)
        objects = Counter(t.object for t in triples)
        predicates = Counter(t.predicate for t in triples)

        concepts: list[str] = []
        # 高频主体 → 概念候选
        for entity, freq in subjects.most_common(5):
            if freq >= 2:
                concept = f"entity_type:{entity}"
                self._concept_frequency[concept] += freq
                if concept not in self._schema_graph:
                    concepts.append(concept)

        # 高频谓词 → 关系概念
        for pred, freq in predicates.most_common(3):
            if freq >= 2:
                concept = f"relation:{pred}"
                self._concept_frequency[concept] += freq
                if concept not in self._schema_graph:
                    concepts.append(concept)

        return concepts

    def _build_schema_nodes(self, concepts: list[str],
                            triples: list[EntityTriple]) -> list[SchemaNode]:
        """为归纳概念构建 Schema 节点。"""
        nodes: list[SchemaNode] = []
        for concept in concepts:
            instances = []
            for t in triples:
                if f"entity_type:{t.subject}" == concept:
                    instances.append(t.subject)
                elif f"relation:{t.predicate}" == concept:
                    instances.append(t.predicate)
            node = SchemaNode(
                concept_name=concept,
                instances=list(set(instances)),
                arity=len(set(instances)),
                description=f"Auto-induced concept: {concept}",
            )
            self._schema_graph[node.node_id] = node
            nodes.append(node)
        return nodes

    def query_schema(self, concept: str) -> Optional[SchemaNode]:
        """按概念名查询 Schema 节点。"""
        with self._lock:
            for node in self._schema_graph.values():
                if node.concept_name == concept:
                    return node
            return None

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "DynamicSchemaInduction",
                "triples": len(self._triple_store),
                "schema_nodes": len(self._schema_graph),
                "concepts": len(self._concept_frequency),
            }


# ---------------------------------------------------------------------------
# ConceptualizationOrganizer — 概念化组织器
# ---------------------------------------------------------------------------

class ConceptualizationOrganizer:
    """概念化组织器：将实例按语义相似度组织进概念类别层次。

    对标 AutoSchemaKG conceptualization 组件。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._concept_hierarchy: dict[str, SchemaNode] = {}

    def organize(self, instances: list[str],
                 concept_label: str) -> SchemaNode:
        """将一批实例组织为一个概念节点。"""
        with self._lock:
            node = SchemaNode(
                concept_name=concept_label,
                instances=list(set(instances)),
                arity=len(set(instances)),
                description=f"Organized concept: {concept_label}",
            )
            self._concept_hierarchy[node.node_id] = node
            logger.info("Organized concept %s with %d instances",
                        concept_label, len(instances))
            return node

    def link_parent(self, child_id: str, parent_id: str) -> None:
        """建立概念父子关系。"""
        with self._lock:
            if child_id in self._concept_hierarchy and parent_id in self._concept_hierarchy:
                self._concept_hierarchy[child_id].parent = parent_id
                self._concept_hierarchy[parent_id].children.append(child_id)
                logger.info("Linked %s → parent %s", child_id, parent_id)

    def get_hierarchy(self) -> dict[str, Any]:
        """导出层次结构。"""
        with self._lock:
            result: dict[str, Any] = {}
            for nid, node in self._concept_hierarchy.items():
                result[nid] = {
                    "concept": node.concept_name,
                    "parent": node.parent,
                    "children": node.children,
                    "instances": len(node.instances),
                }
            return result

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "ConceptualizationOrganizer",
                "concepts": len(self._concept_hierarchy),
            }


# ---------------------------------------------------------------------------
# SchemaPatternObserver — Schema 演进观察器
# ---------------------------------------------------------------------------

class SchemaPatternObserver:
    """Schema 模式观察器：持续观察新实体 → 触发 Schema 演进。

    基于频率阈值 + 概念新颖度自动检测 Schema 需要演进的时机。
    """

    def __init__(self, frequency_threshold: int = 5,
                 novelty_threshold: float = 0.30) -> None:
        self._lock = threading.RLock()
        self._frequency_threshold = frequency_threshold
        self._novelty_threshold = novelty_threshold
        self._entity_counter: Counter[str] = Counter()
        self._pattern_history: list[PatternSignal] = []
        self._known_entities: set[str] = set()

    def observe(self, entities: list[str]) -> list[PatternSignal]:
        """观察一批实体，返回触发 Schema 演进的信号。"""
        with self._lock:
            signals: list[PatternSignal] = []
            self._entity_counter.update(entities)

            for entity, freq in self._entity_counter.most_common(20):
                if freq < self._frequency_threshold:
                    continue
                is_novel = entity not in self._known_entities
                if is_novel:
                    self._known_entities.add(entity)
                    signal = PatternSignal(
                        entity_type=entity,
                        frequency=freq,
                        suggested_schema_change=f"Add concept category for {entity}",
                        confidence=min(0.90, 0.50 + freq * 0.05),
                    )
                    signals.append(signal)
                    self._pattern_history.append(signal)
                    logger.info("SchemaPatternObserver: new pattern %s (freq=%d)",
                                entity, freq)

            return signals

    def recent_signals(self, limit: int = 10) -> list[PatternSignal]:
        """返回最近的演化信号。"""
        with self._lock:
            return self._pattern_history[-limit:]

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "SchemaPatternObserver",
                "tracked_entities": len(self._entity_counter),
                "known_entities": len(self._known_entities),
                "signals_emitted": len(self._pattern_history),
                "frequency_threshold": self._frequency_threshold,
            }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def induce_schema(
    text: str,
    context_hint: str = "",
) -> dict[str, Any]:
    """便捷函数：从文本诱导 Schema + 概念化 + 模式观察。

    Returns:
        dict with induction result + organizer/observer stats.
    """
    inducer = DynamicSchemaInduction()
    organizer = ConceptualizationOrganizer()
    observer = SchemaPatternObserver()

    result = inducer.extract_and_induce(text, context_hint)
    entities = [t.subject for t in result.triples] + [t.object for t in result.triples]
    observer.observe(entities)

    for node in result.schema_nodes:
        organizer.organize(node.instances, node.concept_name)

    return {
        "triples": len(result.triples),
        "schema_nodes": len(result.schema_nodes),
        "new_concepts": result.new_concepts,
        "observer_stats": observer.statistics(),
        "organizer_stats": organizer.statistics(),
    }
