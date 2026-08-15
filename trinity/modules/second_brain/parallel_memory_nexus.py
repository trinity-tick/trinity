"""
# status: orphan (2026-08-15 audit, not in runtime path)
P14-4: Brain-Inspired Parallel Memory Architecture (对标 RoboMemory arXiv 2508.01415)
=======================================================================================

核心设计（基于 RoboMemory: Spatial/Temporal/Episodic/Semantic Parallel Memory）：
  - ParallelMemoryNexus：统一调度层，并行触发四种记忆模块的更新/检索/合并
  - SpatialMemoryAdapter：空间记忆适配器——为机器人/AR 场景预留，图结构表达空间关系
  - VLMBasedSemanticUpdater：VLM 驱动的语义记忆增量更新器，
    对每条新记忆执行四态决策（添加/更新/移除/无操作）
  - ClosedLoopPlanner：闭环规划器——从情景记忆中检索相似成功轨迹，引导当前任务规划
  - MemoryFusion：四种记忆的融合结果，按任务类型自适应加权

兼容性：
  - 与 episodic_rl.py（EpisodicRLScorer）接口兼容
  - 与 graph.py / graph_router.py 图结构接口兼容
  - 与 Semantic Memory 模块接口兼容

Reference:
  - RoboMemory: Parallel Spatial/Temporal/Episodic/Semantic Memory (arXiv 2508.01415)
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ──────────────────────────────────────────────────

class MemoryType(Enum):
    """四种记忆类型。"""
    SPATIAL = "spatial"        # 空间记忆：物理位置、导航路径
    TEMPORAL = "temporal"      # 时间记忆：事件序列、时序关系
    EPISODIC = "episodic"      # 情景记忆：经验轨迹、成功/失败案例
    SEMANTIC = "semantic"      # 语义记忆：概念、事实、知识图谱


class FusionStrategy(Enum):
    """融合策略。"""
    EQUAL_WEIGHT = "equal_weight"          # 等权融合
    TASK_ADAPTIVE = "task_adaptive"         # 任务自适应
    CONFIDENCE_WEIGHTED = "confidence_weighted"  # 置信度加权
    EPISODIC_BIAS = "episodic_bias"         # 情景记忆偏置


class UpdateDecision(Enum):
    """四态更新决策。"""
    ADD = "add"          # 新增记忆
    UPDATE = "update"    # 更新已有记忆
    REMOVE = "remove"    # 移除记忆
    NO_OP = "no_op"      # 无需操作


class TaskType(Enum):
    """任务类型（用于自适应融合加权）。"""
    NAVIGATION = "navigation"      # 导航任务 → 空间+情景偏重
    DIALOGUE = "dialogue"          # 对话任务 → 语义+时间偏重
    PLANNING = "planning"          # 规划任务 → 情景+语义偏重
    Q_A = "q_a"                    # 问答 → 语义偏重
    MANIPULATION = "manipulation"  # 操作 → 空间+情景偏重
    CODE = "code"                  # 编码 → 语义偏重
    GENERAL = "general"            # 通用 → 等权


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class SpatialNode:
    """空间节点。"""
    node_id: str
    label: str
    position: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # (x, y, z)
    properties: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SpatialEdge:
    """空间关系边。"""
    edge_id: str
    source: str
    target: str
    relation: str = "adjacent"  # adjacent / contains / near / far
    distance: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SpatialGraph:
    """空间关系图。"""
    nodes: Dict[str, SpatialNode] = field(default_factory=dict)
    edges: List[SpatialEdge] = field(default_factory=list)
    version: int = 1

    def add_node(self, node: SpatialNode) -> None:
        self.nodes[node.node_id] = node

    def add_edge(self, edge: SpatialEdge) -> None:
        self.edges.append(edge)

    def get_neighbors(self, node_id: str) -> List[Tuple[str, str, float]]:
        """获取邻居列表：[(target_id, relation, distance), ...]"""
        neighbors = []
        for e in self.edges:
            if e.source == node_id:
                neighbors.append((e.target, e.relation, e.distance))
            elif e.target == node_id:
                neighbors.append((e.source, e.relation, e.distance))
        return neighbors

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "version": self.version,
        }


@dataclass
class MemoryEntry:
    """记忆条目。"""
    entry_id: str
    memory_type: MemoryType
    content: str
    embedding: Optional[List[float]] = None
    confidence: float = 0.5
    timestamp: float = field(default_factory=time.time)
    source: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FusionResult:
    """四种记忆融合结果。"""
    result_id: str
    task_type: TaskType
    spatial_weight: float = 0.25
    temporal_weight: float = 0.25
    episodic_weight: float = 0.25
    semantic_weight: float = 0.25
    fused_entries: List[MemoryEntry] = field(default_factory=list)
    confidence: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, float]:
        return {
            "spatial_weight": round(self.spatial_weight, 4),
            "temporal_weight": round(self.temporal_weight, 4),
            "episodic_weight": round(self.episodic_weight, 4),
            "semantic_weight": round(self.semantic_weight, 4),
            "confidence": round(self.confidence, 4),
        }


@dataclass
class EpisodeTrace:
    """情景记忆轨迹。"""
    trace_id: str
    task_type: TaskType
    state_sequence: List[Dict[str, Any]] = field(default_factory=list)
    action_sequence: List[str] = field(default_factory=list)
    outcome: str = "unknown"  # success / failure / partial
    reward: float = 0.0
    timestamp: float = field(default_factory=time.time)
    similarity_score: float = 0.0  # 与当前任务的相似度


# ── 核心类 ─────────────────────────────────────────────────────────

class SpatialMemoryAdapter:
    """空间记忆适配器

    为机器人/AR 场景预留，以图结构表达空间关系。
    当前提供：
      - 空间节点的增删查改
      - 空间关系的建立与查询
      - 路径查找（最短距离、最近邻居）
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._graph = SpatialGraph()
        self._entity_index: Dict[str, SpatialNode] = {}

    def add_location(
        self,
        label: str,
        position: Tuple[float, float, float] = (0.0, 0.0, 0.0),
        properties: Optional[Dict[str, Any]] = None,
    ) -> SpatialNode:
        """添加空间位置节点。"""
        with self._lock:
            node = SpatialNode(
                node_id=str(uuid.uuid4())[:12],
                label=label,
                position=position,
                properties=properties or {},
            )
            self._graph.add_node(node)
            self._entity_index[node.label] = node
            return node

    def add_relation(
        self, source_id: str, target_id: str, relation: str = "adjacent",
        distance: float = 0.0, metadata: Optional[Dict[str, Any]] = None,
    ) -> SpatialEdge:
        """添加空间关系边。"""
        with self._lock:
            edge = SpatialEdge(
                edge_id=str(uuid.uuid4())[:12],
                source=source_id,
                target=target_id,
                relation=relation,
                distance=distance,
                metadata=metadata or {},
            )
            self._graph.add_edge(edge)
            return edge

    def query_nearby(
        self, node_id: str, max_distance: Optional[float] = None
    ) -> List[Tuple[str, str, float]]:
        """查询附近节点。"""
        with self._lock:
            neighbors = self._graph.get_neighbors(node_id)
            if max_distance is not None:
                neighbors = [(t, r, d) for t, r, d in neighbors if d <= max_distance]
            return sorted(neighbors, key=lambda x: x[2])

    def find_path(
        self, source_id: str, target_id: str, max_depth: int = 10
    ) -> Optional[List[str]]:
        """BFS 最短路径查找。"""
        with self._lock:
            if source_id not in self._graph.nodes or target_id not in self._graph.nodes:
                return None

            visited = {source_id}
            queue = deque([(source_id, [source_id])])

            while queue:
                current, path = queue.popleft()
                if current == target_id:
                    return path
                if len(path) >= max_depth:
                    continue

                for neighbor, _, _ in self._graph.get_neighbors(current):
                    if neighbor not in visited:
                        visited.add(neighbor)
                        queue.append((neighbor, path + [neighbor]))

            return None

    def get_location_by_label(self, label: str) -> Optional[SpatialNode]:
        """按标签查找位置。"""
        with self._lock:
            return self._entity_index.get(label)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "node_count": len(self._graph.nodes),
                "edge_count": len(self._graph.edges),
                "graph_density": (
                    len(self._graph.edges) / max(len(self._graph.nodes) * (len(self._graph.nodes) - 1), 1)
                ),
                "indexed_entities": len(self._entity_index),
            }


