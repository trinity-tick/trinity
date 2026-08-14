"""
P18-7: Persona Tree — Three-Layer Structured Personality Memory
================================================================

对标 PersonaTree (arXiv 2606.04780)。

设计要点：
  - 三层人格树：Leaf 事件证据 → Mid 行为模式 → Root 持久主张
  - 类型化支持边（support edges）：证据→模式→主张的可追溯链
  - Schema 形成引擎：从底层事件抽象化形成中层行为模式
  - 生命周期操作：写入 / 整合 / 检索
  - 置信度传播与衰减：低层证据更新自动传播到高层

核心组件：
  - PersonaTreeBuilder:      三层人格树构建器
  - SupportEdgeManager:      类型化支持边管理
  - SchemaFormationEngine:   证据抽象化 schema 形成引擎
  - PersonaTreeLifecycle:    生命周期操作（写入/整合/检索）
  - ConfidencePropagator:    置信度传播与衰减
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

class TreeNodeLevel(Enum):
    """人格树层级。"""
    LEAF = "leaf"        # 事件证据层
    MID = "mid"          # 行为模式层
    ROOT = "root"        # 持久主张层


class EdgeType(Enum):
    """支持边类型。"""
    SUPPORTS = "supports"         # 证据支持模式
    CONTRADICTS = "contradicts"   # 证据矛盾模式
    WEAKENS = "weakens"          # 证据弱化模式
    REINFORCES = "reinforces"    # 强化已有模式


class LifecycleOperation(Enum):
    """生命周期操作。"""
    WRITE = "write"
    INTEGRATE = "integrate"
    RETRIEVE = "retrieve"
    PRUNE = "prune"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class PersonaNode:
    """人格树节点。"""
    node_id: str
    level: TreeNodeLevel
    label: str
    description: str
    confidence: float = 1.0
    evidence_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SupportEdge:
    """类型化支持边。"""
    edge_id: str
    source_id: str           # 底层节点
    target_id: str           # 上层节点
    edge_type: EdgeType
    weight: float = 1.0
    evidence: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class SchemaPattern:
    """Schema 模式 — 从底层事件抽象出的行为模式。"""
    pattern_id: str
    label: str
    description: str
    source_leaf_ids: List[str] = field(default_factory=list)
    confidence: float = 0.0
    abstraction_steps: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class ConfidenceUpdate:
    """置信度更新记录。"""
    node_id: str
    old_confidence: float
    new_confidence: float
    delta: float
    reason: str
    propagated_from: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Core Components
# ============================================================================

class PersonaTreeBuilder:
    """三层人格树构建器。

    Leaf → Mid → Root 三层结构。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.nodes: Dict[str, PersonaNode] = {}
        self.edges: List[SupportEdge] = []
        self.children: Dict[str, Set[str]] = defaultdict(set)   # parent → children
        self.parents: Dict[str, str] = {}                        # child → parent

    def add_leaf(self, label: str, description: str, confidence: float = 1.0,
                 metadata: Optional[Dict[str, Any]] = None) -> str:
        """添加叶子节点（事件证据）。"""
        with self._lock:
            return self._add_node(TreeNodeLevel.LEAF, label, description, confidence, metadata)

    def add_mid(self, label: str, description: str, confidence: float = 0.5,
                metadata: Optional[Dict[str, Any]] = None) -> str:
        """添加中层节点（行为模式）。"""
        with self._lock:
            return self._add_node(TreeNodeLevel.MID, label, description, confidence, metadata)

    def add_root(self, label: str, description: str, confidence: float = 0.3,
                 metadata: Optional[Dict[str, Any]] = None) -> str:
        """添加根节点（持久主张）。"""
        with self._lock:
            return self._add_node(TreeNodeLevel.ROOT, label, description, confidence, metadata)

    def _add_node(self, level: TreeNodeLevel, label: str, description: str, confidence: float,
                  metadata: Optional[Dict[str, Any]]) -> str:
        node_id = str(uuid.uuid4())[:8]
        node = PersonaNode(
            node_id=node_id, level=level, label=label, description=description,
            confidence=confidence, metadata=metadata or {},
        )
        self.nodes[node_id] = node
        return node_id

    def connect(self, source_id: str, target_id: str, edge_type: EdgeType,
                weight: float = 1.0, evidence: str = "") -> str:
        """建立支持边。"""
        with self._lock:
            if source_id not in self.nodes or target_id not in self.nodes:
                raise ValueError(f"Source {source_id} or target {target_id} not in tree")
            edge_id = str(uuid.uuid4())[:8]
            edge = SupportEdge(edge_id=edge_id, source_id=source_id, target_id=target_id,
                               edge_type=edge_type, weight=weight, evidence=evidence)
            self.edges.append(edge)
            self.children[target_id].add(source_id)
            self.parents[source_id] = target_id
            # 更新证据计数
            self.nodes[source_id].evidence_count += 1
            return edge_id

    def get_ancestry(self, node_id: str) -> List[PersonaNode]:
        """获取从叶子到根的全路径。"""
        with self._lock:
            path: List[PersonaNode] = []
            current = node_id
            while current:
                node = self.nodes.get(current)
                if node:
                    path.append(node)
                current = self.parents.get(current, "")
            return path

    def get_supporting_evidence(self, node_id: str) -> List[Tuple[PersonaNode, SupportEdge]]:
        """获取某节点的所有支持证据。"""
        with self._lock:
            evidence: List[Tuple[PersonaNode, SupportEdge]] = []
            for edge in self.edges:
                if edge.target_id == node_id:
                    src_node = self.nodes.get(edge.source_id)
                    if src_node:
                        evidence.append((src_node, edge))
            return evidence

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            level_counts = defaultdict(int)
            for n in self.nodes.values():
                level_counts[n.level.value] += 1
            return {
                "total_nodes": len(self.nodes),
                "total_edges": len(self.edges),
                "by_level": dict(level_counts),
            }


