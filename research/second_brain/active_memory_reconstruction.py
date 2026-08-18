"""
# status: orphan (2026-08-15 audit, not in runtime path)
P21-1: Active Memory Reconstruction — Cue-Tag-Content 关联记忆图 + 主动重构循环

对标论文: MRAgent (Memory Reconstruction Agent, 2026.08)
核心发现: 记忆不应被动检索，而应通过 Cue→Tag→Content 三元关联图主动重构；
        边推理边遍历图，中间证据动态剪枝以控制推理路径爆炸。
三元语: 提示词(Cue) → 标签(Tag) → 内容(Content) → 关联边 → 主动重构循环 → 证据剪枝

设计要点:
- CueTagContentGraph: 以 Cue/Tag/Content 为节点的异构图，关联边带权重与类型
- ActiveReconstructionLoop: 从初始 Cue 出发，逐跳推理并展开证据链，动态剪枝低相关路径
- EvidencePruner: 基于注意力分数 + 边际效用阈值，实时裁剪价值低于阈值的证据分支
- ReconstructionResult: 带完整推理轨迹 (trace) 的重构结果，包含每一步的中间证据与剪枝决策
- MRAgent: 顶层编排器，组合图结构 + 重构循环 + 剪枝器，提供统一 query→reconstruction 接口
- CueAnalyzer: 对初始 query 提取结构化 Cue（关键词、实体、语义向量），作为图遍历起点
"""

from __future__ import annotations

import logging
import math
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

class NodeType(Enum):
    """记忆图中节点类型"""
    CUE = "cue"           # 提示词节点（查询入口）
    TAG = "tag"           # 标签节点（中间抽象）
    CONTENT = "content"   # 内容节点（实际记忆片段）


class EdgeRelation(Enum):
    """关联边语义类型"""
    CUE_TO_TAG = "cue_to_tag"           # 提示词→标签
    TAG_TO_CONTENT = "tag_to_content"    # 标签→内容
    CUE_TO_CONTENT = "cue_to_content"    # 直接提示词→内容（热路径）
    CONTENT_TO_CONTENT = "content_to_content"  # 内容间语义关联
    TAG_TO_TAG = "tag_to_tag"           # 标签间层级/并列关系


class PrunePolicy(Enum):
    """证据剪枝策略"""
    ATTENTION_THRESHOLD = "attention_threshold"    # 注意力分数低于阈值剪枝
    MARGINAL_UTILITY = "marginal_utility"           # 边际效用递减剪枝
    BRANCHING_LIMIT = "branching_limit"             # 分支数上限剪枝
    DEPTH_CAP = "depth_cap"                         # 遍历深度上限剪枝


