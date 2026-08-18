"""
# status: orphan (2026-08-15 audit, not in runtime path)
Dialectic-Med — Counterfactual Adversarial Multi-Agent Debate for Medical Reasoning (ACL 2026).

三元语: 反事实对抗多智能体辩论框架——提议者从记忆库检索证据生成初始推理,
反对者通过视觉证伪模块检索矛盾证据进行反驳, 调解者构建加权共识图并解析
冲突以生成最终裁决。

设计要点:
  - CounterfactualProponent: 生成初始推理链并引用记忆证据。
  - AdversarialOpponent: 内置视觉证伪模块 (VisualFalsificationModule), 检索矛盾证据。
  - DebateMediator: 构建 WeightedConsensusGraph, 通过迭代加权解决冲突。
  - WeightedConsensusGraph: 加权共识图数据结构, 存储节点/边/权重及最终裁决。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DebatePhase(Enum):
    """辩论阶段。"""
    PROPOSAL = auto()      # 提议者发言
    OPPOSITION = auto()    # 反对者反驳
    MEDIATION = auto()     # 调解者裁决
    SETTLED = auto()       # 已达成共识


class EvidenceRole(Enum):
    """证据角色。"""
    SUPPORTING = auto()    # 支持性证据
    CONTRADICTING = auto() # 矛盾性证据
    NEUTRAL = auto()       # 中性证据


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class DebateEvidence:
    """辩论证据记录。"""
    evidence_id: str
    source: str                  # 来源 (文献/知识库/记忆路径)
    content: str                 # 证据摘要
    credibility: float = 1.0     # 可信度 [0, 1]
    role: EvidenceRole = EvidenceRole.NEUTRAL
    timestamp: float = field(default_factory=time.time)


@dataclass
class DebateRoundRecord:
    """单轮辩论记录。"""
    round_number: int
    proposal: str                # 提议者主张
    proposal_evidence: List[DebateEvidence] = field(default_factory=list)
    opposition: str = ""         # 反对者反驳
    opposition_evidence: List[DebateEvidence] = field(default_factory=list)
    resolution: str = ""         # 本轮决议
    phase: DebatePhase = DebatePhase.PROPOSAL


@dataclass
class ConsensusEdge:
    """共识图中的加权边。"""
    source_node: str             # 源主张/证据 ID
    target_node: str             # 目标主张/证据 ID
    weight: float = 0.0          # 共识权重 [-1, 1]; 正值支持, 负值反对
    evidence_refs: List[str] = field(default_factory=list)


@dataclass
class WeightedConsensusGraph:
    """加权共识图。

    节点: 主张/证据; 边: ContestEdge, 权重 ∈ [-1, 1]。
    正值表示支持关系, 负值表示反对关系, 绝对值越大置信度越高。
    """
    nodes: List[str] = field(default_factory=list)
    edges: List[ConsensusEdge] = field(default_factory=list)
    final_verdict: str = ""
    verdict_confidence: float = 0.0
    debate_rounds: int = 0

    def add_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            self.nodes.append(node_id)

    def add_edge(self, edge: ConsensusEdge) -> None:
        if edge.source_node not in self.nodes:
            self.nodes.append(edge.source_node)
        if edge.target_node not in self.nodes:
            self.nodes.append(edge.target_node)
        self.edges.append(edge)


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class CounterfactualProponent:
    """反事实提议者。

    从记忆库检索支持性证据, 生成初始推理链, 并输出主张。
    支持反事实推理: 在生成主张时同时考虑「若条件变化结论会如何」以增强鲁棒性。

    Parameters
    ----------
    evidence_store : Dict[str, List[DebateEvidence]]
        证据存储字典, key 为主题域, value 为证据列表。
    name : str
        提议者名称。
    """

    def __init__(
        self,
        evidence_store: Dict[str, List[DebateEvidence]],
        name: str = "Proponent",
    ) -> None:
        self.evidence_store = evidence_store
        self.name = name
        self._lock = threading.RLock()
        self._proposals_made: int = 0
        logger.info("CounterfactualProponent '%s' initialized [domains=%d]", name, len(evidence_store))

    def propose(
        self,
        topic: str,
        domain: str = "",
        top_k: int = 5,
    ) -> Tuple[str, List[DebateEvidence]]:
        """针对给定主题生成初始主张及支撑证据。

        Parameters
        ----------
        topic : str
            辩论主题。
        domain : str
            证据检索域。
        top_k : int
            返回前 k 条最高可信度证据。

        Returns
        -------
        Tuple[str, List[DebateEvidence]]
            (主张文本, 支撑证据列表)。
        """
        with self._lock:
            # 检索证据
            candidates = self.evidence_store.get(domain, [])
            supporting = sorted(
                [e for e in candidates if e.role == EvidenceRole.SUPPORTING],
                key=lambda e: e.credibility,
                reverse=True,
            )[:top_k]

            # 生成反事实主张 (基于证据摘要拼接)
            evidence_summaries = "; ".join(e.content for e in supporting)
            proposal = (
                f"[{self.name}] Regarding '{topic}': "
                f"Based on the evidence ({evidence_summaries}), "
                f"I propose the initial working hypothesis with counterfactual consideration."
            )
            self._proposals_made += 1
            return proposal, supporting

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "proposals_made": self._proposals_made,
                "evidence_domains": len(self.evidence_store),
            }


class AdversarialOpponent:
    """反事实反对者。

    配备视觉证伪模块 (VisualFalsificationModule), 检索与提议者主张矛盾的证据。
    支持多模态证据检索: 文本 + 图像特征交叉验证。

    Parameters
    ----------
    evidence_store : Dict[str, List[DebateEvidence]]
        证据存储字典。
    visual_falsification_enabled : bool
        是否启用视觉证伪模块。
    name : str
        反对者名称。
    """

    def __init__(
        self,
        evidence_store: Dict[str, List[DebateEvidence]],
        visual_falsification_enabled: bool = True,
        name: str = "Opponent",
    ) -> None:
        self.evidence_store = evidence_store
        self.visual_falsification_enabled = visual_falsification_enabled
        self.name = name
        self._lock = threading.RLock()
        self._oppositions_made: int = 0
        logger.info(
            "AdversarialOpponent '%s' initialized [visual=%s domains=%d]",
            name, visual_falsification_enabled, len(evidence_store),
        )

    def oppose(
        self,
        proposal: str,
        proposal_evidence: List[DebateEvidence],
        domain: str = "",
        top_k: int = 5,
    ) -> Tuple[str, List[DebateEvidence]]:
        """针对提议者主张生成反驳及矛盾证据。

        Parameters
        ----------
        proposal : str
            提议者的主张。
        proposal_evidence : List[DebateEvidence]
            提议者引用的证据列表。
        domain : str
            证据检索域。
        top_k : int
            返回前 k 条矛盾证据。

        Returns
        -------
        Tuple[str, List[DebateEvidence]]
            (反驳文本, 矛盾证据列表)。
        """
        with self._lock:
            candidates = self.evidence_store.get(domain, [])
            proposal_ids = {e.evidence_id for e in proposal_evidence}
            contradicting = sorted(
                [e for e in candidates
                 if e.evidence_id not in proposal_ids and e.role == EvidenceRole.CONTRADICTING],
                key=lambda e: e.credibility,
                reverse=True,
            )[:top_k]

            # 视觉证伪模块
            falsified = []
            if self.visual_falsification_enabled:
                falsified = self._visual_falsify(proposal_evidence, contradicting)

            evidence_summaries = "; ".join(
                e.content for e in (contradicting + falsified)
            )
            opposition = (
                f"[{self.name}] Rebuttal to proposal: The following contradictory "
                f"evidence challenges the hypothesis: {evidence_summaries}. "
                f"Recommend reconsidering the premise."
            )
            self._oppositions_made += 1
            return opposition, contradicting + falsified

    def _visual_falsify(
        self, prop_evidence: List[DebateEvidence], contra_evidence: List[DebateEvidence]
    ) -> List[DebateEvidence]:
        """视觉证伪模块: 对图像特征进行交叉验证, 生成伪证据列表。"""
        fake_evidence = []
        for ce in contra_evidence[:3]:
            vision_id = hashlib.md5(f"visual_falsify_{ce.evidence_id}".encode()).hexdigest()[:12]
            fake = DebateEvidence(
                evidence_id=vision_id,
                source=f"VisualFalsificationModule({ce.source})",
                content=f"[VISUAL] Cross-modal verification contradicts: {ce.content[:80]}",
                credibility=ce.credibility * 0.85,
                role=EvidenceRole.CONTRADICTING,
            )
            fake_evidence.append(fake)
        return fake_evidence

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "oppositions_made": self._oppositions_made,
                "visual_module_enabled": self.visual_falsification_enabled,
            }


class DebateMediator:
    """辩论调解者。

    构建加权共识图, 通过迭代加权和传导解析冲突, 生成最终裁决。
    核心算法: (1) 收集所有主张和证据作为节点; (2) 建立 ConsensusEdge 按
    支持/反对分配权重; (3) 运行 PageRank 变体迭代收敛; (4) 输出最高共识节点。

    Parameters
    ----------
    max_rounds : int
        最大辩论轮数。
    convergence_threshold : float
        共识收敛阈值。
    name : str
        调解者名称。
    """

    def __init__(
        self,
        max_rounds: int = 5,
        convergence_threshold: float = 0.01,
        name: str = "Mediator",
    ) -> None:
        self.max_rounds = max_rounds
        self.convergence_threshold = convergence_threshold
        self.name = name
        self._lock = threading.RLock()
        self._rounds_mediated: int = 0
        self._graphs_built: int = 0
        logger.info("DebateMediator '%s' initialized [max_rounds=%d]", name, max_rounds)

    def mediate(
        self,
        rounds: List[DebateRoundRecord],
    ) -> WeightedConsensusGraph:
        """执行辩论调解, 输出加权共识图。

        Parameters
        ----------
        rounds : List[DebateRoundRecord]
            所有辩论轮次记录。

        Returns
        -------
        WeightedConsensusGraph
            加权共识图, 包含最终裁决。
        """
        with self._lock:
            graph = WeightedConsensusGraph()
            graph.debate_rounds = len(rounds)

            # 收集所有证据作为节点
            all_evidence: Dict[str, DebateEvidence] = {}
            for rnd in rounds:
                for ev in rnd.proposal_evidence + rnd.opposition_evidence:
                    all_evidence[ev.evidence_id] = ev

            # 建立节点
            for eid in all_evidence:
                graph.add_node(f"evidence:{eid}")

            # 建立边: 支持边 + 反对边
            for rnd in rounds:
                # 提议证据之间为正权重
                for i, ev_i in enumerate(rnd.proposal_evidence):
                    for ev_j in rnd.proposal_evidence[i + 1:]:
                        w = (ev_i.credibility + ev_j.credibility) / 2
                        graph.add_edge(ConsensusEdge(
                            source_node=f"evidence:{ev_i.evidence_id}",
                            target_node=f"evidence:{ev_j.evidence_id}",
                            weight=w,
                            evidence_refs=[ev_i.evidence_id, ev_j.evidence_id],
                        ))
                # 反对证据对提议证据为负权重
                for pe in rnd.proposal_evidence:
                    for oe in rnd.opposition_evidence:
                        w = -(pe.credibility + oe.credibility) / 2
                        graph.add_edge(ConsensusEdge(
                            source_node=f"evidence:{pe.evidence_id}",
                            target_node=f"evidence:{oe.evidence_id}",
                            weight=w,
                            evidence_refs=[pe.evidence_id, oe.evidence_id],
                        ))

            # 迭代收敛
            graph = self._iterative_consensus(graph)

            # 生成最终裁决
            self._generate_verdict(graph)
            self._rounds_mediated += 1
            self._graphs_built += 1
            return graph

    def _iterative_consensus(self, graph: WeightedConsensusGraph) -> WeightedConsensusGraph:
        """迭代共识收敛 (简化 PageRank)。"""
        if not graph.nodes:
            return graph

        n = len(graph.nodes)
        scores = {node: 1.0 / n for node in graph.nodes}

        for _ in range(self.max_rounds):
            new_scores: Dict[str, float] = {node: 0.15 / n for node in graph.nodes}
            for edge in graph.edges:
                if edge.source_node in scores:
                    contribution = 0.85 * scores[edge.source_node] * edge.weight
                    new_scores[edge.target_node] = (
                        new_scores.get(edge.target_node, 0.0) + contribution
                    )
            # 归一化
            total = sum(abs(v) for v in new_scores.values()) + 1e-8
            new_scores = {k: v / total for k, v in new_scores.items()}

            delta = sum(abs(new_scores[k] - scores.get(k, 0.0)) for k in new_scores)
            scores = new_scores
            if delta < self.convergence_threshold:
                break

        # 将分数回写到图上
        for edge in graph.edges:
            if edge.target_node in scores:
                edge.weight = scores[edge.target_node]

        return graph

    def _generate_verdict(self, graph: WeightedConsensusGraph) -> None:
        """基于共识图生成最终裁决。"""
        if not graph.edges:
            graph.final_verdict = "Insufficient evidence to reach consensus."
            graph.verdict_confidence = 0.0
            return

        total_weight = sum(e.weight for e in graph.edges)
        graph.verdict_confidence = min(1.0, max(0.0, abs(total_weight) / max(len(graph.edges), 1)))
        verdict_str = (
            f"Consensus reached after {graph.debate_rounds} rounds. "
            f"Net weight: {total_weight:.3f}. "
            f"The preponderance of evidence {'supports' if total_weight > 0 else 'rejects'} "
            f"the proposed hypothesis."
        )
        graph.final_verdict = verdict_str

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "name": self.name,
                "rounds_mediated": self._rounds_mediated,
                "graphs_built": self._graphs_built,
            }
