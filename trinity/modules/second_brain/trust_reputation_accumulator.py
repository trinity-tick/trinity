"""
# status: orphan (2026-08-15 audit, not in runtime path)
P21-7: Trust & Reputation Accumulator — Bayesian Trust + PageRank Reputation
=============================================================================

对标方案：Bayesian Trust Models & Multi-Hop Reputation Propagation (2026).

设计要点：
  - 基于贝叶斯的信任评分（Beta 信誉模型）
  - 多跳声望传播（PageRank 变体）
  - 背叛/合作事件加权更新（不对称更新系数 δ_pos << δ_neg）
  - 信任阈值门控交互决策

核心组件：
  - TrustAccumulator:     贝叶斯信任累积器
  - ReputationPropagator:  PageRank 多跳声望传播器
  - TrustGate:            信任阈值门控决策
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

class InteractionOutcome(Enum):
    """交互结果。"""
    COOPERATION = "cooperation"      # 合作
    BETRAYAL = "betrayal"            # 背叛
    NEUTRAL = "neutral"              # 中性
    UNKNOWN = "unknown"              # 未知


class TrustLevel(Enum):
    """信任等级。"""
    UNTRUSTED = "untrusted"          # trust < 0.2
    LOW_TRUST = "low_trust"          # 0.2 ~ 0.4
    MODERATE = "moderate"            # 0.4 ~ 0.6
    TRUSTED = "trusted"              # 0.6 ~ 0.8
    HIGH_TRUST = "high_trust"        # > 0.8


class GateDecision(Enum):
    """信任门控决策。"""
    ALLOW = "allow"
    CAUTION = "caution"       # 需要额外验证
    BLOCK = "block"
    PENDING = "pending"       # 信任评分不足，等待更多证据


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TrustRecord:
    """信任记录（单次交互）。"""
    record_id: str
    trustee_id: str
    trustor_id: str
    outcome: InteractionOutcome
    context: str = ""
    weight: float = 1.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class TrustScore:
    """Beta 信任评分。"""
    alpha: float = 1.0     # 合作次数 + 1 (Jeffreys prior)
    beta: float = 1.0      # 背叛次数 + 1

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        ab = self.alpha + self.beta
        return (self.alpha * self.beta) / (ab * ab * (ab + 1))

    @property
    def confidence(self) -> float:
        """证据充分度（样本量归一化）。"""
        n = self.alpha + self.beta - 2
        return min(n / 50.0, 1.0)


@dataclass
class ReputationNode:
    """声望图谱节点。"""
    agent_id: str
    reputation_score: float = 0.0
    incoming_trust: Dict[str, float] = field(default_factory=dict)  # source → trust
    outgoing_trust: Dict[str, float] = field(default_factory=dict)
    pagerank: float = 0.15   # PageRank 值
    is_seed: bool = False


@dataclass
class GateResult:
    """信任门控结果。"""
    decision: GateDecision
    trust_score: float
    reputation_score: float
    evidence_count: int
    reason: str


# ============================================================================
# Constants
# ============================================================================

# 不对称更新系数（背叛惩罚远大于合作奖励）
DELTA_COOPERATION: float = 1.0
DELTA_BETRAYAL: float = 3.0

# 信任阈值
TRUST_THRESHOLD_ALLOW: float = 0.6
TRUST_THRESHOLD_CAUTION: float = 0.3
TRUST_THRESHOLD_BLOCK: float = 0.15

# PageRank 参数
PR_DAMPING: float = 0.85
PR_MAX_ITERATIONS: int = 100
PR_CONVERGENCE: float = 1e-6


# ============================================================================
# Core Components
# ============================================================================

class TrustAccumulator:
    """贝叶斯信任累积器。

    Beta(α,β) 信誉模型，不对称更新（背叛惩罚 3x）。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.scores: Dict[Tuple[str, str], TrustScore] = {}  # (trustor, trustee) → score
        self.records: List[TrustRecord] = []

    def record(self, trustor_id: str, trustee_id: str, outcome: InteractionOutcome,
               context: str = "", weight: float = 1.0) -> TrustScore:
        """记录交互并更新贝叶斯信任评分。"""
        with self._lock:
            key = (trustor_id, trustee_id)
            score = self.scores.get(key, TrustScore())

            if outcome == InteractionOutcome.COOPERATION:
                score.alpha += DELTA_COOPERATION * weight
            elif outcome == InteractionOutcome.BETRAYAL:
                score.beta += DELTA_BETRAYAL * weight
            # NEUTRAL / UNKNOWN：轻微偏向中性
            elif outcome == InteractionOutcome.NEUTRAL:
                score.alpha += 0.3 * weight
                score.beta += 0.3 * weight

            self.scores[key] = score

            rec = TrustRecord(
                record_id=str(uuid.uuid4())[:8],
                trustor_id=trustor_id,
                trustee_id=trustee_id,
                outcome=outcome,
                context=context,
                weight=weight,
            )
            self.records.append(rec)
            return score

    def get_trust(self, trustor_id: str, trustee_id: str) -> TrustScore:
        """获取信任评分。"""
        with self._lock:
            return self.scores.get((trustor_id, trustee_id), TrustScore())

    def get_trust_level(self, trustor_id: str, trustee_id: str) -> TrustLevel:
        """获取信任等级。"""
        mean = self.get_trust(trustor_id, trustee_id).mean
        if mean > 0.8:
            return TrustLevel.HIGH_TRUST
        if mean > 0.6:
            return TrustLevel.TRUSTED
        if mean > 0.4:
            return TrustLevel.MODERATE
        if mean > 0.2:
            return TrustLevel.LOW_TRUST
        return TrustLevel.UNTRUSTED

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            trust_levels = defaultdict(int)
            for score in self.scores.values():
                if score.mean > 0.8:
                    trust_levels["high_trust"] += 1
                elif score.mean > 0.6:
                    trust_levels["trusted"] += 1
                elif score.mean > 0.4:
                    trust_levels["moderate"] += 1
                elif score.mean > 0.2:
                    trust_levels["low_trust"] += 1
                else:
                    trust_levels["untrusted"] += 1
            return {
                "total_relationships": len(self.scores),
                "total_records": len(self.records),
                "trust_distribution": dict(trust_levels),
                "avg_trust": round(
                    sum(s.mean for s in self.scores.values()) /
                    max(len(self.scores), 1), 4),
            }