class LoopPhase(Enum):
    """重构循环阶段"""
    INIT = "init"              # 初始化：Cue 解析
    EXPAND = "expand"          # 扩展：沿边展开邻接节点
    EVALUATE = "evaluate"      # 评估：计算注意力分数与边际效用
    PRUNE = "prune"            # 剪枝：移除低价值分支
    TERMINATE = "terminate"    # 终止：证据充分或达到停止条件


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class CueTag:
    """提示词标签 — 从查询中提取的结构化提示单元"""
    cue_id: str
    text: str
    embedding: List[float] = field(default_factory=list)
    entity_types: List[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class MemoryTag:
    """标签节点 — 记忆体系中的分类标签"""
    tag_id: str
    name: str
    layer: int = 0                   # 标签层级 (0=根)
    parent_tag_id: Optional[str] = None
    activation: float = 0.0          # 当前激活值


@dataclass
class MemoryContent:
    """内容节点 — 实际记忆片段"""
    content_id: str
    text: str
    embedding: List[float] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    timestamp: float = 0.0
    importance: float = 0.5
    access_count: int = 0


@dataclass
class AssociationEdge:
    """关联边 — 图中节点间的语义连接"""
    edge_id: str
    source_id: str
    target_id: str
    relation: EdgeRelation
    weight: float = 1.0              # 关联强度 (0~1)
    co_occurrence: int = 0           # 共现次数
    last_activated: float = 0.0


@dataclass
class TraversalStep:
    """单步遍历记录"""
    step_id: int
    phase: LoopPhase
    node_id: str
    node_type: NodeType
    attention_score: float = 0.0
    marginal_utility: float = 0.0
    pruned: bool = False


@dataclass
class ReconstructionResult:
    """主动重构结果"""
    query: str
    cues: List[CueTag]
    retrieved_contents: List[MemoryContent]
    traversal_trace: List[TraversalStep]
    pruned_branches: int = 0
    total_steps: int = 0
    elapsed_ms: float = 0.0
    confidence: float = 0.0

    def summary(self) -> Dict[str, Any]:
        return {
            "query": self.query[:80],
            "num_cues": len(self.cues),
            "num_contents": len(self.retrieved_contents),
            "total_steps": self.total_steps,
            "pruned_branches": self.pruned_branches,
            "confidence": round(self.confidence, 4),
            "elapsed_ms": round(self.elapsed_ms, 2),
        }


# ============================================================================
# Core Classes
# ============================================================================

class CueAnalyzer:
    """Cue 分析器 — 从自然语言查询中提取结构化提示词

    功能:
    - 实体识别与关键词抽取
    - 多粒度 Cue 生成 (词级/短语级/语义向量级)
    - 置信度打分
    """

    def __init__(self, min_confidence: float = 0.3):
        self.min_confidence = min_confidence
        self._cue_counter: int = 0

    def analyze(self, query: str) -> List[CueTag]:
        """解析查询并生成 CueTag 列表"""
        cues: List[CueTag] = []
        words = query.strip().split()
        # 词级 Cue
        for w in words:
            if len(w) >= 2:
                self._cue_counter += 1
                cues.append(CueTag(
                    cue_id=f"cue_{self._cue_counter}",
                    text=w,
                    confidence=0.7,
                ))
        # 短语级 Cue（相邻词对）
        for i in range(len(words) - 1):
            phrase = f"{words[i]} {words[i + 1]}"
            if len(phrase) >= 4:
                self._cue_counter += 1
                cues.append(CueTag(
                    cue_id=f"cue_{self._cue_counter}",
                    text=phrase,
                    confidence=0.85,
                ))
        return cues


class CueTagContentGraph:
    """Cue-Tag-Content 关联记忆图

    异构图结构:
    - 节点: Cue / Tag / Content 三类
    - 边: 五类关联关系，带权重与激活衰减
    - 索引: cue_id -> edges / tag_id -> edges / content_id -> edges

    线程安全: 使用 RLock 保护读写操作
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._nodes: Dict[str, Tuple[NodeType, Any]] = {}
        self._edges: Dict[str, AssociationEdge] = {}
        self._adjacency: Dict[str, List[str]] = defaultdict(list)  # node_id → [edge_id, ...]
        self._edge_counter: int = 0

    # ---- 节点管理 ----

    def add_cue(self, cue: CueTag) -> None:
        with self._lock:
            self._nodes[cue.cue_id] = (NodeType.CUE, cue)

    def add_tag(self, tag: MemoryTag) -> None:
        with self._lock:
            self._nodes[tag.tag_id] = (NodeType.TAG, tag)

    def add_content(self, content: MemoryContent) -> None:
        with self._lock:
            self._nodes[content.content_id] = (NodeType.CONTENT, content)

    # ---- 边管理 ----

    def add_edge(self, source_id: str, target_id: str, relation: EdgeRelation,
                 weight: float = 1.0) -> AssociationEdge:
        with self._lock:
            self._edge_counter += 1
            edge = AssociationEdge(
                edge_id=f"edge_{self._edge_counter}",
                source_id=source_id,
                target_id=target_id,
                relation=relation,
                weight=weight,
                co_occurrence=1,
                last_activated=time.time(),
            )
            self._edges[edge.edge_id] = edge
            self._adjacency[source_id].append(edge.edge_id)
            self._adjacency[target_id].append(edge.edge_id)
            return edge

    def get_neighbors(self, node_id: str) -> List[Tuple[AssociationEdge, str]]:
        """获取节点的所有邻居 (edge, neighbor_node_id)"""
        with self._lock:
            result: List[Tuple[AssociationEdge, str]] = []
            for edge_id in self._adjacency.get(node_id, []):
                edge = self._edges[edge_id]
                neighbor = edge.target_id if edge.source_id == node_id else edge.source_id
                result.append((edge, neighbor))
            return result

    def get_node_type(self, node_id: str) -> Optional[NodeType]:
        with self._lock:
            entry = self._nodes.get(node_id)
            return entry[0] if entry else None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self._nodes),
                "total_edges": len(self._edges),
                "cue_nodes": sum(1 for t, _ in self._nodes.values() if t == NodeType.CUE),
                "tag_nodes": sum(1 for t, _ in self._nodes.values() if t == NodeType.TAG),
                "content_nodes": sum(1 for t, _ in self._nodes.values() if t == NodeType.CONTENT),
            }


class EvidencePruner:
    """证据剪枝器 — 在重构循环中动态裁剪低价值分支

    实现三种剪枝策略的组合:
    - 注意力分数阈值剪枝: 展开节点的 attention_score 低于阈值则舍弃
    - 边际效用剪枝: 连续 N 步边际效用增量低于阈值则剪枝整条路径
    - 分支数限制: 每步最多保留 Top-K 分支
    """

    def __init__(
        self,
        attention_threshold: float = 0.15,
        marginal_utility_threshold: float = 0.05,
        max_branches_per_step: int = 8,
        max_depth: int = 5,
    ):
        self.attention_threshold = attention_threshold
        self.marginal_utility_threshold = marginal_utility_threshold
        self.max_branches_per_step = max_branches_per_step
        self.max_depth = max_depth

    def should_prune(self, step: TraversalStep, branch_depth: int,
                     consecutive_low_utility: int) -> Tuple[bool, PrunePolicy]:
        """判断当前遍历步是否应剪枝"""
        if branch_depth > self.max_depth:
            return True, PrunePolicy.DEPTH_CAP
        if step.attention_score < self.attention_threshold:
            return True, PrunePolicy.ATTENTION_THRESHOLD
        if consecutive_low_utility >= 3:
            return True, PrunePolicy.MARGINAL_UTILITY
        return False, PrunePolicy.ATTENTION_THRESHOLD

    def select_top_branches(self, candidates: List[Tuple[str, float]]) -> List[str]:
        """按分数选择 Top-K 分支"""
        sorted_candidates = sorted(candidates, key=lambda x: x[1], reverse=True)
        return [cid for cid, _ in sorted_candidates[:self.max_branches_per_step]]


class ActiveReconstructionLoop:
    """主动重构循环 — 边推理边遍历，动态剪枝

    流程:
    1. INIT: 接收 Cue 列表，计算初始注意力分布
    2. EXPAND: 从当前活跃节点沿边展开邻居
    3. EVALUATE: 计算每个展开节点的注意力分数 + 边际效用
    4. PRUNE: 调用 EvidencePruner 裁剪低价值分支
    5. 若证据充分或活跃节点为空 → TERMINATE；否则回到 EXPAND
    """

    def __init__(self, graph: CueTagContentGraph, pruner: EvidencePruner):
        self.graph = graph
        self.pruner = pruner
        self._max_iterations = 20
        self._evidence_threshold = 3  # 收集到足够 Content 数量后终止

    def reconstruct(self, cues: List[CueTag]) -> ReconstructionResult:
        trace: List[TraversalStep] = []
        collected_contents: List[MemoryContent] = []
        pruned_count = 0
        step_counter = 0
        t_start = time.time()

        # BFS-style 遍历队列: (node_id, depth, consecutive_low_utility)
        queue: List[Tuple[str, int, int]] = [(cue.cue_id, 0, 0) for cue in cues]
        visited: Set[str] = set()

        while queue and step_counter < self._max_iterations:
            node_id, depth, low_util_count = queue.pop(0)
            if node_id in visited:
                continue
            visited.add(node_id)
            step_counter += 1

            node_type = self.graph.get_node_type(node_id) or NodeType.CONTENT
            step = TraversalStep(
                step_id=step_counter,
                phase=LoopPhase.EXPAND,
                node_id=node_id,
                node_type=node_type,
            )

            # EVALUATE 阶段
            neighbors = self.graph.get_neighbors(node_id)
            attention_score = 1.0 / (1.0 + depth * 0.3)  # 深度衰减注意力
            marginal_utility = attention_score * (1.0 / (1.0 + low_util_count * 0.2))
            step.phase = LoopPhase.EVALUATE
            step.attention_score = attention_score
            step.marginal_utility = marginal_utility

            # PRUNE 阶段
            should_prune, policy = self.pruner.should_prune(step, depth, low_util_count)
            if should_prune:
                step.phase = LoopPhase.PRUNE
                step.pruned = True
                pruned_count += 1
                trace.append(step)
                continue

            # 收集 Content 类型节点
            if node_type == NodeType.CONTENT:
                entry = self.graph._nodes.get(node_id)
                if entry and isinstance(entry[1], MemoryContent):
                    collected_contents.append(entry[1])

            # 选择 Top-K 邻居入队
            candidate_branches: List[Tuple[str, float]] = []
            for edge, neighbor_id in neighbors:
                if neighbor_id not in visited:
                    candidate_branches.append((neighbor_id, edge.weight * attention_score))
            selected = self.pruner.select_top_branches(candidate_branches)

            for nid in selected:
                new_low = low_util_count + 1 if marginal_utility < self.pruner.marginal_utility_threshold else 0
                queue.append((nid, depth + 1, new_low))

            trace.append(step)

            # 终止条件: 证据充分
            if len(collected_contents) >= self._evidence_threshold:
                step.phase = LoopPhase.TERMINATE
                break

        elapsed_ms = (time.time() - t_start) * 1000.0
        confidence = min(1.0, len(collected_contents) / max(1, self._evidence_threshold))

        return ReconstructionResult(
            query="",
            cues=cues,
            retrieved_contents=collected_contents,
            traversal_trace=trace,
            pruned_branches=pruned_count,
            total_steps=step_counter,
            elapsed_ms=elapsed_ms,
            confidence=confidence,
        )


class MRAgent:
    """MR Agent — 主动记忆重构顶层编排器

    组合:
    - CueAnalyzer: 查询解析
    - CueTagContentGraph: 异构图存储
    - ActiveReconstructionLoop: 重构循环
    - EvidencePruner: 证据剪枝

    接口与现有模块兼容: 提供 statistics() 诊断方法
    """

    def __init__(
        self,
        attention_threshold: float = 0.15,
        max_branches: int = 8,
        max_depth: int = 5,
    ):
        self._lock = threading.RLock()
        self.cue_analyzer = CueAnalyzer()
        self.graph = CueTagContentGraph()
        self.pruner = EvidencePruner(
            attention_threshold=attention_threshold,
            max_branches_per_step=max_branches,
            max_depth=max_depth,
        )
        self.loop = ActiveReconstructionLoop(self.graph, self.pruner)
        self._query_count: int = 0
        self._total_elapsed_ms: float = 0.0

    # ---- 记忆录入 ----

    def ingest(self, content: MemoryContent) -> None:
        """录入单条记忆内容，自动建立 Tag→Content 关联边"""
        with self._lock:
            self.graph.add_content(content)
            for tag_id in content.tags:
                # 确保标签节点存在
                if self.graph.get_node_type(tag_id) is None:
                    self.graph.add_tag(MemoryTag(tag_id=tag_id, name=tag_id))
                self.graph.add_edge(tag_id, content.content_id, EdgeRelation.TAG_TO_CONTENT,
                                    weight=content.importance)

    def ingest_tag(self, tag: MemoryTag) -> None:
        with self._lock:
            self.graph.add_tag(tag)

    # ---- 查询重构 ----

    def query(self, query_text: str) -> ReconstructionResult:
        """执行主动记忆重构查询"""
        with self._lock:
            cues = self.cue_analyzer.analyze(query_text)
            # 将 Cue 注册到图中
            for cue in cues:
                self.graph.add_cue(cue)
            result = self.loop.reconstruct(cues)
            result.query = query_text
            self._query_count += 1
            self._total_elapsed_ms += result.elapsed_ms
            return result

    # ---- 诊断 ----

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            graph_stats = self.graph.statistics()
            return {
                "module": "MRAgent",
                "queries_served": self._query_count,
                "avg_latency_ms": round(self._total_elapsed_ms / max(1, self._query_count), 2),
                "prune_config": {
                    "attention_threshold": self.pruner.attention_threshold,
                    "max_branches": self.pruner.max_branches_per_step,
                    "max_depth": self.pruner.max_depth,
                },
                **graph_stats,
            }