class VLMBasedSemanticUpdater:
    """VLM 驱动的语义记忆增量更新器

    对每条新记忆执行四态决策：
      1. ADD：新事实/概念，不存在于现有语义记忆中
      2. UPDATE：部分匹配，需更新已有记忆
      3. REMOVE：已过时/被证伪的记忆
      4. NO_OP：完全重复，无需操作

    当前使用启发式规则（embedding 余弦相似度 + 置信度阈值），
    预留 VLM 接口供未来多模态扩展。
    """

    SIMILARITY_THRESHOLDS = {
        UpdateDecision.ADD: 0.3,       # 相似度 < 0.3 → 新记忆
        UpdateDecision.UPDATE: 0.7,    # 0.3 ~ 0.7 → 更新
        UpdateDecision.NO_OP: 0.95,    # > 0.95 → 无操作
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._memory_store: Dict[str, MemoryEntry] = {}
        self._update_log: List[Dict[str, Any]] = []

    def decide(
        self,
        new_entry: MemoryEntry,
        existing_entries: Optional[List[MemoryEntry]] = None,
    ) -> Tuple[UpdateDecision, Optional[MemoryEntry]]:
        """对新记忆做四态决策。

        Returns:
            (decision, matched_existing_entry|None)
        """
        with self._lock:
            if existing_entries is None:
                existing_entries = list(self._memory_store.values())

            if not existing_entries:
                return (UpdateDecision.ADD, None)

            # 计算与所有现有记忆的最大相似度
            max_sim = 0.0
            best_match: Optional[MemoryEntry] = None

            for entry in existing_entries:
                sim = self._compute_similarity(new_entry, entry)
                if sim > max_sim:
                    max_sim = sim
                    best_match = entry

            if max_sim < self.SIMILARITY_THRESHOLDS[UpdateDecision.ADD]:
                decision = UpdateDecision.ADD
            elif max_sim > self.SIMILARITY_THRESHOLDS[UpdateDecision.NO_OP]:
                decision = UpdateDecision.NO_OP
            else:
                # 在中间区域，检查置信度差距
                if best_match and new_entry.confidence > best_match.confidence + 0.2:
                    decision = UpdateDecision.UPDATE
                else:
                    decision = UpdateDecision.NO_OP

            self._update_log.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "entry_id": new_entry.entry_id,
                "decision": decision.value,
                "max_similarity": round(max_sim, 4),
                "matched_entry": best_match.entry_id if best_match else None,
            })

            return (decision, best_match)

    def apply(
        self,
        new_entry: MemoryEntry,
        decision: UpdateDecision,
        matched: Optional[MemoryEntry] = None,
    ) -> None:
        """执行四态决策。"""
        with self._lock:
            if decision == UpdateDecision.ADD:
                self._memory_store[new_entry.entry_id] = new_entry
            elif decision == UpdateDecision.UPDATE and matched:
                matched.content = new_entry.content
                matched.confidence = max(matched.confidence, new_entry.confidence)
                matched.timestamp = new_entry.timestamp
                matched.metadata.update(new_entry.metadata)
            elif decision == UpdateDecision.REMOVE:
                self._memory_store.pop(new_entry.entry_id, None)
            # NO_OP: do nothing

    def ingest(self, new_entry: MemoryEntry) -> Tuple[UpdateDecision, Optional[MemoryEntry]]:
        """一站式摄入：决策 + 执行。"""
        decision, matched = self.decide(new_entry)
        self.apply(new_entry, decision, matched)
        return (decision, matched)

    def _compute_similarity(self, a: MemoryEntry, b: MemoryEntry) -> float:
        """计算两条记忆的余弦相似度。"""
        if a.embedding and b.embedding:
            vec_a = np.array(a.embedding)
            vec_b = np.array(b.embedding)
            norm = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
            if norm > 0:
                return float(np.dot(vec_a, vec_b) / norm)

        # 降级到文本 Jaccard
        tokens_a = set(a.content.lower().split())
        tokens_b = set(b.content.lower().split())
        if not tokens_a or not tokens_b:
            return 0.0
        return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            decision_counts = defaultdict(int)
            for log_entry in self._update_log[-1000:]:
                decision_counts[log_entry["decision"]] += 1
            return {
                "memory_count": len(self._memory_store),
                "total_updates": len(self._update_log),
                "decision_distribution": dict(decision_counts),
            }


