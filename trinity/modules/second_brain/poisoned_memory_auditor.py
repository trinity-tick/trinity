"""
PoisonedMemoryAuditor — Post-hoc Poisoned Memory Audit Framework
=================================================================
arXiv 2605.23723 · P38-1 · MemAudit

三元语: 投毒记忆事后审计框架。通过多层因果归因图 (MCG) 追踪
记忆注入的因果传播路径，结合结构异常检测与 NLI 一致性检查，
识别被投毒的记忆片段及其下游影响。

设计要点:
  - PoisonedMemoryAuditor: 审计调度中枢，协调 MCG 构建、异常检测
    和 NLI 验证三条流水线，输出标准化 AuditFinding 报告。
  - MultiLevelCausalGraph: 多层因果归因图，按语义层 (L0-L3) 建模
    记忆注入的因果传播链, 支持正向与反向因果追踪。
  - StructuralAnomalyDetector: 图拓扑异常检测器，利用图密度/
    出度分布/三角闭包率检测投毒特征模式。
  - ConsistencyChecker: NLI 一致性检查器，对记忆对做蕴含/中立/
    矛盾三分类，验证记忆间的逻辑一致性。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MCGNodeType(Enum):
    """MCG 节点类型 (按语义层)。"""
    RAW_INJECTION = auto()    # L0: 原始注入记忆
    DERIVED_FACT = auto()     # L1: 派生事实
    INFERRED_RULE = auto()    # L2: 推断规则
    DOWNSTREAM_ACTION = auto()  # L3: 下游行为影响


class CausalPathType(Enum):
    """因果边方向类型。"""
    ENTAILMENT = "ENTAILMENT"     # 正向蕴含
    CONFLICT = "CONFLICT"           # 正向冲突
    AMPLIFICATION = "AMPLIFICATION"  # 正向放大
    ATTENUATION = "ATTENUATION"     # 正向衰减
    REVERSE_TRACE = "REVERSE_TRACE"  # 反向追溯


class AuditSeverity(Enum):
    """审计严重等级。"""
    CRITICAL = auto()
    HIGH = auto()
    MEDIUM = auto()
    LOW = auto()
    INFO = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class MCGNode:
    """多层因果归因图节点。"""
    node_id: str
    layer: int                              # 0-3
    node_type: MCGNodeType
    content: str
    source_memory_id: str                   # 上游记忆ID
    inject_timestamp: float
    embedding: Optional[np.ndarray] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MCGEdge:
    """MCG 因果边。"""
    edge_id: str
    source: str                             # source node_id
    target: str                             # target node_id
    path_type: CausalPathType
    weight: float                           # 因果强度 [0, 1]
    evidence: str                           # 支撑证据文本
    hop_distance: int = 1


@dataclass
class AnomalyScore:
    """图结构异常评分。"""
    node_id: str
    density_deviation: float                # 局部密度偏差
    outdegree_zscore: float                 # 出度 Z-score
    triangle_closure_ratio: float           # 三角闭包率
    composite_score: float                  # 综合异常分 0-1
    is_poisoned: bool = False
    indicators: List[str] = field(default_factory=list)


@dataclass
class AuditFinding:
    """单条审计发现。"""
    finding_id: str
    severity: AuditSeverity
    description: str
    poisoned_nodes: List[str] = field(default_factory=list)
    causal_path: List[str] = field(default_factory=list)  # POI 传播链
    consistency_violations: int = 0
    recommendation: str = ""
    timestamp: float = field(default_factory=time.time)


# =============================================================================
# PoisonedMemoryAuditor
# =============================================================================

class PoisonedMemoryAuditor:
    """投毒记忆事后审计框架。

    协调多层因果归因图构建、结构异常检测和 NLI 一致性检查，
    输出标准化审计发现。

    Parameters
    ----------
    max_nodes : int
        MCG 最大节点数。
    anomaly_threshold : float
        异常判定阈值 [0, 1]。
    """

    def __init__(
        self,
        max_nodes: int = 4096,
        anomaly_threshold: float = 0.7,
    ) -> None:
        self.max_nodes = max_nodes
        self.anomaly_threshold = anomaly_threshold
        self._lock = threading.RLock()

        self._mcg = MultiLevelCausalGraph(max_nodes)
        self._detector = StructuralAnomalyDetector(anomaly_threshold)
        self._checker = ConsistencyChecker()

        self._findings: List[AuditFinding] = []
        self._audit_count: int = 0
        logger.info("PoisonedMemoryAuditor initialized [max_nodes=%d thresh=%.2f]", max_nodes, anomaly_threshold)

    def audit(
        self,
        injected_memories: List[Dict[str, Any]],
        downstream_effects: List[Dict[str, Any]],
    ) -> List[AuditFinding]:
        """对一组记忆执行完整审计。

        Parameters
        ----------
        injected_memories : List[Dict]
            可能的注入记忆列表，每项含 content / memory_id / timestamp。
        downstream_effects : List[Dict]
            下游影响记录，含 parent_id / content / layer / evidence。

        Returns
        -------
        List[AuditFinding]
            审计发现列表，按 severity 降序。
        """
        with self._lock:
            self._audit_count += 1
            self._findings.clear()

            # Phase 1: Build MCG
            for mem in injected_memories:
                self._mcg.add_node(
                    node_type=MCGNodeType.RAW_INJECTION,
                    layer=0,
                    content=mem.get("content", ""),
                    source_memory_id=mem.get("memory_id", ""),
                    inject_timestamp=mem.get("timestamp", time.time()),
                )

            for effect in downstream_effects:
                parent_id = effect.get("parent_id", "")
                parent = self._mcg.get_node(parent_id)
                layer = min((parent.layer + 1) if parent else 1, 3)
                node_type = [MCGNodeType.DERIVED_FACT, MCGNodeType.INFERRED_RULE,
                             MCGNodeType.DOWNSTREAM_ACTION][layer - 1]

                node_id = self._mcg.add_node(
                    node_type=node_type,
                    layer=layer,
                    content=effect.get("content", ""),
                    source_memory_id=effect.get("memory_id", parent_id),
                    inject_timestamp=effect.get("timestamp", time.time()),
                )
                if parent_id:
                    self._mcg.add_edge(
                        source=parent_id,
                        target=node_id,
                        path_type=CausalPathType.ENTAILMENT,
                        evidence=effect.get("evidence", ""),
                        hop_distance=layer - (parent.layer if parent else 0),
                    )

            # Phase 2: Structural anomaly detection
            anomalies = self._detector.scan(self._mcg)

            # Phase 3: NLI consistency check
            violations = self._checker.check_graph(self._mcg)

            # Phase 4: Compile findings
            poisoned_nodes = [a.node_id for a in anomalies if a.is_poisoned]
            if poisoned_nodes:
                for pn in poisoned_nodes:
                    path = self._mcg.trace_causal_path(pn)
                    relevance_violations = sum(
                        1 for v in violations if pn in v.get("involved_nodes", [])
                    )
                    severity = AuditSeverity.CRITICAL if relevance_violations >= 2 else AuditSeverity.HIGH
                    finding = AuditFinding(
                        finding_id=f"AF-{self._audit_count}-{pn[:8]}",
                        severity=severity,
                        description=f"Poisoned node {pn} with {relevance_violations} consistency violations",
                        poisoned_nodes=[pn],
                        causal_path=path,
                        consistency_violations=relevance_violations,
                        recommendation=self._recommend(pn, path),
                    )
                    self._findings.append(finding)

            # Add structural-only findings
            struct_only = [a for a in anomalies if a.is_poisoned and a.composite_score >= self.anomaly_threshold * 1.2]
            for a in struct_only:
                if not any(f for f in self._findings if a.node_id in f.poisoned_nodes):
                    self._findings.append(AuditFinding(
                        finding_id=f"AF-{self._audit_count}-s_{a.node_id[:8]}",
                        severity=AuditSeverity.MEDIUM,
                        description=f"Structural anomaly: composite={a.composite_score:.2f}",
                        poisoned_nodes=[a.node_id],
                        recommendation="Isolate node and re-verify upstream sources.",
                    ))

            self._findings.sort(key=lambda f: f.severity.value)
            return list(self._findings)

    def _recommend(self, node_id: str, path: List[str]) -> str:
        hop = len(path)
        if hop <= 1:
            return "Direct injection suspected; remove source memory and its immediate consequences."
        elif hop <= 3:
            return f"Propagation depth={hop}; quarantine the causal chain and re-evaluate L{hop} nodes."
        else:
            return f"Deep propagation ({hop} hops); full rollback of the causal tree recommended."

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "audit_count": self._audit_count,
                "total_findings": len(self._findings),
                "critical": sum(1 for f in self._findings if f.severity == AuditSeverity.CRITICAL),
                "mcg": self._mcg.statistics(),
                "detector": self._detector.statistics(),
                "checker": self._checker.statistics(),
            }


# =============================================================================
# MultiLevelCausalGraph
# =============================================================================

class MultiLevelCausalGraph:
    """多层因果归因图 (MCG)。

    按四层语义深度 (L0-L3) 建模记忆注入的因果传播路径,
    支持正向追踪 (injection → effect) 和反向追溯 (effect → root cause)。

    Parameters
    ----------
    max_nodes : int
        最大节点数。
    """

    def __init__(self, max_nodes: int = 4096) -> None:
        self.max_nodes = max_nodes
        self._lock = threading.RLock()
        self._nodes: Dict[str, MCGNode] = {}
        self._edges: List[MCGEdge] = []
        self._adj: Dict[str, List[str]] = {}       # source → [target]
        self._rev_adj: Dict[str, List[str]] = {}    # target → [source]
        logger.info("MultiLevelCausalGraph initialized [max=%d]", max_nodes)

    def add_node(
        self,
        node_type: MCGNodeType,
        layer: int,
        content: str,
        source_memory_id: str,
        inject_timestamp: float,
    ) -> str:
        with self._lock:
            if len(self._nodes) >= self.max_nodes:
                self._evict_oldest()
            node_id = f"mcg_{uuid.uuid4().hex[:12]}"
            node = MCGNode(
                node_id=node_id,
                layer=layer,
                node_type=node_type,
                content=content,
                source_memory_id=source_memory_id,
                inject_timestamp=inject_timestamp,
            )
            self._nodes[node_id] = node
            self._adj.setdefault(node_id, [])
            self._rev_adj.setdefault(node_id, [])
            return node_id

    def add_edge(
        self,
        source: str,
        target: str,
        path_type: CausalPathType,
        evidence: str,
        hop_distance: int = 1,
    ) -> Optional[str]:
        with self._lock:
            if source not in self._nodes or target not in self._nodes:
                return None
            edge_id = f"mcg_e_{uuid.uuid4().hex[:12]}"
            edge = MCGEdge(
                edge_id=edge_id,
                source=source,
                target=target,
                path_type=path_type,
                weight=1.0 / (1.0 + 0.5 * (hop_distance - 1)),
                evidence=evidence,
                hop_distance=hop_distance,
            )
            self._edges.append(edge)
            self._adj[source].append(target)
            self._rev_adj[target].append(source)
            return edge_id

    def get_node(self, node_id: str) -> Optional[MCGNode]:
        return self._nodes.get(node_id)

    def trace_causal_path(self, node_id: str) -> List[str]:
        """反向追溯因果路径到 L0 注入源。"""
        with self._lock:
            path: List[str] = []
            visited: Set[str] = set()
            queue = [node_id]
            while queue:
                cur = queue.pop(0)
                if cur in visited:
                    continue
                visited.add(cur)
                path.append(cur)
                for parent in self._rev_adj.get(cur, []):
                    queue.append(parent)
            return path

    def get_edges_from(self, node_id: str) -> List[MCGEdge]:
        return [e for e in self._edges if e.source == node_id]

    def get_layer_distribution(self) -> Dict[int, int]:
        dist: Dict[int, int] = {}
        for n in self._nodes.values():
            dist[n.layer] = dist.get(n.layer, 0) + 1
        return dist

    def _evict_oldest(self) -> None:
        oldest = min(self._nodes.values(), key=lambda n: n.inject_timestamp)
        del self._nodes[oldest.node_id]
        self._adj.pop(oldest.node_id, None)
        self._rev_adj.pop(oldest.node_id, None)
        self._edges = [e for e in self._edges if e.source != oldest.node_id and e.target != oldest.node_id]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "nodes": len(self._nodes),
                "edges": len(self._edges),
                "layer_distribution": self.get_layer_distribution(),
            }


# =============================================================================
# StructuralAnomalyDetector
# =============================================================================

class StructuralAnomalyDetector:
    """图结构异常检测器。

    检测 MCG 拓扑中的投毒特征: 异常高密度子图、出度尖峰、
    三角闭包率异常 (投毒节点常表现为风扇形孤岛而非三角形闭合)。

    Parameters
    ----------
    threshold : float
        综合异常判定阈值 [0, 1]。
    """

    def __init__(self, threshold: float = 0.7) -> None:
        self.threshold = threshold
        self._lock = threading.RLock()
        self._scan_count: int = 0
        logger.info("StructuralAnomalyDetector initialized [threshold=%.2f]", threshold)

    def scan(self, mcg: MultiLevelCausalGraph) -> List[AnomalyScore]:
        with self._lock:
            self._scan_count += 1
            scores: List[AnomalyScore] = []

            nodes = list(mcg._nodes.values())
            if len(nodes) < 3:
                return scores

            total_nodes = len(nodes)
            total_edges = len(mcg._edges)
            global_density = total_edges / max(total_nodes * (total_nodes - 1), 1)

            # Outdegree stats
            outdegrees = [len(mcg._adj.get(n.node_id, [])) for n in nodes]
            mean_out = np.mean(outdegrees) if outdegrees else 0.0
            std_out = np.std(outdegrees) if outdegrees else 1.0

            for node in nodes:
                node_id = node.node_id

                # Local density: ego-network density
                neighbors = set(mcg._adj.get(node_id, []))
                neighbors.update(mcg._rev_adj.get(node_id, []))
                n_neighbors = len(neighbors)
                if n_neighbors >= 2:
                    ego_edges = sum(
                        1 for e in mcg._edges
                        if e.source in neighbors and e.target in neighbors
                    )
                    local_density = ego_edges / (n_neighbors * (n_neighbors - 1))
                else:
                    local_density = 0.0
                density_deviation = abs(local_density - global_density)

                # Outdegree z-score
                od = len(mcg._adj.get(node_id, []))
                outdegree_z = (od - mean_out) / max(std_out, 1e-8)

                # Triangle closure ratio
                triangle_count = 0
                target_set = set(mcg._adj.get(node_id, []))
                for t in target_set:
                    t_neighbors = set(mcg._adj.get(t, []))
                    triangle_count += len(target_set & t_neighbors)
                max_triangles = n_neighbors * (n_neighbors - 1) // 2
                closure_ratio = triangle_count / max(max_triangles, 1)

                # Composite score — high density + high outdegree + low closure = poison pattern
                composite = (
                    0.35 * min(density_deviation / max(global_density, 0.01), 1.0)
                    + 0.35 * min(abs(outdegree_z) / 3.0, 1.0)
                    + 0.30 * (1.0 - min(closure_ratio, 1.0))
                )

                indicators = []
                if density_deviation > global_density * 2:
                    indicators.append("HIGH_DENSITY_EGO")
                if abs(outdegree_z) > 2.5:
                    indicators.append("OUTDEGREE_SPIKE")
                if closure_ratio < 0.2 and n_neighbors >= 3:
                    indicators.append("LOW_CLOSURE_FAN_PATTERN")

                is_poisoned = composite >= self.threshold

                scores.append(AnomalyScore(
                    node_id=node_id,
                    density_deviation=float(density_deviation),
                    outdegree_zscore=float(outdegree_z),
                    triangle_closure_ratio=float(closure_ratio),
                    composite_score=float(composite),
                    is_poisoned=is_poisoned,
                    indicators=indicators,
                ))

            return scores

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"scans": self._scan_count, "threshold": self.threshold}


# =============================================================================
# ConsistencyChecker
# =============================================================================

class ConsistencyChecker:
    """NLI 一致性检查器。

    对记忆对做蕴含/中立/矛盾三分类语义判断, 验证记忆间的逻辑一致性。
    矛盾对数量超过阈值则标记为可疑。

    Parameters
    ----------
    contradiction_threshold : int
        矛盾数超过此值即触发警报。
    precision : int
        语义哈希精度（控制向量维度）。
    """

    def __init__(self, contradiction_threshold: int = 3, precision: int = 128) -> None:
        self.contradiction_threshold = contradiction_threshold
        self.precision = precision
        self._lock = threading.RLock()
        self._check_count: int = 0
        self._total_contradictions: int = 0
        logger.info("ConsistencyChecker initialized [threshold=%d]", contradiction_threshold)

    def check_graph(self, mcg: MultiLevelCausalGraph) -> List[Dict[str, Any]]:
        """对 MCG 中节点对做一致性检查。

        Returns
        -------
        List[Dict]
            矛盾列表，每项含 node_a / node_b / label / score / involved_nodes。
        """
        with self._lock:
            self._check_count += 1
            nodes = list(mcg._nodes.values())
            violations: List[Dict[str, Any]] = []

            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    label, score = self._nli_pair(nodes[i], nodes[j])
                    if label == "contradiction":
                        self._total_contradictions += 1
                        violations.append({
                            "node_a": nodes[i].node_id,
                            "node_b": nodes[j].node_id,
                            "label": label,
                            "score": score,
                            "involved_nodes": [nodes[i].node_id, nodes[j].node_id],
                        })

            return violations

    def _nli_pair(self, a: MCGNode, b: MCGNode) -> Tuple[str, float]:
        """NLI 判断 (哈希代理)。生产环境替换为 NLI 模型 (RoBERTa-MNLI / DeBERTa)。"""
        # 语义哈希 + 余弦相似度简化判断
        ha = hashlib.sha256(a.content.encode()).digest()
        hb = hashlib.sha256(b.content.encode()).digest()
        vec_a = np.frombuffer(ha[:self.precision // 8], dtype=np.uint8).astype(np.float32)
        vec_b = np.frombuffer(hb[:self.precision // 8], dtype=np.uint8).astype(np.float32)
        vec_a = vec_a / (np.linalg.norm(vec_a) + 1e-8)
        vec_b = vec_b / (np.linalg.norm(vec_b) + 1e-8)
        sim = float(np.dot(vec_a, vec_b))

        # 不同层、相似度在中间带 → 矛盾概率高
        if a.layer != b.layer and 0.3 < sim < 0.7:
            score = 1.0 - sim
            return ("contradiction", score)
        elif sim >= 0.85:
            return ("entailment", sim)
        else:
            return ("neutral", sim)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "checks": self._check_count,
                "total_contradictions": self._total_contradictions,
                "threshold": self.contradiction_threshold,
            }
