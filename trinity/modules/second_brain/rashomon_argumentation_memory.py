"""
# status: orphan (2026-08-15 audit, not in runtime path)
P22-7: Rashomon Argumentation Memory — Multi-Perspective Argumentation-Driven Retrieval
========================================================================================

对标论文：Rashomon Memory: Argumentation-Driven Retrieval for Multi-Perspective Agent Memory
(Warsaw University of Technology, arXiv 2604.03588, April 2026).

设计要点：
  - 目标条件化 Agent 独立编码同一事件（各自本体/KG）
  - 查询时多视角提议 → 非对称领域知识互评 → Dung 论辩语义裁决
  - 攻击图作为可解释性输出（记录选择/替代/拒绝理由）
  - 三种检索模式（选择/组合/冲突暴露），从攻击图拓扑涌现

核心组件：
  - GoalConditionedPerspective:  目标条件化视角
  - RashomonMemory:              多视角论辩记忆总控
  - ArgumentationResolver:       Dung 论辩语义裁决器
  - AttackGraph:                 攻击图（可解释性输出）
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

class PersRetrievalMode(Enum):
    """检索模式（Rashomon 三种模式）。"""
    SELECTION = "selection"         # 选择最强视角（赢家通吃）
    COMPOSITION = "composition"     # 组合多视角（并集融合）
    CONFLICT_SURFACING = "conflict_surfacing"  # 冲突暴露（暴露真正分歧）


class ArgumentStatus(Enum):
    """Dung 论辩语义中的论证状态。"""
    IN = "in"               # 在稳定扩展中（存活）
    OUT = "out"             # 被攻击击败（拒绝）
    UNDECIDED = "undecided"  # 未决


class AttackType(Enum):
    """攻击边类型。"""
    REBUTTAL = "rebuttal"            # 结论反驳
    UNDERMINING = "undermining"      # 前提削弱
    UNDER_CUTTING = "undercutting"   # 推理切断
    DOMAIN_OVERRIDE = "domain_override"  # 领域知识优先


class PerspectiveGoal(Enum):
    """目标条件化视角的目标类型。"""
    STRATEGIC = "strategic"          # 战略目标
    COMPLIANCE = "compliance"        # 合规目标
    EFFICIENCY = "efficiency"        # 效率目标
    RELATIONSHIP = "relationship"    # 关系目标
    INNOVATION = "innovation"        # 创新目标
    SECURITY = "security"            # 安全目标


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class KnowledgeEntity:
    """视角下的知识实体。"""
    entity_id: str
    entity_type: str
    properties: Dict[str, Any] = field(default_factory=dict)
    embedding: Optional[List[float]] = None


@dataclass
class KnowledgeRelation:
    """视角下的知识关系。"""
    relation_id: str
    source: str
    target: str
    relation_type: str
    weight: float = 1.0


@dataclass
class PerspectiveOntology:
    """视角专属本体。"""
    perspective_id: str
    goal: PerspectiveGoal
    entities: Dict[str, KnowledgeEntity] = field(default_factory=dict)
    relations: List[KnowledgeRelation] = field(default_factory=list)
    priority_weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class EncodedEvent:
    """目标条件化编码的同一事件。"""
    event_id: str
    raw_description: str
    perspectives: Dict[str, PerspectiveOntology] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class PerspectiveProposal:
    """视角提议（查询时）。"""
    proposal_id: str
    perspective_id: str
    goal: PerspectiveGoal
    content: str                     # 解释/解读
    confidence: float                # 自我评估置信度
    supporting_entities: List[str] = field(default_factory=list)
    domain_knowledge_tags: List[str] = field(default_factory=list)


@dataclass
class AttackEdge:
    """攻击边（论辩攻击关系）。"""
    attacker_proposal_id: str
    target_proposal_id: str
    attack_type: AttackType
    reason: str
    strength: float = 1.0            # 攻击强度
    domain_justification: str = ""


@dataclass
class ArgumentNode:
    """论证节点。"""
    proposal: PerspectiveProposal
    status: ArgumentStatus = ArgumentStatus.UNDECIDED
    attackers: List[str] = field(default_factory=list)
    victims: List[str] = field(default_factory=list)
    grounded_label: bool = False     # 有根基论证


@dataclass
class AttackGraph:
    """攻击图（可解释性输出）。"""
    graph_id: str
    event_id: str
    nodes: Dict[str, ArgumentNode] = field(default_factory=dict)
    attacks: List[AttackEdge] = field(default_factory=list)
    retrieval_mode: PersRetrievalMode = PersRetrievalMode.CONFLICT_SURFACING
    selected_proposals: List[str] = field(default_factory=list)
    rejected_proposals: List[str] = field(default_factory=list)
    rejection_reasons: Dict[str, str] = field(default_factory=dict)
    explanation: str = ""


# ============================================================================
# Core Components
# ============================================================================

class GoalConditionedPerspective:
    """目标条件化视角。

    每个视角按自身目标优先级，独立编码同一事件到专属本体/KG。
    """

    def __init__(self, perspective_id: str, goal: PerspectiveGoal,
                 priority_weights: Dict[str, float] = None):
        self._lock = threading.RLock()
        self.ontology = PerspectiveOntology(
            perspective_id=perspective_id,
            goal=goal,
            priority_weights=priority_weights or {},
        )

    def encode(self, event: EncodedEvent) -> PerspectiveOntology:
        """将事件编码到本视角的本体。"""
        with self._lock:
            # 根据目标优先级过滤和加权
            event_keywords = set(event.raw_description.lower().split())

            for kw in event_keywords:
                weight = self.ontology.priority_weights.get(kw, 0.5)
                if weight > 0.3:
                    entity = KnowledgeEntity(
                        entity_id=str(uuid.uuid4())[:8],
                        entity_type="event_concept",
                        properties={"keyword": kw, "weight": weight},
                    )
                    self.ontology.entities[entity.entity_id] = entity

            # 创建跨实体关系
            entity_ids = list(self.ontology.entities.keys())
            for i in range(len(entity_ids)):
                for j in range(i + 1, len(entity_ids)):
                    rel = KnowledgeRelation(
                        relation_id=str(uuid.uuid4())[:8],
                        source=entity_ids[i],
                        target=entity_ids[j],
                        relation_type="co_occurrence",
                        weight=0.5,
                    )
                    self.ontology.relations.append(rel)

            event.perspectives[self.ontology.perspective_id] = self.ontology
            return self.ontology

    def propose_interpretation(self, query: str) -> PerspectiveProposal:
        """查询时提出对本事件的解读。"""
        confidence = 0.5 + 0.1 * len(self.ontology.entities) / max(
            len(self.ontology.entities) + 5, 1)

        return PerspectiveProposal(
            proposal_id=str(uuid.uuid4())[:8],
            perspective_id=self.ontology.perspective_id,
            goal=self.ontology.goal,
            content=f"[{self.ontology.goal.value}] Interpretation of: {query[:60]}",
            confidence=round(min(confidence, 0.95), 4),
            supporting_entities=list(self.ontology.entities.keys())[:5],
            domain_knowledge_tags=[self.ontology.goal.value],
        )

    def critique(self, other_proposal: PerspectiveProposal) -> Optional[AttackEdge]:
        """对另一视角的提议进行批判（非对称领域知识互评）。"""
        # 基于目标冲突生成攻击
        goal_conflicts = {
            (PerspectiveGoal.STRATEGIC, PerspectiveGoal.COMPLIANCE): AttackType.UNDERMINING,
            (PerspectiveGoal.EFFICIENCY, PerspectiveGoal.SECURITY): AttackType.UNDER_CUTTING,
            (PerspectiveGoal.RELATIONSHIP, PerspectiveGoal.STRATEGIC): AttackType.REBUTTAL,
            (PerspectiveGoal.INNOVATION, PerspectiveGoal.COMPLIANCE): AttackType.DOMAIN_OVERRIDE,
        }

        key = (self.ontology.goal, other_proposal.goal)
        if key in goal_conflicts:
            attack_type = goal_conflicts[key]
            reason = (f"Goal conflict: {self.ontology.goal.value} vs "
                     f"{other_proposal.goal.value} — "
                     f"{self.ontology.goal.value} perspective challenges "
                     f"{other_proposal.goal.value} interpretation")
            return AttackEdge(
                attacker_proposal_id=f"from_{self.ontology.perspective_id}",
                target_proposal_id=other_proposal.proposal_id,
                attack_type=attack_type,
                reason=reason,
                strength=0.7,
                domain_justification=f"Domain knowledge from {self.ontology.goal.value}",
            )

        key2 = (other_proposal.goal, self.ontology.goal)
        if key2 in goal_conflicts:
            # 反过来也攻击
            attack_type = goal_conflicts[key2]
            reason = (f"Goal conflict (reversed): {other_proposal.goal.value} vs "
                     f"{self.ontology.goal.value}")
            return AttackEdge(
                attacker_proposal_id=f"from_{self.ontology.perspective_id}",
                target_proposal_id=other_proposal.proposal_id,
                attack_type=attack_type,
                reason=reason,
                strength=0.5,
                domain_justification=f"Reversed domain knowledge",
            )

        return None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "perspective_id": self.ontology.perspective_id,
                "goal": self.ontology.goal.value,
                "entities": len(self.ontology.entities),
                "relations": len(self.ontology.relations),
            }


class ArgumentationResolver:
    """Dung 论辩语义裁决器。

    将视角提议映射为论证框架（AF），生成攻击图，用 grounded 语义裁决。
    """

    def __init__(self):
        self._lock = threading.RLock()

    def resolve(self, proposals: List[PerspectiveProposal],
                attacks: List[AttackEdge],
                mode: PersRetrievalMode) -> AttackGraph:
        """论辩裁决。"""
        with self._lock:
            graph = AttackGraph(
                graph_id=str(uuid.uuid4())[:8],
                event_id="",
                retrieval_mode=mode,
            )

            # 构建论证节点
            for p in proposals:
                node = ArgumentNode(proposal=p, status=ArgumentStatus.UNDECIDED)
                graph.nodes[p.proposal_id] = node

            # 构建攻击边
            graph.attacks = attacks
            for att in attacks:
                if att.target_proposal_id in graph.nodes:
                    graph.nodes[att.target_proposal_id].attackers.append(att.attacker_proposal_id)
                if att.attacker_proposal_id in graph.nodes:
                    graph.nodes[att.attacker_proposal_id].victims.append(att.target_proposal_id)

            # Grounded 语义：计算最小不动点
            in_nodes, out_nodes = self._grounded_semantics(graph)

            # 更新节点状态
            for nid in in_nodes:
                if nid in graph.nodes:
                    graph.nodes[nid].status = ArgumentStatus.IN
                    graph.nodes[nid].grounded_label = True
            for nid in out_nodes:
                if nid in graph.nodes:
                    graph.nodes[nid].status = ArgumentStatus.OUT

            # 按检索模式选择结果
            if mode == PersRetrievalMode.SELECTION:
                # 选择最强存活论证
                survivors = [nid for nid in in_nodes if nid in graph.nodes]
                if survivors:
                    best = max(survivors, key=lambda nid: graph.nodes[nid].proposal.confidence)
                    graph.selected_proposals = [best]
                    graph.rejected_proposals = [nid for nid in graph.nodes if nid != best]
            elif mode == PersRetrievalMode.COMPOSITION:
                # 组合所有存活论证
                graph.selected_proposals = [nid for nid in in_nodes if nid in graph.nodes]
                graph.rejected_proposals = list(out_nodes)
            elif mode == PersRetrievalMode.CONFLICT_SURFACING:
                # 暴露冲突：同时展示 IN 和 OUT 的理由
                graph.selected_proposals = [nid for nid in in_nodes if nid in graph.nodes]
                graph.rejected_proposals = list(out_nodes)
                # 记录每个拒绝的详细理由
                for nid in graph.rejected_proposals:
                    reasons = [att.reason for att in attacks
                              if att.target_proposal_id == nid]
                    graph.rejection_reasons[nid] = " | ".join(reasons) if reasons else "defeated"

            # 生成可解释性说明
            graph.explanation = self._generate_explanation(graph, mode)
            return graph

    def _grounded_semantics(self, graph: AttackGraph) -> Tuple[Set[str], Set[str]]:
        """Grounded 语义：计算 IN/OUT 最小不动点。"""
        in_set: Set[str] = set()
        out_set: Set[str] = set()

        changed = True
        iteration = 0
        while changed and iteration < 100:
            changed = False
            iteration += 1

            for nid, node in graph.nodes.items():
                if nid in in_set or nid in out_set:
                    continue

                # 所有攻击者都 OUT → 该节点 IN（可辩护）
                all_attackers_out = all(
                    a in out_set or a not in graph.nodes
                    for a in node.attackers
                )
                if all_attackers_out and node.attackers:
                    in_set.add(nid)
                    changed = True
                    continue

                # 有攻击者 IN → 该节点 OUT
                has_in_attacker = any(a in in_set for a in node.attackers)
                if has_in_attacker:
                    out_set.add(nid)
                    changed = True

        # 未被攻击的节点 → IN
        for nid, node in graph.nodes.items():
            if nid not in in_set and nid not in out_set and not node.attackers:
                in_set.add(nid)

        # 未决 → OUT（保守）
        for nid, node in graph.nodes.items():
            if nid not in in_set and nid not in out_set:
                out_set.add(nid)

        return in_set, out_set

    def _generate_explanation(self, graph: AttackGraph,
                             mode: PersRetrievalMode) -> str:
        """生成攻击图解释。"""
        in_count = len(graph.selected_proposals)
        out_count = len(graph.rejected_proposals)
        lines = [
            f"Rashomon {mode.value} mode: {in_count} accepted, {out_count} rejected",
        ]
        for nid in graph.selected_proposals:
            if nid in graph.nodes:
                node = graph.nodes[nid]
                lines.append(f"  SELECTED [{node.proposal.goal.value}]: "
                           f"{node.proposal.content[:60]}")
        for nid in graph.rejected_proposals[:3]:
            if nid in graph.nodes:
                reason = graph.rejection_reasons.get(nid, "defeated by argument")
                lines.append(f"  REJECTED [{graph.nodes[nid].proposal.goal.value}]: "
                           f"{reason[:80]}")
        return "\n".join(lines)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {}


class RashomonMemory:
    """Rashomon 多视角论辩记忆总控。

    管理多个 GoalConditionedPerspective，事件编码 + 查询时论辩裁决。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.perspectives: Dict[str, GoalConditionedPerspective] = {}
        self.events: Dict[str, EncodedEvent] = {}
        self.resolver = ArgumentationResolver()
        self.attack_graphs: List[AttackGraph] = []

    def add_perspective(self, perspective_id: str, goal: PerspectiveGoal,
                        priority_weights: Dict[str, float] = None) -> GoalConditionedPerspective:
        """注册新视角。"""
        with self._lock:
            p = GoalConditionedPerspective(perspective_id, goal, priority_weights)
            self.perspectives[perspective_id] = p
            return p

    def encode_event(self, event_description: str) -> EncodedEvent:
        """各视角独立编码同一事件。"""
        with self._lock:
            event = EncodedEvent(
                event_id=str(uuid.uuid4())[:8],
                raw_description=event_description,
            )
            for pid, perspective in self.perspectives.items():
                perspective.encode(event)
            self.events[event.event_id] = event
            return event

    def query(self, event_id: str, query: str,
              mode: PersRetrievalMode = PersRetrievalMode.CONFLICT_SURFACING) -> AttackGraph:
        """查询时论辩裁决。"""
        with self._lock:
            event = self.events.get(event_id)
            if not event:
                raise ValueError(f"Event {event_id} not found")

            # 各视角提议
            proposals: List[PerspectiveProposal] = []
            for pid, perspective in self.perspectives.items():
                proposal = perspective.propose_interpretation(query)
                proposals.append(proposal)

            # 非对称互评 → 攻击边
            attacks: List[AttackEdge] = []
            for pi in self.perspectives.values():
                for pj in proposals:
                    if pj.perspective_id != pi.ontology.perspective_id:
                        attack = pi.critique(pj)
                        if attack:
                            attacks.append(attack)

            # Dung 论辩裁决
            graph = self.resolver.resolve(proposals, attacks, mode)
            graph.event_id = event_id
            self.attack_graphs.append(graph)
            return graph

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            goal_counts = defaultdict(int)
            for p in self.perspectives.values():
                goal_counts[p.ontology.goal.value] += 1
            return {
                "total_perspectives": len(self.perspectives),
                "goal_distribution": dict(goal_counts),
                "total_events": len(self.events),
                "total_attack_graphs": len(self.attack_graphs),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P22-7 Rashomon Argumentation Memory",
        "benchmark": "Argumentation-Driven Multi-Perspective Retrieval (arXiv 2604.03588, Warsaw UT)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 6,
        "key_pattern": "GoalConditioned(6 goals)→IndependentEncoding→Proposals→AttackGraph→DungGrounded→Explainable",
        "key_metric": "3 retrieval modes (selection/composition/conflict surfacing) from attack graph topology",
        "thread_safe": True,
    }
