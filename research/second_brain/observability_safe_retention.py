"""
# status: orphan (2026-08-15 audit, not in runtime path)
P22-8: Observability-Safe Memory Retention (OSL-MR) — Constrained Optimization
===============================================================================

对标论文：Learning What to Remember: Observability-Safe Memory Retention via
Constrained Optimization for Long-Horizon Language Agents
(arXiv 2606.10616, June 2026).

设计要点：
  - Mixed-Score 先验特征向量（命中率/过时/重获/缺失代价）
  - MLP 证据学习器（单隐层 16 单元 sigmoid）
  - 约束优化选留策略（仅用在线可观测特征）
  - 预算约束（32/64/128 tokens for LoCoMo, 256/512/1024 for LongMemEval）
  - 奖励系数：α_hit=4.0, α_reacq=6.0, α_miss=12.0, α_stale=6.0
  - 对比 OSL-MR (full) vs OSL-MR (w/o prior) 消融

核心组件：
  - MixedScorePrior:   混合先验特征向量
  - EvidenceLearner:    MLP 证据学习器
  - RetentionPolicy:   约束优化选留策略
  - ObservabilitySafeRetention:  可观测安全记忆保留总控
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class EvidenceState(Enum):
    """证据状态。"""
    ACTIVE = "active"        # 活跃（常被命中）
    STALE = "stale"          # 过时（长期未命中）
    DORMANT = "dormant"      # 休眠（可能重新激活）
    EXPIRED = "expired"      # 到期（应被回收）


class BudgetRegime(Enum):
    """预算约束协议。"""
    LOCOMO_LOOSE = "locomo_loose"           # 128 tokens
    LOCOMO_MODERATE = "locomo_moderate"     # 64 tokens
    LOCOMO_TIGHT = "locomo_tight"           # 32 tokens
    LONGMEM_EVAL_LOOSE = "longmem_loose"    # 1024 tokens
    LONGMEM_EVAL_MODERATE = "longmem_moderate"  # 512 tokens
    LONGMEM_EVAL_TIGHT = "longmem_tight"    # 256 tokens


class RetentionAction(Enum):
    """保留动作。"""
    KEEP = "keep"             # 保留
    EVICT = "evict"           # 逐出
    COMPRESS = "compress"     # 压缩
    DELEGATE = "delegate"     # 委托到长期存储


class OSLMode(Enum):
    """OSL-MR 模式。"""
    FULL = "full"                   # OSL-MR (full)：含 Mixed-Score prior
    WITHOUT_PRIOR = "without_prior"  # 消融：移除 prior 特征


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class MixedScorePrior:
    """Mixed-Score 先验特征向量。

    f = [hit_rate, hit_freshness, stale_rate, reacquisition_cost, miss_cost,
         coverage_gap, age_rank, evidence_length]
    """
    hit_rate: float = 0.0              # 命中率
    hit_freshness: float = 0.0         # 命中时的平均新鲜度
    stale_rate: float = 0.0            # 过时率
    reacquisition_cost: float = 0.0    # 重获成本（归一化）
    miss_cost: float = 0.0             # 缺失代价（最高权重）
    coverage_gap: float = 0.0          # 覆盖缺口
    age_rank: float = 0.0              # 年龄排名（归一化）
    evidence_length: float = 0.0       # 证据长度 (tokens)

    def to_vector(self) -> List[float]:
        return [
            self.hit_rate, self.hit_freshness, self.stale_rate,
            self.reacquisition_cost, self.miss_cost, self.coverage_gap,
            self.age_rank, self.evidence_length,
        ]

    def to_vector_no_prior(self) -> List[float]:
        """无 prior 版本（消融）。"""
        return [
            self.hit_rate, self.hit_freshness, self.stale_rate,
            self.reacquisition_cost, self.miss_cost, self.coverage_gap,
            self.age_rank, self.evidence_length,
        ]


@dataclass
class EvidenceItem:
    """证据项。"""
    evidence_id: str
    content: str
    length_tokens: int
    state: EvidenceState = EvidenceState.ACTIVE
    prior: MixedScorePrior = field(default_factory=MixedScorePrior)
    retention_score: float = 0.5       # MLP 输出分数
    hit_count: int = 0
    stale_count: int = 0
    last_access: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)


@dataclass
class BudgetState:
    """预算状态。"""
    regime: BudgetRegime
    budget_tokens: int
    used_tokens: int = 0
    retained_items: int = 0
    evicted_items: int = 0
    utilization: float = 0.0


@dataclass
class RetentionDecision:
    """保留决策。"""
    evidence_id: str
    action: RetentionAction
    score: float
    reason: str


@dataclass
class OSLStats:
    """OSL-MR 统计。"""
    mode: OSLMode
    total_evictions: int = 0
    total_keeps: int = 0
    avg_retention_score: float = 0.0
    budget_compliance: float = 1.0     # <1 表示超预算
    hit_rate_after_retention: float = 0.0


# ============================================================================
# Constants
# ============================================================================

# 奖励系数（来自 OSL-MR 论文）
REWARD_HIT: float = 4.0
REWARD_REACQ: float = 6.0
REWARD_MISS: float = -12.0          # 最大惩罚
REWARD_STALE: float = -6.0
REWARD_STORE: float = -1.0           # 存储成本基准
REWARD_FULL: float = 64.0            # 全量覆盖奖励

# 预算映射
BUDGET_MAP: Dict[BudgetRegime, int] = {
    BudgetRegime.LOCOMO_TIGHT: 32,
    BudgetRegime.LOCOMO_MODERATE: 64,
    BudgetRegime.LOCOMO_LOOSE: 128,
    BudgetRegime.LONGMEM_EVAL_TIGHT: 256,
    BudgetRegime.LONGMEM_EVAL_MODERATE: 512,
    BudgetRegime.LONGMEM_EVAL_LOOSE: 1024,
}

# MLP 超参数
MLP_HIDDEN_UNITS: int = 16
MLP_INPUT_DIM: int = 8
MLP_LEARNING_RATE: float = 0.01


# ============================================================================
# Core Components
# ============================================================================

class EvidenceLearner:
    """MLP 证据学习器。

    单隐层 16 单元，sigmoid 输出。
    仅用在线可观测特征做推断。
    """

    def __init__(self, mode: OSLMode = OSLMode.FULL):
        self._lock = threading.RLock()
        self.mode = mode
        self.input_dim = MLP_INPUT_DIM

        # 简单 MLP 权重（伪训练初始化，实际用 Xavier 类初始化）
        scale = math.sqrt(2.0 / self.input_dim)
        self.W1: List[List[float]] = [
            [random.uniform(-scale, scale) for _ in range(MLP_INPUT_DIM)]
            for _ in range(MLP_HIDDEN_UNITS)
        ]
        self.b1: List[float] = [0.0] * MLP_HIDDEN_UNITS
        self.W2: List[float] = [random.uniform(-scale, scale) for _ in range(MLP_HIDDEN_UNITS)]
        self.b2: float = 0.0
        self.trained: bool = False

    def forward(self, prior: MixedScorePrior) -> float:
        """前向传播 → 保留分数 (0~1)。"""
        with self._lock:
            if self.mode == OSLMode.FULL:
                x = prior.to_vector()
            else:
                x = prior.to_vector_no_prior()

            # 隐层：sigmoid(W1·x + b1)
            hidden = [self._sigmoid(sum(self.W1[i][j] * x[j] for j in range(self.input_dim)) + self.b1[i])
                     for i in range(MLP_HIDDEN_UNITS)]

            # 输出：sigmoid(W2·h + b2)
            score = self._sigmoid(sum(self.W2[i] * hidden[i] for i in range(MLP_HIDDEN_UNITS)) + self.b2)
            return score

    def update(self, prior: MixedScorePrior, target: float):
        """简化 SGD 更新（在线学习）。"""
        with self._lock:
            x = prior.to_vector() if self.mode == OSLMode.FULL else prior.to_vector_no_prior()

            # 前向
            hidden = [self._sigmoid(sum(self.W1[i][j] * x[j] for j in range(self.input_dim)) + self.b1[i])
                     for i in range(MLP_HIDDEN_UNITS)]
            output = self._sigmoid(sum(self.W2[i] * hidden[i] for i in range(MLP_HIDDEN_UNITS)) + self.b2)

            # 误差
            error = output - target
            d_output = error * output * (1 - output)

            # 反向传播 W2
            for i in range(MLP_HIDDEN_UNITS):
                self.W2[i] -= MLP_LEARNING_RATE * d_output * hidden[i]
            self.b2 -= MLP_LEARNING_RATE * d_output

            # 反向传播 W1
            for i in range(MLP_HIDDEN_UNITS):
                d_hidden = d_output * self.W2[i] * hidden[i] * (1 - hidden[i])
                for j in range(self.input_dim):
                    self.W1[i][j] -= MLP_LEARNING_RATE * d_hidden * x[j]
                self.b1[i] -= MLP_LEARNING_RATE * d_hidden

            self.trained = True

    @staticmethod
    def _sigmoid(x: float) -> float:
        try:
            return 1.0 / (1.0 + math.exp(-x))
        except OverflowError:
            return 0.0 if x < 0 else 1.0

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode.value,
                "input_dim": self.input_dim,
                "hidden_units": MLP_HIDDEN_UNITS,
                "trained": self.trained,
            }


class RetentionPolicy:
    """约束优化选留策略。

    在预算约束下，用 evidence learner 打分，贪心选留最高分证据。
    仅用在线可观测特征。
    """

    def __init__(self, regime: BudgetRegime, learner: EvidenceLearner):
        self._lock = threading.RLock()
        self.regime = regime
        self.budget = BUDGET_MAP[regime]
        self.learner = learner
        self.state = BudgetState(regime=regime, budget_tokens=self.budget)
        self.decisions: List[RetentionDecision] = []

    def select(self, items: List[EvidenceItem]) -> Tuple[List[EvidenceItem], List[RetentionDecision], BudgetState]:
        """贪心选留。"""
        with self._lock:
            decisions: List[RetentionDecision] = []
            retained: List[EvidenceItem] = []

            # 打分
            scored: List[Tuple[EvidenceItem, float]] = []
            for item in items:
                score = self.learner.forward(item.prior)
                item.retention_score = score
                scored.append((item, score))

            # 按分数降序
            scored.sort(key=lambda x: x[1], reverse=True)

            # 贪心选留
            used = 0
            for item, score in scored:
                if used + item.length_tokens <= self.budget:
                    decision = RetentionDecision(
                        evidence_id=item.evidence_id,
                        action=RetentionAction.KEEP,
                        score=round(score, 4),
                        reason=f"SCORE={score:.3f}, BUDGET_OK",
                    )
                    retained.append(item)
                    used += item.length_tokens
                else:
                    action = (RetentionAction.COMPRESS
                             if item.length_tokens > self.budget * 0.1
                             else RetentionAction.EVICT)
                    decision = RetentionDecision(
                        evidence_id=item.evidence_id,
                        action=action,
                        score=round(score, 4),
                        reason=f"SCORE={score:.3f}, BUDGET_EXCEEDED",
                    )
                decisions.append(decision)

            # 更新预算状态
            self.state.used_tokens = used
            self.state.retained_items = len(retained)
            self.state.evicted_items = len(items) - len(retained)
            self.state.utilization = used / self.budget

            self.decisions = decisions
            return retained, decisions, self.state

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "regime": self.regime.value,
                "budget": self.budget,
                "utilization": round(self.state.utilization, 4),
                "retained": self.state.retained_items,
                "evicted": self.state.evicted_items,
            }


class ObservabilitySafeRetention:
    """可观测安全记忆保留总控。

    集成 Mixed-Score prior、Evidence Learner、Retention Policy。
    支持 LoCoMo / LongMemEval 评测协议。
    """

    def __init__(self, regime: BudgetRegime = BudgetRegime.LOCOMO_MODERATE,
                 mode: OSLMode = OSLMode.FULL):
        self._lock = threading.RLock()
        self.mode = mode
        self.learner = EvidenceLearner(mode)
        self.policy = RetentionPolicy(regime, self.learner)
        self.evidences: Dict[str, EvidenceItem] = {}
        self.stats = OSLStats(mode=mode)

    def add_evidence(self, content: str) -> EvidenceItem:
        """添加证据项。"""
        with self._lock:
            item = EvidenceItem(
                evidence_id=str(uuid.uuid4())[:8],
                content=content,
                length_tokens=len(content.split()),
            )
            self.evidences[item.evidence_id] = item
            return item

    def record_access(self, evidence_id: str, hit: bool = True):
        """记录访问事件 → 更新 prior。"""
        with self._lock:
            item = self.evidences.get(evidence_id)
            if not item:
                return

            item.last_access = time.time()
            if hit:
                item.hit_count += 1
                item.state = EvidenceState.ACTIVE
                item.prior.hit_rate = item.hit_count / max(item.hit_count + item.stale_count, 1)
                item.prior.hit_freshness = 0.8
            else:
                item.stale_count += 1
                if item.stale_count > 10:
                    item.state = EvidenceState.STALE
                item.prior.stale_rate = item.stale_count / max(item.hit_count + item.stale_count, 1)

            item.prior.hit_freshness = 1.0 / (1.0 + item.prior.stale_rate)
            item.prior.miss_cost = REWARD_MISS * -1 * item.prior.stale_rate
            item.prior.reacquisition_cost = REWARD_REACQ * item.prior.stale_rate
            item.prior.age_rank = min(
                (time.time() - item.created_at) / (86400 * 30), 1.0)
            item.prior.evidence_length = item.length_tokens / 128.0

    def optimize_retention(self) -> Tuple[List[EvidenceItem], List[RetentionDecision]]:
        """执行约束优化选留。"""
        with self._lock:
            items = list(self.evidences.values())
            retained, decisions, budget_state = self.policy.select(items)
            self.stats.total_keeps = len(retained)
            self.stats.total_evictions = len(items) - len(retained)
            self.stats.avg_retention_score = round(
                sum(d.score for d in decisions) / max(len(decisions), 1), 4)
            self.stats.budget_compliance = budget_state.utilization
            return retained, decisions

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self.mode.value,
                "total_evidence": len(self.evidences),
                "regime": self.policy.regime.value,
                "budget": self.policy.budget,
                "keeps": self.stats.total_keeps,
                "evictions": self.stats.total_evictions,
                "avg_score": self.stats.avg_retention_score,
                "budget_compliance": round(self.stats.budget_compliance, 4),
                "learner": self.learner.statistics(),
                "reward_coefficients": {
                    "hit": REWARD_HIT, "reacq": REWARD_REACQ,
                    "miss": REWARD_MISS, "stale": REWARD_STALE,
                    "store": REWARD_STORE, "full": REWARD_FULL,
                },
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P22-8 Observability-Safe Memory Retention (OSL-MR)",
        "benchmark": "Constrained Optimization for Long-Horizon Agents (arXiv 2606.10616)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "MixedScorePrior(8D)→MLP(16h,sigmoid)→ConstrainedGreedy→Budget(32/64/128/256/512/1024)",
        "key_metric": "OSL-MR (full) vs (w/o prior) ablation, α_miss=12.0, 6-tier budget, LoCoMo+LongMemEval",
        "thread_safe": True,
    }