class ClosedLoopPlanner:
    """闭环规划器

    从情景记忆中检索相似成功轨迹，引导当前任务规划：
      1. 检索 top-k 相似轨迹
      2. 提取成功模式（state → action 序列）
      3. 为当前任务生成规划建议
    """

    TOP_K = 5
    SUCCESS_THRESHOLD = 0.0

    def __init__(self):
        self._lock = threading.RLock()
        self._episodic_store: List[EpisodeTrace] = []
        self._planning_log: List[Dict[str, Any]] = []

    def store_episode(self, trace: EpisodeTrace) -> None:
        """存储情景轨迹。"""
        with self._lock:
            self._episodic_store.append(trace)
            if len(self._episodic_store) > 10000:
                self._episodic_store = self._episodic_store[-5000:]

    def retrieve_similar(
        self,
        current_state: Dict[str, Any],
        task_type: TaskType,
        top_k: int = TOP_K,
    ) -> List[EpisodeTrace]:
        """检索与当前状态最相似的轨迹。"""
        with self._lock:
            candidates = [
                ep for ep in self._episodic_store
                if ep.task_type == task_type and ep.outcome == "success"
            ]

            if not candidates:
                # 降级：也考虑失败任务（避坑）
                candidates = [
                    ep for ep in self._episodic_store
                    if ep.task_type == task_type
                ]

            scored = []
            for ep in candidates:
                score = self._state_similarity(current_state, ep.state_sequence[0] if ep.state_sequence else {})
                scored.append((score, ep))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [ep for _, ep in scored[:top_k]]

    def plan_action(
        self,
        current_state: Dict[str, Any],
        task_type: TaskType,
        top_k: int = TOP_K,
    ) -> Dict[str, Any]:
        """基于相似轨迹生成行动建议。"""
        with self._lock:
            similar = self.retrieve_similar(current_state, task_type, top_k)

            action_suggestions: List[str] = []
            for ep in similar:
                if ep.action_sequence:
                    action_suggestions.extend(ep.action_sequence[:3])

            # 去重并保持顺序
            seen = set()
            unique_actions = []
            for a in action_suggestions:
                if a not in seen:
                    seen.add(a)
                    unique_actions.append(a)

            plan = {
                "task_type": task_type.value,
                "similar_traces_found": len(similar),
                "suggested_actions": unique_actions[:10],
                "avg_similarity": (
                    float(np.mean([ep.similarity_score for ep in similar])) if similar else 0.0
                ),
                "risk_flag": len(similar) == 0,
            }

            self._planning_log.append(plan)
            return plan

    def _state_similarity(
        self, state_a: Dict[str, Any], state_b: Dict[str, Any]
    ) -> float:
        """计算状态相似度（Jaccard over keys + normalized value match）。"""
        if not state_a or not state_b:
            return 0.0

        common_keys = set(state_a.keys()) & set(state_b.keys())
        if not common_keys:
            return 0.0

        matches = 0
        for key in common_keys:
            if str(state_a[key]) == str(state_b[key]):
                matches += 1

        return matches / max(len(set(state_a.keys()) | set(state_b.keys())), 1)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            success_count = sum(1 for ep in self._episodic_store if ep.outcome == "success")
            return {
                "stored_episodes": len(self._episodic_store),
                "success_rate": (
                    success_count / max(len(self._episodic_store), 1)
                ),
                "plans_generated": len(self._planning_log),
            }


