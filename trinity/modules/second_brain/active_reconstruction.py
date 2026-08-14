"""
P12-5: Active Memory Reconstruction — 对标 MRAgent (ICML 2026)

实现 Cue-Tag-Content 三元组关联记忆图:
  - Cue: 检索触发线索 (感知输入/查询)
  - Tag: 语义标签 (主题/情感/任务类型)
  - Content: 完整记忆片段 (压缩/原始)
  - ActiveReconstructionLoop: 基于中间证据动态选择遍历方向并剪枝无关分支
  - reconstruct() 替代传统 retrieve(): 迭代式 evidence_guided_prune()
  - 输出重构后的情境记忆视图 (Contextualized Memory View)

关键收益: LoCoMo/LongMemEval 最高 +23%

Reference:
    MRAgent: Memory Reconstruction Agent for Long-Context Reasoning (ICML 2026)
    Cue-Tag-Content Memory Graph Architecture
"""

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════
# 枚举
# ══════════════════════════════════════════════════════════════════════

class TagCategory(Enum):
    """语义标签类别。"""
    TOPIC = "topic"           # 主题标签
    EMOTION = "emotion"       # 情感标签
    TASK = "task"             # 任务类型标签
    DOMAIN = "domain"         # 领域标签
    TEMPORAL = "temporal"     # 时间标签
    ENTITY = "entity"         # 实体标签


class TraversalDirection(Enum):
    """图遍历方向。"""
    FORWARD = "forward"         # 前向扩展（沿时间/因果）
    BACKWARD = "backward"       # 后向追溯
    LATERAL = "lateral"         # 横向关联（语义相似）
    HIERARCHICAL = "hierarchical"  # 层级钻取


class EvidenceStrength(Enum):
    """中间证据强度。"""
    STRONG = "strong"       # 强证据，保留并深入
    MODERATE = "moderate"   # 中等，标记待观察
    WEAK = "weak"           # 弱证据，可剪枝
    IRRELEVANT = "irrelevant"  # 无关，立即剪枝


# ══════════════════════════════════════════════════════════════════════
# 数据结构
# ══════════════════════════════════════════════════════════════════════

@dataclass
class CueTagNode:
    """Cue-Tag-Content 三元组节点。"""
    node_id: str
    cue: str                          # 检索触发线索
    tags: dict[TagCategory, list[str]] = field(default_factory=dict)  # 多维度标签
    content: str = ""                 # 完整记忆片段
    content_hash: str = ""            # 内容哈希
    confidence: float = 1.0           # 记忆置信度
    timestamp: float = field(default_factory=time.time)
    source_episode: str = ""          # 来源片段 ID
    embedding_hint: list[float] = field(default_factory=list)  # 嵌入向量提示

    def has_tag(self, category: TagCategory, value: str) -> bool:
        return value in self.tags.get(category, [])


@dataclass
class AssociationEdge:
    """关联边。"""
    edge_id: str
    source_id: str
    target_id: str
    relation_type: str                # "causal", "temporal", "semantic", "hierarchical"
    weight: float = 1.0
    evidence: list[str] = field(default_factory=list)  # 支持该关联的证据片段
    bidirectional: bool = False


@dataclass
class TraversalState:
    """遍历状态。"""
    current_node_id: str
    direction: TraversalDirection
    depth: int = 0
    accumulated_evidence: float = 0.0  # 累积证据得分
    path: list[str] = field(default_factory=list)  # 走过的节点路径
    pruned_branches: list[str] = field(default_factory=list)


@dataclass
class ReconstructedView:
    """重构后的情境记忆视图。"""
    query: str
    root_nodes: list[str]                # 入口节点
    retained_nodes: list[CueTagNode]     # 保留的节点
    retained_edges: list[AssociationEdge]  # 保留的边
    pruned_nodes: list[str]              # 被剪枝的节点
    traversal_log: list[dict]            # 遍历过程日志
    reconstruction_confidence: float     # 重构置信度
    context_summary: str = ""            # 情境摘要
    timestamp: float = field(default_factory=time.time)


@dataclass
class EvidenceSignal:
    """中间证据信号。"""
    node_id: str
    strength: EvidenceStrength
    score: float                        # 0~1 证据得分
    reason: str                         # 判定理由
    supporting_tags: dict[TagCategory, list[str]] = field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════
# Active Reconstruction Loop
# ══════════════════════════════════════════════════════════════════════