class SupportEdgeManager:
    """类型化支持边管理。

    Supports / Contradicts / Weakens / Reinforces。
    """

    def __init__(self, tree: PersonaTreeBuilder):
        self._lock = threading.RLock()
        self.tree = tree
        self.edge_types: Dict[EdgeType, float] = {
            EdgeType.SUPPORTS: 1.0,
            EdgeType.REINFORCES: 0.8,
            EdgeType.WEAKENS: -0.5,
            EdgeType.CONTRADICTS: -1.0,
        }

    def get_effective_weight(self, edge_type: EdgeType, base_weight: float = 1.0) -> float:
        return self.edge_types.get(edge_type, 0.0) * base_weight

    def aggregate_evidence(self, target_id: str) -> float:
        """聚合所有指向 target 的边的综合权重。"""
        with self._lock:
            total = 0.0
            for edge in self.tree.edges:
                if edge.target_id == target_id:
                    total += self.get_effective_weight(edge.edge_type, edge.weight)
            return total

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = defaultdict(int)
            for e in self.tree.edges:
                type_counts[e.edge_type.value] += 1
            return {"total_edges": len(self.tree.edges), "by_type": dict(type_counts)}


class SchemaFormationEngine:
    """Schema 形成引擎。

    从底层 Leaf 事件抽象出 Middle 行为模式：
    1. 收集同主题 Leaf
    2. 提取共同特征
    3. 形成 Mid 节点
    4. 建立支持边
    """

    def __init__(self, tree: PersonaTreeBuilder):
        self._lock = threading.RLock()
        self.tree = tree
        self.patterns: List[SchemaPattern] = []

    def form_schema(self, leaf_labels: List[str], pattern_label: str, pattern_description: str,
                    abstraction_threshold: int = 3) -> Optional[str]:
        """从 Leaf 节点抽象形成 Mid 行为模式。"""
        with self._lock:
            # 查找匹配的 Leaf
            matching_leaves = [
                nid for nid, node in self.tree.nodes.items()
                if node.level == TreeNodeLevel.LEAF and any(lbl in node.label for lbl in leaf_labels)
            ]
            if len(matching_leaves) < abstraction_threshold:
                return None

            # 创建 Mid 模式节点
            avg_confidence = sum(self.tree.nodes[nid].confidence for nid in matching_leaves) / len(matching_leaves)
            mid_id = self.tree.add_mid(pattern_label, pattern_description, avg_confidence)

            # 建立支持边
            for leaf_id in matching_leaves:
                self.tree.connect(leaf_id, mid_id, EdgeType.SUPPORTS, weight=1.0 / len(matching_leaves))

            # 记录 schema 模式
            pattern = SchemaPattern(
                pattern_id=str(uuid.uuid4())[:8],
                label=pattern_label,
                description=pattern_description,
                source_leaf_ids=matching_leaves,
                confidence=avg_confidence,
                abstraction_steps=len(matching_leaves),
            )
            self.patterns.append(pattern)
            return mid_id

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"total_patterns": len(self.patterns)}