class MemoryFusion:
    """四种记忆的融合器

    按任务类型自适应加权融合四种记忆（空间/时间/情景/语义）。
    """

    # 任务类型 → 四种记忆权重
    TASK_WEIGHTS: Dict[TaskType, Tuple[float, float, float, float]] = {
        TaskType.NAVIGATION:   (0.40, 0.10, 0.30, 0.20),   # 空间为主
        TaskType.DIALOGUE:     (0.05, 0.30, 0.15, 0.50),   # 语义为主
        TaskType.PLANNING:     (0.10, 0.15, 0.45, 0.30),   # 情景为主
        TaskType.Q_A:          (0.05, 0.05, 0.10, 0.80),   # 语义绝对主导
        TaskType.MANIPULATION: (0.35, 0.10, 0.35, 0.20),   # 空间+情景
        TaskType.CODE:         (0.05, 0.05, 0.10, 0.80),   # 语义主导
        TaskType.GENERAL:      (0.25, 0.25, 0.25, 0.25),   # 等权
    }

    def __init__(self, strategy: FusionStrategy = FusionStrategy.TASK_ADAPTIVE):
        self._lock = threading.RLock()
        self._strategy = strategy
        self._fusions: List[FusionResult] = []

    def fuse(
        self,
        spatial_entries: List[MemoryEntry],
        temporal_entries: List[MemoryEntry],
        episodic_entries: List[MemoryEntry],
        semantic_entries: List[MemoryEntry],
        task_type: TaskType = TaskType.GENERAL,
    ) -> FusionResult:
        """融合四种记忆。"""
        with self._lock:
            if self._strategy == FusionStrategy.TASK_ADAPTIVE:
                w_sp, w_tm, w_ep, w_sm = self.TASK_WEIGHTS.get(
                    task_type, (0.25, 0.25, 0.25, 0.25)
                )
            elif self._strategy == FusionStrategy.CONFIDENCE_WEIGHTED:
                total_conf = (
                    sum(e.confidence for e in spatial_entries)
                    + sum(e.confidence for e in temporal_entries)
                    + sum(e.confidence for e in episodic_entries)
                    + sum(e.confidence for e in semantic_entries)
                ) or 1.0
                w_sp = sum(e.confidence for e in spatial_entries) / total_conf
                w_tm = sum(e.confidence for e in temporal_entries) / total_conf
                w_ep = sum(e.confidence for e in episodic_entries) / total_conf
                w_sm = sum(e.confidence for e in semantic_entries) / total_conf
            else:
                w_sp = w_tm = w_ep = w_sm = 0.25

            # 按权重合并 (加权去重)
            fused = []
            all_entries = (
                [(e, w_sp, MemoryType.SPATIAL) for e in spatial_entries]
                + [(e, w_tm, MemoryType.TEMPORAL) for e in temporal_entries]
                + [(e, w_ep, MemoryType.EPISODIC) for e in episodic_entries]
                + [(e, w_sm, MemoryType.SEMANTIC) for e in semantic_entries]
            )

            for entry, weight, mtype in all_entries:
                entry.confidence *= weight
                fused.append(entry)

            # 按融合后置信度排序
            fused.sort(key=lambda e: e.confidence, reverse=True)

            avg_conf = float(np.mean([e.confidence for e in fused])) if fused else 0.0

            result = FusionResult(
                result_id=str(uuid.uuid4())[:12],
                task_type=task_type,
                spatial_weight=w_sp,
                temporal_weight=w_tm,
                episodic_weight=w_ep,
                semantic_weight=w_sm,
                fused_entries=fused,
                confidence=avg_conf,
            )
            self._fusions.append(result)
            return result

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_fusions": len(self._fusions),
                "strategy": self._strategy.value,
                "avg_confidence": (
                    float(np.mean([f.confidence for f in self._fusions[-100:]]))
                    if self._fusions else 0.0
                ),
            }