class ActiveReconstructionLoop:
    """主动记忆重构循环。

    基于中间证据动态选择遍历方向并剪枝无关分支。
    reconstruct() 替代传统 retrieve()，通过迭代式 evidence_guided_prune() 输出情境化视图。
    """

    def __init__(self, max_depth: int = 5, evidence_threshold: float = 0.3,
                 max_nodes: int = 200, expansion_factor: float = 3.0):
        self.max_depth = max_depth
        self.evidence_threshold = evidence_threshold
        self.max_nodes = max_nodes
        self.expansion_factor = expansion_factor  # 每轮扩展倍数
        self._nodes: dict[str, CueTagNode] = {}
        self._edges: dict[str, AssociationEdge] = {}
        self._adjacency: dict[str, list[str]] = defaultdict(list)
        self._tag_index: dict[TagCategory, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    # ── 图管理 ────────────────────────────────────────────────────

    def add_node(self, node: CueTagNode) -> None:
        self._nodes[node.node_id] = node
        for cat, values in node.tags.items():
            for v in values:
                self._tag_index[cat][v].append(node.node_id)

    def add_edge(self, edge: AssociationEdge) -> None:
        self._edges[edge.edge_id] = edge
        self._adjacency[edge.source_id].append(edge.target_id)
        if edge.bidirectional:
            self._adjacency[edge.target_id].append(edge.source_id)

    def get_node(self, node_id: str) -> CueTagNode | None:
        return self._nodes.get(node_id)

    def get_neighbors(self, node_id: str) -> list[str]:
        return self._adjacency.get(node_id, [])

    # ── 核心：reconstruct() ────────────────────────────────────────

    def reconstruct(self, query: str, cues: list[str],
                    initial_direction: TraversalDirection = TraversalDirection.FORWARD) -> ReconstructedView:
        """替代传统 retrieve() 的主动重构入口。

        Args:
            query: 用户查询
            cues: 检索触发线索列表
            initial_direction: 初始遍历方向

        Returns:
            ReconstructedView: 重构后的情境记忆视图
        """
        # 阶段 1: 线索锚定 — 根据 cues 找到入口节点
        entry_nodes = self._anchor_cues(cues)
        if not entry_nodes:
            return ReconstructedView(
                query=query,
                root_nodes=[],
                retained_nodes=[],
                retained_edges=[],
                pruned_nodes=[],
                traversal_log=[],
                reconstruction_confidence=0.0,
                context_summary="No nodes matched cues",
            )

        # 阶段 2: 主动重构循环
        visited: set[str] = set()
        retained_nodes: dict[str, CueTagNode] = {}
        retained_edges: dict[str, AssociationEdge] = {}
        pruned_nodes: list[str] = []
        traversal_log: list[dict] = []
        frontier: list[TraversalState] = [
            TraversalState(node_id=nid, direction=initial_direction, path=[nid])
            for nid in entry_nodes
        ]

        for depth in range(self.max_depth):
            if not frontier or len(retained_nodes) >= self.max_nodes:
                break

            next_frontier: list[TraversalState] = []

            for state in frontier:
                if state.node_id in visited:
                    continue
                visited.add(state.node_id)

                node = self._nodes.get(state.node_id)
                if node is None:
                    continue

                # 评估当前节点的证据强度
                evidence = self._evaluate_evidence(node, query, cues)

                if evidence.strength == EvidenceStrength.IRRELEVANT:
                    pruned_nodes.append(state.node_id)
                    traversal_log.append({
                        "node": state.node_id, "action": "pruned",
                        "reason": evidence.reason, "depth": depth,
                    })
                    continue

                if evidence.strength == EvidenceStrength.WEAK:
                    # 弱证据：保留但不扩展
                    retained_nodes[state.node_id] = node
                    traversal_log.append({
                        "node": state.node_id, "action": "retained_no_expand",
                        "reason": evidence.reason, "depth": depth,
                    })
                    continue

                # 强/中等证据：保留并扩展
                retained_nodes[state.node_id] = node

                # 证据引导剪枝：选择遍历方向
                neighbors = self.get_neighbors(state.node_id)
                scored_neighbors = self._score_neighbors(neighbors, query, cues, evidence)

                # 保留超过阈值的邻居作为下一轮 frontier
                for neighbor_id, score in scored_neighbors:
                    if score > self.evidence_threshold:
                        next_frontier.append(TraversalState(
                            current_node_id=neighbor_id,
                            direction=self._infer_direction(state.node_id, neighbor_id),
                            depth=depth + 1,
                            accumulated_evidence=state.accumulated_evidence + score,
                            path=state.path + [neighbor_id],
                        ))
                        # 保留边
                        for eid, edge in self._edges.items():
                            if edge.source_id == state.node_id and edge.target_id == neighbor_id:
                                retained_edges[eid] = edge
                    elif score > self.evidence_threshold * 0.5:
                        # 弱相关但保留节点
                        retained_nodes[neighbor_id] = self._nodes.get(neighbor_id)
                    else:
                        pruned_nodes.append(neighbor_id)

                traversal_log.append({
                    "node": state.node_id, "action": "expanded",
                    "neighbors_kept": len(scored_neighbors),
                    "neighbors_pruned": len(neighbors) - len(scored_neighbors),
                    "depth": depth,
                    "evidence_score": evidence.score,
                })

            frontier = next_frontier

        # 阶段 3: 计算重构置信度
        total_candidates = len(retained_nodes) + len(pruned_nodes)
        conf = len(retained_nodes) / max(total_candidates, 1)
        conf = min(1.0, conf * (1.0 + len(entry_nodes) * 0.1))

        return ReconstructedView(
            query=query,
            root_nodes=entry_nodes,
            retained_nodes=list(retained_nodes.values()),
            retained_edges=list(retained_edges.values()),
            pruned_nodes=pruned_nodes,
            traversal_log=traversal_log,
            reconstruction_confidence=round(conf, 4),
            context_summary=f"Reconstructed {len(retained_nodes)} nodes from {total_candidates} candidates "
                            f"(pruned {len(pruned_nodes)}, depth={self.max_depth})",
        )

    # ── Cue 锚定 ──────────────────────────────────────────────────

    def _anchor_cues(self, cues: list[str]) -> list[str]:
        """根据 cues 找到所有匹配的入口节点。"""
        matched: dict[str, float] = {}  # node_id -> match_score
        for cue in cues:
            cue_lower = cue.lower()
            for nid, node in self._nodes.items():
                if cue_lower in node.cue.lower():
                    matched[nid] = matched.get(nid, 0) + 1.0
                # 标签匹配
                for cat, values in node.tags.items():
                    for v in values:
                        if cue_lower in v.lower():
                            matched[nid] = matched.get(nid, 0) + 0.5

        # 按匹配分排序，取 top-k
        sorted_ids = sorted(matched, key=lambda x: matched[x], reverse=True)
        return sorted_ids[:min(10, len(sorted_ids))]

    # ── 证据评估 ──────────────────────────────────────────────────

    def _evaluate_evidence(self, node: CueTagNode, query: str,
                           original_cues: list[str]) -> EvidenceSignal:
        """评估节点相对于查询的证据强度。"""
        query_lower = query.lower()

        # 内容相关性
        content_hits = sum(1 for cue in original_cues if cue.lower() in node.content.lower())

        # 标签相关性
        tag_hits = 0
        supporting_tags: dict[TagCategory, list[str]] = {}
        for cat, values in node.tags.items():
            for v in values:
                if any(c.lower() in v.lower() for c in original_cues):
                    tag_hits += 1
                    if cat not in supporting_tags:
                        supporting_tags[cat] = []
                    supporting_tags[cat].append(v)

        # 综合得分
        base_score = (content_hits * 0.6 + tag_hits * 0.4) / max(len(original_cues), 1)
        score = min(1.0, base_score * node.confidence)

        if score >= 0.7:
            strength = EvidenceStrength.STRONG
            reason = f"Strong match: content_hits={content_hits}, tag_hits={tag_hits}"
        elif score >= 0.4:
            strength = EvidenceStrength.MODERATE
            reason = f"Moderate match: content_hits={content_hits}, tag_hits={tag_hits}"
        elif score >= 0.15:
            strength = EvidenceStrength.WEAK
            reason = f"Weak match: content_hits={content_hits}, tag_hits={tag_hits}"
        else:
            strength = EvidenceStrength.IRRELEVANT
            reason = f"Irrelevant: score={score:.2f}"

        return EvidenceSignal(
            node_id=node.node_id,
            strength=strength,
            score=score,
            reason=reason,
            supporting_tags=supporting_tags,
        )

    # ── 邻居评分 ──────────────────────────────────────────────────

    def _score_neighbors(self, neighbor_ids: list[str], query: str,
                         cues: list[str], parent_evidence: EvidenceSignal) -> list[tuple[str, float]]:
        """对邻居节点打分，返回 (node_id, score) 排序列表。"""
        scored = []
        for nid in neighbor_ids:
            node = self._nodes.get(nid)
            if node is None:
                continue
            evidence = self._evaluate_evidence(node, query, cues)
            # 邻居得分受父节点证据加成
            boosted = evidence.score * (1.0 + parent_evidence.score * 0.3)
            scored.append((nid, min(1.0, boosted)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    # ── 方向推断 ──────────────────────────────────────────────────

    def _infer_direction(self, from_id: str, to_id: str) -> TraversalDirection:
        """推断从 from 到 to 的遍历方向。"""
        from_node = self._nodes.get(from_id)
        to_node = self._nodes.get(to_id)
        if from_node and to_node:
            if to_node.timestamp > from_node.timestamp:
                return TraversalDirection.FORWARD
            elif to_node.timestamp < from_node.timestamp:
                return TraversalDirection.BACKWARD
        return TraversalDirection.LATERAL

    # ── 证据引导剪枝 ──────────────────────────────────────────────

    def evidence_guided_prune(self, candidates: list[str], query: str,
                              cues: list[str]) -> tuple[list[str], list[str]]:
        """基于中间证据剪枝无关分支。

        Returns:
            (retained_ids, pruned_ids)
        """
        retained = []
        pruned = []
        for nid in candidates:
            node = self._nodes.get(nid)
            if node is None:
                pruned.append(nid)
                continue
            evidence = self._evaluate_evidence(node, query, cues)
            if evidence.strength in (EvidenceStrength.STRONG, EvidenceStrength.MODERATE):
                retained.append(nid)
            else:
                pruned.append(nid)
        return retained, pruned

    # ── 统计 ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            "max_depth": self.max_depth,
            "evidence_threshold": self.evidence_threshold,
            "tag_categories": {cat.value: len(idx) for cat, idx in self._tag_index.items()},
        }


# ══════════════════════════════════════════════════════════════════════
# 模块自测
# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    loop = ActiveReconstructionLoop(max_depth=3, evidence_threshold=0.25)

    # 构建 CTC 记忆图
    t0 = time.time()
    nodes = [
        CueTagNode("n1", "Python async programming", {
            TagCategory.TOPIC: ["python", "async"], TagCategory.TASK: ["coding"]
        }, "async def with await patterns", timestamp=t0 - 500),
        CueTagNode("n2", "FastAPI deployment", {
            TagCategory.TOPIC: ["web", "deployment"], TagCategory.TASK: ["devops"]
        }, "Deploy with uvicorn and gunicorn", timestamp=t0 - 400),
        CueTagNode("n3", "Database connection pooling", {
            TagCategory.TOPIC: ["database", "performance"], TagCategory.TASK: ["backend"]
        }, "SQLAlchemy pool_size and max_overflow", timestamp=t0 - 300),
        CueTagNode("n4", "Distributed tracing with OpenTelemetry", {
            TagCategory.TOPIC: ["observability", "tracing"], TagCategory.TASK: ["monitoring"]
        }, "OTLP exporter with Jaeger backend", timestamp=t0 - 200),
        CueTagNode("n5", "Python CPU profiling", {
            TagCategory.TOPIC: ["python", "performance"], TagCategory.TASK: ["optimization"]
        }, "Use cProfile and snakeviz for profiling", timestamp=t0 - 100),
    ]
    for n in nodes:
        loop.add_node(n)

    loop.add_edge(AssociationEdge("e1", "n1", "n2", "temporal", weight=0.9, bidirectional=False))
    loop.add_edge(AssociationEdge("e2", "n1", "n3", "semantic", weight=0.7, bidirectional=True))
    loop.add_edge(AssociationEdge("e3", "n2", "n4", "causal", weight=0.8, bidirectional=False))
    loop.add_edge(AssociationEdge("e4", "n3", "n5", "semantic", weight=0.6, bidirectional=False))

    print("=" * 60)
    print("Active Memory Reconstruction — Self Test")
    print("=" * 60)

    view = loop.reconstruct(
        query="Python performance optimization",
        cues=["python", "performance", "profiling"],
        initial_direction=TraversalDirection.FORWARD,
    )

    print(f"\n[Reconstructed View]")
    print(f"  Retained nodes: {len(view.retained_nodes)}")
    print(f"  Pruned nodes: {len(view.pruned_nodes)}")
    print(f"  Retained edges: {len(view.retained_edges)}")
    print(f"  Confidence: {view.reconstruction_confidence:.2%}")
    for node in view.retained_nodes:
        print(f"  - [{node.node_id}] {node.cue} (conf={node.confidence})")
    for nid in view.pruned_nodes:
        print(f"  - [pruned] {nid}")

    print(f"\n[Traversal Log] {len(view.traversal_log)} entries")
    for entry in view.traversal_log[:5]:
        print(f"  {entry['node']}: {entry['action']} @ depth={entry.get('depth','?')}")

    # Evidence-guided prune
    retained, pruned = loop.evidence_guided_prune(
        ["n1", "n2", "n3", "n4", "n5"],
        query="deployment and observability",
        cues=["deployment", "observability", "tracing"],
    )
    print(f"\n[Evidence-Guided Prune] retained={retained}, pruned={pruned}")