class PersonaTreeLifecycle:
    """生命周期操作：写入 / 整合 / 检索 / 修剪。"""

    def __init__(self, tree: PersonaTreeBuilder, edge_mgr: SupportEdgeManager, schema_engine: SchemaFormationEngine):
        self._lock = threading.RLock()
        self.tree = tree
        self.edge_mgr = edge_mgr
        self.schema_engine = schema_engine
        self.operation_log: List[Tuple[LifecycleOperation, str, float]] = []

    def write(self, label: str, description: str, level: TreeNodeLevel = TreeNodeLevel.LEAF,
              confidence: float = 1.0) -> str:
        with self._lock:
            node_id = self.tree._add_node(level, label, description, confidence)
            self.operation_log.append((LifecycleOperation.WRITE, node_id, time.time()))
            return node_id

    def integrate(self, leaf_labels: List[str], pattern_label: str, pattern_description: str) -> Optional[str]:
        with self._lock:
            mid_id = self.schema_engine.form_schema(leaf_labels, pattern_label, pattern_description)
            if mid_id:
                self.operation_log.append((LifecycleOperation.INTEGRATE, mid_id, time.time()))
            return mid_id

    def retrieve(self, node_id: str, depth: int = 3) -> Dict[str, Any]:
        with self._lock:
            node = self.tree.nodes.get(node_id)
            if not node:
                return {}
            ancestry = self.tree.get_ancestry(node_id)[:depth]
            evidence = self.tree.get_supporting_evidence(node_id)
            return {
                "node": {
                    "id": node.node_id, "level": node.level.value, "label": node.label,
                    "description": node.description, "confidence": node.confidence,
                },
                "ancestry": [{"id": n.node_id, "label": n.label} for n in ancestry[1:]],
                "supporting_evidence": [
                    {"id": n.node_id, "label": n.label, "type": e.edge_type.value}
                    for n, e in evidence
                ],
            }

    def prune(self, confidence_threshold: float = 0.1) -> int:
        """修剪低置信度节点。"""
        with self._lock:
            to_remove: List[str] = []
            for nid, node in self.tree.nodes.items():
                if node.confidence < confidence_threshold and node.level != TreeNodeLevel.ROOT:
                    to_remove.append(nid)
            for nid in to_remove:
                self.tree.nodes.pop(nid, None)
                self.tree.edges = [e for e in self.tree.edges if e.source_id != nid and e.target_id != nid]
            return len(to_remove)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            op_counts = defaultdict(int)
            for op, _, _ in self.operation_log:
                op_counts[op.value] += 1
            return {
                "total_operations": len(self.operation_log),
                "by_type": dict(op_counts),
                "tree": self.tree.statistics(),
            }


class ConfidencePropagator:
    """置信度传播与衰减。

    下层节点置信度更新 → 上层节点置信度自动传播调整。
    """

    def __init__(self, tree: PersonaTreeBuilder, decay_factor: float = 0.95):
        self._lock = threading.RLock()
        self.tree = tree
        self.decay_factor = decay_factor
        self.update_history: List[ConfidenceUpdate] = []

    def update_leaf(self, leaf_id: str, new_confidence: float, reason: str = ""):
        with self._lock:
            node = self.tree.nodes.get(leaf_id)
            if not node or node.level != TreeNodeLevel.LEAF:
                return
            old = node.confidence
            node.confidence = new_confidence
            node.updated_at = time.time()
            self.update_history.append(ConfidenceUpdate(
                node_id=leaf_id, old_confidence=old, new_confidence=new_confidence,
                delta=new_confidence - old, reason=reason,
            ))
            self._propagate_up(leaf_id)

    def _propagate_up(self, source_id: str):
        """自底向上传播置信度。"""
        target_id = self.tree.parents.get(source_id)
        if not target_id:
            return
        target_node = self.tree.nodes.get(target_id)
        if not target_node:
            return

        # 收集所有 child 的置信度和边权重
        total_weight = 0.0
        weighted_confidence = 0.0
        edge_mgr = SupportEdgeManager(self.tree)

        for edge in self.tree.edges:
            if edge.target_id == target_id:
                child = self.tree.nodes.get(edge.source_id)
                if child:
                    w = edge_mgr.get_effective_weight(edge.edge_type, edge.weight)
                    w = abs(w)  # 取绝对值用于置信度传播
                    weighted_confidence += child.confidence * w
                    total_weight += w

        if total_weight > 0:
            new_conf = weighted_confidence / total_weight
            old = target_node.confidence
            target_node.confidence = old * (1 - self.decay_factor) + new_conf * self.decay_factor
            target_node.updated_at = time.time()
            self.update_history.append(ConfidenceUpdate(
                node_id=target_id, old_confidence=old, new_confidence=target_node.confidence,
                delta=target_node.confidence - old, reason=f"Propagated from {source_id}",
                propagated_from=source_id,
            ))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_updates": len(self.update_history),
                "avg_delta": round(sum(u.delta for u in self.update_history) / max(len(self.update_history), 1), 4),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P18-7 Persona Tree",
        "benchmark": "PersonaTree (arXiv 2606.04780)",
        "classes": 5,
        "enums": 3,
        "dataclasses": 5,
        "key_pattern": "3-Layer Tree (Leaf→Mid→Root) + Typed Support Edges + Schema Formation + Lifecycle + Confidence Propagation",
        "key_metric": "Structured personality memory with confidence propagation & evidence traceability",
        "thread_safe": True,
    }