class ParallelMemoryNexus:
    """脑启式并行记忆架构统一调度层

    并行触发四种记忆模块的更新/检索/合并：
      - 空间记忆：SpatialMemoryAdapter
      - 语义记忆：VLMBasedSemanticUpdater
      - 情景记忆：ClosedLoopPlanner (轨迹存储 + 检索)
      - 时间记忆：内置时间线索引

    使用 ThreadPoolExecutor 实现并行调度。
    """

    MAX_WORKERS = 4

    def __init__(self):
        self._lock = threading.RLock()
        self._spatial = SpatialMemoryAdapter()
        self._semantic_updater = VLMBasedSemanticUpdater()
        self._planner = ClosedLoopPlanner()
        self._fusion = MemoryFusion()
        self._temporal_index: Dict[str, List[MemoryEntry]] = defaultdict(list)
        self._executor = ThreadPoolExecutor(max_workers=self.MAX_WORKERS)
        self._version: str = "P14-4/v1.0"

    # ── 并行更新 ──────────────────────────────────────────────────

    def parallel_ingest(
        self, entries: List[MemoryEntry]
    ) -> Dict[str, List[Tuple[UpdateDecision, Optional[MemoryEntry]]]]:
        """并行摄入记忆到四种子系统。"""
        results: Dict[str, List[Tuple[UpdateDecision, Optional[MemoryEntry]]]] = {
            "semantic": [],
            "temporal": [],
        }

        with self._lock, self._executor as executor:
            futures = {}
            for entry in entries:
                if entry.memory_type == MemoryType.SEMANTIC:
                    fut = executor.submit(self._semantic_updater.ingest, entry)
                    futures[fut] = ("semantic", entry)
                elif entry.memory_type == MemoryType.TEMPORAL:
                    fut = executor.submit(self._ingest_temporal, entry)
                    futures[fut] = ("temporal", entry)
                elif entry.memory_type == MemoryType.SPATIAL:
                    fut = executor.submit(self._ingest_spatial, entry)
                    futures[fut] = ("spatial", entry)
                elif entry.memory_type == MemoryType.EPISODIC:
                    fut = executor.submit(self._ingest_episodic, entry)
                    futures[fut] = ("episodic", entry)

            for fut in as_completed(futures):
                category, _ = futures[fut]
                try:
                    result = fut.result()
                    if result is not None:
                        if category in results:
                            results[category].append(result)
                except Exception as e:
                    logger.error("Parallel ingest failed for %s: %s", category, e)

        return results

    def _ingest_temporal(self, entry: MemoryEntry) -> Tuple[UpdateDecision, Optional[MemoryEntry]]:
        """时间记忆摄入。"""
        with self._lock:
            self._temporal_index[entry.memory_type.value].append(entry)
            # 保持时间排序
            self._temporal_index[entry.memory_type.value].sort(key=lambda e: e.timestamp)
            return (UpdateDecision.ADD, None)

    def _ingest_spatial(self, entry: MemoryEntry) -> None:
        """空间记忆摄入（占位）。"""
        pass

    def _ingest_episodic(self, entry: MemoryEntry) -> None:
        """情景记忆摄入——转换为 EpisodeTrace。"""
        trace = EpisodeTrace(
            trace_id=entry.entry_id,
            task_type=TaskType.GENERAL,
            state_sequence=entry.metadata.get("state_sequence", []),
            action_sequence=entry.metadata.get("action_sequence", []),
            outcome=entry.metadata.get("outcome", "unknown"),
            reward=entry.metadata.get("reward", 0.0),
            timestamp=entry.timestamp,
        )
        self._planner.store_episode(trace)

    # ── 并行检索 ──────────────────────────────────────────────────

    def parallel_retrieve(
        self,
        query: str,
        task_type: TaskType = TaskType.GENERAL,
        top_k: int = 10,
    ) -> FusionResult:
        """并行检索并融合四种记忆。"""
        spatial_results: List[MemoryEntry] = []
        temporal_results: List[MemoryEntry] = []
        episodic_results: List[MemoryEntry] = []
        semantic_results: List[MemoryEntry] = []

        with self._lock, self._executor as executor:
            # 语义检索
            semantic_results = [
                e for e in self._semantic_updater._memory_store.values()
                if query.lower() in e.content.lower()
            ][:top_k]

            # 时间检索
            temporal_results = self._temporal_index.get(
                MemoryType.TEMPORAL.value, []
            )[-top_k:]

            # 情景检索
            plan = self._planner.plan_action({"query": query}, task_type, top_k)
            # 从 planner 获取的相似轨迹转为 entries
            similar_traces = self._planner.retrieve_similar({"query": query}, task_type, top_k)
            episodic_results = [
                MemoryEntry(
                    entry_id=t.trace_id,
                    memory_type=MemoryType.EPISODIC,
                    content=f"Task: {t.task_type.value}, Outcome: {t.outcome}",
                    confidence=t.reward,
                )
                for t in similar_traces
            ]

        return self._fusion.fuse(
            spatial_results,
            temporal_results,
            episodic_results,
            semantic_results,
            task_type,
        )

    def plan_with_context(
        self, current_state: Dict[str, Any], task_type: TaskType
    ) -> Dict[str, Any]:
        """使用完整记忆上下文做任务规划。"""
        with self._lock:
            plan = self._planner.plan_action(current_state, task_type)
            fused = self.parallel_retrieve(str(current_state), task_type, top_k=5)
            plan["fused_memory_confidence"] = fused.confidence
            plan["fused_entry_count"] = len(fused.fused_entries)
            plan["fusion_weights"] = fused.to_dict()
            return plan

    # ── 属性 ───────────────────────────────────────────────────────

    @property
    def spatial(self) -> SpatialMemoryAdapter:
        return self._spatial

    @property
    def semantic_updater(self) -> VLMBasedSemanticUpdater:
        return self._semantic_updater

    @property
    def planner(self) -> ClosedLoopPlanner:
        return self._planner

    @property
    def fusion(self) -> MemoryFusion:
        return self._fusion

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "version": self._version,
                "spatial": self._spatial.statistics(),
                "semantic": self._semantic_updater.statistics(),
                "planner": self._planner.statistics(),
                "fusion": self._fusion.statistics(),
                "temporal_entries": sum(
                    len(v) for v in self._temporal_index.values()
                ),
            }
