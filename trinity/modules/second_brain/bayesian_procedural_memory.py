"""
P20-5: Bayesian Procedural Memory — AAMAS 2026
===============================================

对标论文：Learning Hierarchical Procedural Memory for LLM Agents
through Bayesian Selection and Contrastive Refinement (AAMAS 2026).

设计要点：
  - Beta(α,β) 后验维护过程成功率
  - 贝叶斯期望效用排序选过程（平衡上下文相关/成功概率/失败风险/信息增益）
  - 对比精炼：成功/失败轨迹对比收紧前置条件 + 修复动作序列 + 精炼后置条件
  - 元过程 Playbook：频繁共现过程组包条件控制策略 continue/skip/repeat/abort
  - 推理/学习解耦：冻结 LLM + 外部结构化过程记忆，无梯度更新

核心组件：
  - BetaPosterior:              Beta(α,β) 后验分布
  - BayesianProcedureSelector:  贝叶斯期望效用过程选择器
  - ContrastiveRefiner:         成功/失败对比精炼器
  - MetaProcedurePlaybook:      元过程条件控制策略组
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

class PlaybookPolicy(Enum):
    """元过程 Playbook 条件控制策略。"""
    CONTINUE = "continue"    # 继续执行下一过程
    SKIP = "skip"            # 跳过当前过程
    REPEAT = "repeat"        # 重试当前过程
    ABORT = "abort"          # 终止整组


class RefinementTrigger(Enum):
    """精炼触发条件。"""
    EVIDENCE_THRESHOLD = "evidence_threshold"  # 累积足够证据
    FAILURE_SPIKE = "failure_spike"            # 失败率突增
    MANUAL = "manual"                          # 手动触发


class ProcedureStatus(Enum):
    """过程状态。"""
    ACTIVE = "active"
    REFINING = "refining"
    DEPRECATED = "deprecated"
    MATURE = "mature"  # α+β 超过阈值，稳定


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class BetaPosterior:
    """Beta(α,β) 后验分布。

    α = 成功次数 + 1 (Jeffreys prior)
    β = 失败次数 + 1
    """
    alpha: float = 1.0
    beta: float = 1.0
    procedure_id: str = ""
    total_trials: int = 0

    @property
    def mean(self) -> float:
        """后验均值 α/(α+β)。"""
        return self.alpha / (self.alpha + self.beta) if (self.alpha + self.beta) > 0 else 0.5

    @property
    def variance(self) -> float:
        """后验方差。"""
        ab = self.alpha + self.beta
        return (self.alpha * self.beta) / (ab * ab * (ab + 1)) if ab > 0 else 0.25

    @property
    def uncertainty(self) -> float:
        """不确定性：标准差。"""
        return math.sqrt(self.variance)

    def update(self, success: bool, weight: float = 1.0):
        """Bayesian 更新。"""
        if success:
            self.alpha += weight
        else:
            self.beta += weight
        self.total_trials += 1


@dataclass
class ProcedureCandidate:
    """过程候选。"""
    procedure_id: str
    name: str
    description: str
    posterior: BetaPosterior
    preconditions: List[str] = field(default_factory=list)
    action_sequence: List[str] = field(default_factory=list)
    postconditions: List[str] = field(default_factory=list)
    context_tags: List[str] = field(default_factory=list)
    co_occurrence_frequency: Dict[str, int] = field(default_factory=dict)
    status: ProcedureStatus = ProcedureStatus.ACTIVE
    evidence_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class ContrastivePair:
    """对比精炼对。"""
    pair_id: str
    procedure_id: str
    success_context: Dict[str, Any] = field(default_factory=dict)
    failure_context: Dict[str, Any] = field(default_factory=dict)
    success_actions: List[str] = field(default_factory=list)
    failure_actions: List[str] = field(default_factory=list)
    refined_preconditions: List[str] = field(default_factory=list)
    refined_actions: List[str] = field(default_factory=list)
    refined_postconditions: List[str] = field(default_factory=list)
    refined_at: float = 0.0


@dataclass
class PlaybookEntry:
    """元过程 Playbook 条目。"""
    entry_id: str
    name: str
    procedure_ids: List[str]
    control_policies: Dict[str, Dict[str, PlaybookPolicy]] = field(default_factory=dict)
    success_rate: float = 0.0
    usage_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class ExpectedUtility:
    """贝叶斯期望效用计算结果。"""
    procedure_id: str
    utility: float
    success_prob: float = 0.0
    failure_risk: float = 0.0
    information_gain: float = 0.0
    context_relevance: float = 0.0


# ============================================================================
# Constants
# ============================================================================

EVIDENCE_THRESHOLD: int = 10
MATURE_THRESHOLD: int = 50
CO_OCCURRENCE_THRESHOLD: int = 3


# ============================================================================
# Core Components
# ============================================================================

class BayesianProcedureSelector:
    """贝叶斯期望效用过程选择器。

    维护 Beta(α,β) 后验，通过期望效用排序选择过程。
    平衡：上下文相关性 + 成功概率 + 失败风险 + 信息增益。
    """

    def __init__(self, exploration_weight: float = 0.15, utility_temperature: float = 1.0):
        self._lock = threading.RLock()
        self.procedures: Dict[str, ProcedureCandidate] = {}
        self.exploration_weight = exploration_weight
        self.utility_temperature = utility_temperature
        self.selection_history: List[Tuple[str, float]] = []

    def register(self, name: str, description: str, preconditions: List[str] = None,
                 actions: List[str] = None, postconditions: List[str] = None,
                 context_tags: List[str] = None) -> str:
        """注册新过程。"""
        with self._lock:
            pid = str(uuid.uuid4())[:8]
            proc = ProcedureCandidate(
                procedure_id=pid,
                name=name,
                description=description,
                posterior=BetaPosterior(procedure_id=pid),
                preconditions=preconditions or [],
                action_sequence=actions or [],
                postconditions=postconditions or [],
                context_tags=context_tags or [],
            )
            self.procedures[pid] = proc
            return pid

    def observe(self, procedure_id: str, success: bool, context: Dict[str, Any] = None,
                weight: float = 1.0):
        """记录过程执行结果，更新后验。"""
        with self._lock:
            proc = self.procedures.get(procedure_id)
            if not proc:
                return
            proc.posterior.update(success, weight)
            proc.evidence_count += 1

            # 更新状态
            if proc.posterior.alpha + proc.posterior.beta >= MATURE_THRESHOLD:
                proc.status = ProcedureStatus.MATURE

    def select(self, query_context: Dict[str, Any], top_k: int = 3) -> List[ExpectedUtility]:
        """基于期望效用选择 top-k 过程。

        期望效用 = context_relevance * (success_prob - risk_penalty * failure_risk + exploration * info_gain)
        """
        with self._lock:
            utilities: List[ExpectedUtility] = []

            for proc in self.procedures.values():
                posterior = proc.posterior
                success_prob = posterior.mean
                failure_risk = 1.0 - success_prob

                # 信息增益（不确定性驱动探索）
                info_gain = posterior.uncertainty * self.exploration_weight

                # 上下文相关性
                context_relevance = self._context_relevance(proc, query_context)

                # 期望效用
                utility = context_relevance * (
                    success_prob * 0.5
                    - failure_risk * 0.3
                    + info_gain * 0.2
                )

                eu = ExpectedUtility(
                    procedure_id=proc.procedure_id,
                    utility=round(utility, 6),
                    success_prob=round(success_prob, 4),
                    failure_risk=round(failure_risk, 4),
                    information_gain=round(info_gain, 4),
                    context_relevance=round(context_relevance, 4),
                )
                utilities.append(eu)

            # 排序
            utilities.sort(key=lambda x: x.utility, reverse=True)

            selected = utilities[:top_k]
            for u in selected:
                self.selection_history.append((u.procedure_id, u.utility))
            return selected

    def _context_relevance(self, proc: ProcedureCandidate, query: Dict[str, Any]) -> float:
        """上下文相关性计算。"""
        if not proc.context_tags:
            return 0.5  # 无标签默认中等相关

        query_text = str(query.get("task", "")) + " " + str(query.get("description", ""))
        query_lower = query_text.lower()

        hits = sum(1 for tag in proc.context_tags if tag.lower() in query_lower)
        return min(0.5 + hits / max(len(proc.context_tags), 1) * 0.5, 1.0)

    def exploitation_score(self, procedure_id: str) -> float:
        """纯利用分数（不含探索项）。"""
        proc = self.procedures.get(procedure_id)
        if not proc:
            return 0.0
        return proc.posterior.mean

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            status_counts = defaultdict(int)
            for p in self.procedures.values():
                status_counts[p.status.value] += 1
            return {
                "total_procedures": len(self.procedures),
                "by_status": dict(status_counts),
                "avg_success_rate": round(
                    sum(p.posterior.mean for p in self.procedures.values()) /
                    max(len(self.procedures), 1), 4),
                "total_selections": len(self.selection_history),
            }


class ContrastiveRefiner:
    """成功/失败对比精炼器。

    对比成功与失败执行上下文，收紧前置条件、修复动作序列、精炼后置条件。
    通过记忆编辑而非梯度更新来改进过程质量。
    """

    def __init__(self, evidence_threshold: int = EVIDENCE_THRESHOLD):
        self._lock = threading.RLock()
        self.evidence_threshold = evidence_threshold
        self.pairs: List[ContrastivePair] = []
        self.refinement_count: int = 0

    def collect_pair(self, procedure_id: str, success_context: Dict[str, Any],
                     failure_context: Dict[str, Any], success_actions: List[str],
                     failure_actions: List[str]) -> ContrastivePair:
        """收集对比对。"""
        with self._lock:
            pair = ContrastivePair(
                pair_id=str(uuid.uuid4())[:8],
                procedure_id=procedure_id,
                success_context=success_context,
                failure_context=failure_context,
                success_actions=success_actions,
                failure_actions=failure_actions,
            )
            self.pairs.append(pair)
            return pair

    def refine(self, procedure: ProcedureCandidate) -> ProcedureCandidate:
        """对比精炼：收紧前置、修复动作、精炼后置。"""
        with self._lock:
            relevant_pairs = [p for p in self.pairs if p.procedure_id == procedure.procedure_id]
            if len(relevant_pairs) < self.evidence_threshold:
                return procedure

            procedure.status = ProcedureStatus.REFINING
            procedure.evidence_count = len(relevant_pairs)

            # 收紧前置条件：添加失败中出现但成功中缺失的约束
            for pair in relevant_pairs[-5:]:
                fail_keys = set(pair.failure_context.keys()) - set(pair.success_context.keys())
                for key in fail_keys:
                    cond = f"{key} must be {pair.success_context.get(key, 'valid')}"
                    if cond not in procedure.preconditions:
                        procedure.preconditions.append(cond)

            # 修复动作序列：成功动作替代失败动作
            all_success_actions: Set[str] = set()
            for pair in relevant_pairs:
                all_success_actions.update(pair.success_actions)
            if all_success_actions and len(all_success_actions) > len(procedure.action_sequence):
                procedure.action_sequence = list(all_success_actions)[:10]

            # 精炼后置条件
            success_outcomes: Set[str] = set()
            for pair in relevant_pairs:
                if pair.success_context.get("outcome"):
                    success_outcomes.add(str(pair.success_context["outcome"]))
            if success_outcomes:
                for outcome in success_outcomes:
                    if outcome not in procedure.postconditions:
                        procedure.postconditions.append(f"achieves: {outcome}")

            # 标记最新精炼
            for pair in relevant_pairs[-5:]:
                pair.refined_preconditions = list(procedure.preconditions)
                pair.refined_actions = list(procedure.action_sequence)
                pair.refined_postconditions = list(procedure.postconditions)
                pair.refined_at = time.time()

            procedure.status = ProcedureStatus.ACTIVE
            self.refinement_count += 1
            return procedure

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_pairs": len(self.pairs),
                "refinements": self.refinement_count,
            }


class MetaProcedurePlaybook:
    """元过程 Playbook。

    频繁共现过程组包为分层 Playbook，含条件控制策略 continue/skip/repeat/abort。
    """

    def __init__(self, co_occurrence_threshold: int = CO_OCCURRENCE_THRESHOLD):
        self._lock = threading.RLock()
        self.playbooks: Dict[str, PlaybookEntry] = {}
        self.co_occurrence_threshold = co_occurrence_threshold
        self.execution_log: List[Dict[str, Any]] = []

    def build_playbook(self, name: str, procedure_ids: List[str]) -> str:
        """构建或更新 Playbook。"""
        with self._lock:
            # 更新共现频率
            for i, pid_a in enumerate(procedure_ids):
                for pid_b in procedure_ids[i + 1:]:
                    pass  # 由外部 register_co_occurrence 维护

            entry_id = str(uuid.uuid4())[:8]
            entry = PlaybookEntry(
                entry_id=entry_id,
                name=name,
                procedure_ids=procedure_ids,
                control_policies={pid: {} for pid in procedure_ids},
            )
            self.playbooks[entry_id] = entry
            return entry_id

    def derive_policies(self, entry_id: str, selector: BayesianProcedureSelector):
        """基于后验统计推导条件控制策略。"""
        with self._lock:
            entry = self.playbooks.get(entry_id)
            if not entry:
                return

            for pid in entry.procedure_ids:
                proc = selector.procedures.get(pid)
                if not proc:
                    continue
                mean = proc.posterior.mean
                uncertainty = proc.posterior.uncertainty

                # 高成功率低不确定性 → CONTINUE
                if mean > 0.7 and uncertainty < 0.15:
                    policy = PlaybookPolicy.CONTINUE
                # 中等 → REPEAT (重试有望)
                elif mean > 0.4:
                    policy = PlaybookPolicy.REPEAT
                # 低成功率 → SKIP
                elif mean > 0.15:
                    policy = PlaybookPolicy.SKIP
                # 极低 → ABORT
                else:
                    policy = PlaybookPolicy.ABORT

                entry.control_policies[pid]["default"] = policy

            entry.success_rate = round(
                sum(selector.procedures.get(pid).posterior.mean
                    for pid in entry.procedure_ids
                    if pid in selector.procedures) / max(len(entry.procedure_ids), 1), 4)

    def get_policy(self, entry_id: str, procedure_id: str) -> PlaybookPolicy:
        """获取指定过程的控制策略。"""
        with self._lock:
            entry = self.playbooks.get(entry_id)
            if not entry:
                return PlaybookPolicy.CONTINUE
            return entry.control_policies.get(procedure_id, {}).get("default", PlaybookPolicy.CONTINUE)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            policy_counts = defaultdict(int)
            for entry in self.playbooks.values():
                for policies in entry.control_policies.values():
                    for policy in policies.values():
                        policy_counts[policy.value] += 1
            return {
                "total_playbooks": len(self.playbooks),
                "avg_procedures_per_playbook": round(
                    sum(len(e.procedure_ids) for e in self.playbooks.values()) /
                    max(len(self.playbooks), 1), 1),
                "policy_distribution": dict(policy_counts),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P20-5 Bayesian Procedural Memory",
        "benchmark": "AAMAS 2026 — Bayesian Selection + Contrastive Refinement + Meta-Playbook",
        "classes": 3,
        "enums": 3,
        "dataclasses": 5,
        "key_pattern": "BetaPosterior→ExpectedUtility→Select→ContrastiveRefine→Playbook Policy",
        "key_metric": "Closed-form Bayesian utility with Beta(α,β) posteriors & contrastive memory editing",
        "thread_safe": True,
    }