class ReputationPropagator:
    """多跳声望传播器。

    PageRank 变体：全局声望流经信任网络扩散。
    """

    def __init__(self, damping: float = PR_DAMPING,
                 convergence: float = PR_CONVERGENCE,
                 max_iterations: int = PR_MAX_ITERATIONS):
        self._lock = threading.RLock()
        self.nodes: Dict[str, ReputationNode] = {}
        self.damping = damping
        self.convergence = convergence
        self.max_iterations = max_iterations

    def add_node(self, agent_id: str, is_seed: bool = False):
        """注册声望节点。"""
        with self._lock:
            self.nodes[agent_id] = ReputationNode(
                agent_id=agent_id,
                is_seed=is_seed,
            )

    def update_trust_edge(self, source: str, target: str, trust: float):
        """更新信任边权重。"""
        with self._lock:
            if source not in self.nodes:
                self.add_node(source)
            if target not in self.nodes:
                self.add_node(target)
            self.nodes[source].outgoing_trust[target] = trust
            self.nodes[target].incoming_trust[source] = trust

    def propagate(self) -> Dict[str, float]:
        """PageRank 迭代传播。"""
        with self._lock:
            n = len(self.nodes)
            if n == 0:
                return {}

            # 初始化
            for node in self.nodes.values():
                node.pagerank = 1.0 / n

            for iteration in range(self.max_iterations):
                new_pr: Dict[str, float] = {}
                max_delta = 0.0

                for agent_id, node in self.nodes.items():
                    # 入边 PR 贡献
                    incoming_sum = 0.0
                    for source, trust in node.incoming_trust.items():
                        source_node = self.nodes.get(source)
                        if source_node and source_node.outgoing_trust:
                            weight = trust / sum(source_node.outgoing_trust.values())
                            incoming_sum += source_node.pagerank * weight
                        else:
                            incoming_sum += trust / n

                    new_pr[agent_id] = (1 - self.damping) / n + self.damping * incoming_sum

                    delta = abs(new_pr[agent_id] - node.pagerank)
                    max_delta = max(max_delta, delta)

                for agent_id, pr in new_pr.items():
                    self.nodes[agent_id].pagerank = pr

                if max_delta < self.convergence:
                    logger.debug(f"PageRank converged at iteration {iteration + 1}")
                    break

            return {aid: round(node.pagerank, 6) for aid, node in self.nodes.items()}

    def get_reputation(self, agent_id: str) -> float:
        """获取全局声望（PageRank 值）。"""
        return self.nodes.get(agent_id, ReputationNode(agent_id)).pagerank

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self.nodes),
                "avg_pagerank": round(
                    sum(n.pagerank for n in self.nodes.values()) /
                    max(len(self.nodes), 1), 6),
            }


class TrustGate:
    """信任阈值门控交互决策。

    组合直接信任评分 + 全局声望，门控 Allow/Caution/Block。
    """

    def __init__(self, accumulator: TrustAccumulator, propagator: ReputationPropagator,
                 allow_threshold: float = TRUST_THRESHOLD_ALLOW,
                 caution_threshold: float = TRUST_THRESHOLD_CAUTION,
                 block_threshold: float = TRUST_THRESHOLD_BLOCK):
        self._lock = threading.RLock()
        self.accumulator = accumulator
        self.propagator = propagator
        self.allow_threshold = allow_threshold
        self.caution_threshold = caution_threshold
        self.block_threshold = block_threshold

    def evaluate(self, trustor_id: str, trustee_id: str) -> GateResult:
        """评估是否允许交互。"""
        with self._lock:
            # 直接信任
            trust_score = self.accumulator.get_trust(trustor_id, trustee_id)
            direct_trust = trust_score.mean
            evidence = int(trust_score.alpha + trust_score.beta - 2)

            # 全局声望
            reputation = self.propagator.get_reputation(trustee_id)

            # 混合评分（直接信任 70% + 全局声望 30%）
            composite = direct_trust * 0.7 + reputation * 0.3

            # 决策门控
            if composite >= self.allow_threshold:
                decision = GateDecision.ALLOW
                reason = f"High composite trust ({composite:.3f})"
            elif composite >= self.caution_threshold:
                decision = GateDecision.CAUTION
                reason = f"Moderate trust ({composite:.3f}), additional verification recommended"
            elif composite >= self.block_threshold:
                decision = GateDecision.PENDING
                reason = f"Low trust ({composite:.3f}), insufficient evidence ({evidence} interactions)"
            else:
                decision = GateDecision.BLOCK
                reason = f"Trust too low ({composite:.3f}), blocked"

            return GateResult(
                decision=decision,
                trust_score=round(direct_trust, 4),
                reputation_score=round(reputation, 4),
                evidence_count=evidence,
                reason=reason,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "allow_threshold": self.allow_threshold,
                "caution_threshold": self.caution_threshold,
                "block_threshold": self.block_threshold,
                "accumulator": self.accumulator.statistics(),
                "propagator": self.propagator.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P21-7 Trust & Reputation Accumulator",
        "benchmark": "Bayesian Trust (Beta model) + PageRank Reputation Propagation (2026)",
        "classes": 3,
        "enums": 3,
        "dataclasses": 4,
        "key_pattern": "Beta(α,β)→AsymmetricUpdate(3x Betrayal)→PageRank→TrustGate(Allow/Caution/Block)",
        "key_metric": "δ_coop=1.0, δ_betray=3.0, PR damping=0.85, 3-tier gate (0.6/0.3/0.15)",
        "thread_safe": True,
    }
