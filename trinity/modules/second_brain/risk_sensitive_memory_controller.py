
"""
# status: orphan (2026-08-15 audit, not in runtime path)
P19-4: Risk-Sensitive Memory Controller — 风险敏感记忆控制

对标论文: RSCB-MC (arXiv 2604.27283, 2026.04)
核心发现: Pattern-Variant-Episode 三层记忆 + 16 特征上下文 + 风险敏感 Bandit 7 动作 + 误报惩罚 + 弃权决策
三元语: 存储(Store P-V-E) → 评估(Assess Risk) → 决策(Bandit Decide) → 闭环(Feedback Loop)

设计要点:
- PatternVariantEpisodeStore: Pattern(问题模式)/Variant(变体)/Episode(具体实例)三层存储
- ContextualStateExtractor: 提取16特征——相关性/不确定性/结构兼容/反馈历史/误报风险/延迟/Token成本
- RiskSensitiveBanditController: 7种动作——无记忆/注入最佳/摘要多候选/高精检索/高召检索/弃权/反馈请求
- FalsePositivePenalizer: 误报惩罚策略，惩罚误报记忆注入远多于漏用
- AbstentionDecisionEngine: 检索置信度低于阈值时主动弃权
- FeedbackLoopCollector: 收集注入后结果(正确/错误/无关)，闭环更新 Bandit 策略
- 与 P3 critic.py / P14-2 confidence_scored_retrieval.py 互补
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class PatternCategory(Enum):
    """问题模式分类"""
    REASONING = "reasoning"
    CODING = "coding"
    PLANNING = "planning"
    RETRIEVAL = "retrieval"
    CONVERSATION = "conversation"


class BanditAction(Enum):
    """风险敏感 Bandit 动作 (7种)"""
    NO_MEMORY = "no_memory"
    INJECT_BEST = "inject_best"
    SUMMARIZE_MULTI_CANDIDATE = "summarize_multi_candidate"
    HIGH_PRECISION_RETRIEVAL = "high_precision_retrieval"
    HIGH_RECALL_RETRIEVAL = "high_recall_retrieval"
    ABSTAIN = "abstain"
    REQUEST_FEEDBACK = "request_feedback"


class FeedbackOutcome(Enum):
    """反馈结果"""
    CORRECT = "correct"
    INCORRECT = "incorrect"
    IRRELEVANT = "irrelevant"
    HARMFUL = "harmful"


class AbstentionReason(Enum):
    """弃权原因"""
    LOW_CONFIDENCE = "low_confidence"
    HIGH_RISK = "high_risk"
    DOMAIN_MISMATCH = "domain_mismatch"
    UNTESTED_PATTERN = "untested_pattern"
    FALSE_POSITIVE_RISK = "false_positive_risk"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ContextualState:
    """16 特征上下文状态"""
    relevance: float           # 相关性
    uncertainty: float         # 不确定性
    structural_compatibility: float  # 结构兼容
    feedback_history_pos: float    # 正向反馈历史
    feedback_history_neg: float    # 负向反馈历史
    false_positive_risk: float     # 误报风险
    latency_ms: float              # 延迟
    token_cost: float              # Token 成本
    memory_recency: float          # 记忆新鲜度
    source_credibility: float      # 来源可信度
    domain_similarity: float       # 领域相似度
    content_length: float          # 内容长度
    retrieval_depth: float         # 检索深度
    ambiguity_score: float        # 歧义评分
    novelty_score: float          # 新颖性评分
    semantic_distance: float      # 语义距离


@dataclass
class VariantEntry:
    """问题变体条目"""
    variant_id: str
    pattern_id: str
    context_fingerprint: str  # 16 特征哈希
    description: str
    occurrence_count: int
    last_seen: float


@dataclass
class EpisodeEntry:
    """具体实例条目"""
    episode_id: str
    variant_id: str
    pattern_id: str
    context: ContextualState
    action_taken: BanditAction
    outcome: FeedbackOutcome
    reward: float
    timestamp: float
    trace: str


@dataclass
class PatternRecord:
    """问题模式记录"""
    pattern_id: str
    category: PatternCategory
    name: str
    description: str
    variants: List[str]  # variant_ids
    total_episodes: int
    success_rate: float
    last_action_distribution: Dict[str, float]


@dataclass
class BanditLearner:
    """Bandit 学习器状态"""
    action: BanditAction
    q_value: float
    pull_count: int
    success_count: int
    ucb: float


@dataclass
class AbstentionDecision:
    """弃权决策"""
    should_abstain: bool
    reason: AbstentionReason
    confidence: float
    threshold: float
    risk_context: str


@dataclass
class FeedbackRecord:
    """反馈闭环记录"""
    record_id: str
    episode_id: str
    pattern_id: str
    action: BanditAction
    outcome: FeedbackOutcome
    reward_delta: float
    policy_update: Dict[str, float]
    timestamp: float


# ============================================================================
# PatternVariantEpisodeStore
# ============================================================================

class PatternVariantEpisodeStore:
    """Pattern-Variant-Episode 三层模式存储

    Pattern(问题模式) → Variant(变体) → Episode(具体实例)，三层递归检索。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.patterns: Dict[str, PatternRecord] = {}
        self.variants: Dict[str, VariantEntry] = {}
        self.episodes: Dict[str, EpisodeEntry] = {}
        self.pattern_variant_index: Dict[str, List[str]] = defaultdict(list)  # pattern_id -> variant_ids
        self.variant_episode_index: Dict[str, List[str]] = defaultdict(list)  # variant_id -> episode_ids

        logger.info("PatternVariantEpisodeStore initialized")

    def register_pattern(self, name: str, category: PatternCategory, description: str = "") -> PatternRecord:
        """注册问题模式"""
        with self._lock:
            pattern_id = f"pattern-{uuid.uuid4().hex[:12]}"
            pattern = PatternRecord(
                pattern_id=pattern_id,
                category=category,
                name=name,
                description=description,
                variants=[],
                total_episodes=0,
                success_rate=0.0,
                last_action_distribution={},
            )
            self.patterns[pattern_id] = pattern
            return pattern

    def register_variant(self, pattern_id: str, context: ContextualState, description: str = "") -> Optional[VariantEntry]:
        """注册问题变体"""
        with self._lock:
            if pattern_id not in self.patterns:
                return None

            fp = hashlib.md5(json.dumps([
                context.relevance, context.uncertainty, context.structural_compatibility,
                context.false_positive_risk, context.source_credibility,
            ]).encode()).hexdigest()[:16]

            variant_id = f"variant-{uuid.uuid4().hex[:12]}"
            variant = VariantEntry(
                variant_id=variant_id,
                pattern_id=pattern_id,
                context_fingerprint=fp,
                description=description,
                occurrence_count=1,
                last_seen=time.time(),
            )
            self.variants[variant_id] = variant
            self.pattern_variant_index[pattern_id].append(variant_id)
            self.patterns[pattern_id].variants.append(variant_id)
            return variant

    def record_episode(self, variant_id: str, pattern_id: str, context: ContextualState,
                       action: BanditAction, outcome: FeedbackOutcome, reward: float, trace: str = "") -> Optional[EpisodeEntry]:
        """记录具体实例"""
        with self._lock:
            if variant_id not in self.variants:
                return None

            episode = EpisodeEntry(
                episode_id=f"episode-{uuid.uuid4().hex[:12]}",
                variant_id=variant_id,
                pattern_id=pattern_id,
                context=context,
                action_taken=action,
                outcome=outcome,
                reward=reward,
                timestamp=time.time(),
                trace=trace,
            )
            self.episodes[episode.episode_id] = episode
            self.variant_episode_index[variant_id].append(episode.episode_id)

            pattern = self.patterns.get(pattern_id)
            if pattern:
                pattern.total_episodes += 1
                successes = sum(1 for eid in [eid for vids in [
                    self.pattern_variant_index.get(pid, []) for pid in [pattern_id]
                ] for eid in [self.variant_episode_index.get(vid, []) for vid in vids if vid in self.variant_episode_index] for eid in eid] if eid in self.episodes and self.episodes[eid].outcome == FeedbackOutcome.CORRECT)
                pattern.success_rate = successes / max(pattern.total_episodes, 1)

                # Update action distribution
                all_eps = [
                    self.episodes[eid]
                    for vid in self.pattern_variant_index.get(pattern_id, [])
                    for eid in self.variant_episode_index.get(vid, [])
                    if eid in self.episodes
                ]
                action_counts = defaultdict(int)
                for ep in all_eps:
                    action_counts[ep.action_taken.value] += 1
                total = sum(action_counts.values()) or 1
                pattern.last_action_distribution = {
                    k: v / total for k, v in action_counts.items()
                }

            return episode

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_patterns": len(self.patterns),
                "total_variants": len(self.variants),
                "total_episodes": len(self.episodes),
                "avg_variants_per_pattern": len(self.variants) / max(len(self.patterns), 1),
                "avg_episodes_per_variant": len(self.episodes) / max(len(self.variants), 1),
            }


# ============================================================================
# ContextualStateExtractor
# ============================================================================

class ContextualStateExtractor:
    """上下文状态提取器

    提取 16 特征: 相关性/不确定性/结构兼容/反馈历史/误报风险/延迟/Token成本/记忆新鲜度/
    来源可信度/领域相似度/内容长度/检索深度/歧义评分/新颖性评分/语义距离。
    """

    FEATURE_NAMES = [
        "relevance", "uncertainty", "structural_compatibility",
        "feedback_history_pos", "feedback_history_neg", "false_positive_risk",
        "latency_ms", "token_cost", "memory_recency", "source_credibility",
        "domain_similarity", "content_length", "retrieval_depth",
        "ambiguity_score", "novelty_score", "semantic_distance",
    ]

    def __init__(self):
        self._lock = threading.RLock()
        self.extraction_history: List[ContextualState] = []

        logger.info("ContextualStateExtractor initialized (%d features)", len(self.FEATURE_NAMES))

    def extract(
        self,
        features: Optional[Dict[str, float]] = None,
        default_context: Optional[str] = None,
    ) -> ContextualState:
        """从特征字典或上下文提取 16 维状态"""
        with self._lock:
            if features:
                state = ContextualState(
                    relevance=features.get("relevance", 0.5),
                    uncertainty=features.get("uncertainty", 0.3),
                    structural_compatibility=features.get("structural_compatibility", 0.6),
                    feedback_history_pos=features.get("feedback_history_pos", 0.0),
                    feedback_history_neg=features.get("feedback_history_neg", 0.0),
                    false_positive_risk=features.get("false_positive_risk", 0.1),
                    latency_ms=features.get("latency_ms", 50.0),
                    token_cost=features.get("token_cost", 100.0),
                    memory_recency=features.get("memory_recency", 0.5),
                    source_credibility=features.get("source_credibility", 0.7),
                    domain_similarity=features.get("domain_similarity", 0.6),
                    content_length=features.get("content_length", 200.0),
                    retrieval_depth=features.get("retrieval_depth", 2.0),
                    ambiguity_score=features.get("ambiguity_score", 0.2),
                    novelty_score=features.get("novelty_score", 0.3),
                    semantic_distance=features.get("semantic_distance", 0.4),
                )
            else:
                seed = hash(default_context or "") % (2 ** 31)
                np.random.seed(seed)
                state = ContextualState(
                    relevance=np.random.uniform(0.3, 0.9),
                    uncertainty=np.random.uniform(0.1, 0.5),
                    structural_compatibility=np.random.uniform(0.4, 0.9),
                    feedback_history_pos=np.random.uniform(0.0, 0.3),
                    feedback_history_neg=np.random.uniform(0.0, 0.2),
                    false_positive_risk=np.random.uniform(0.05, 0.3),
                    latency_ms=np.random.uniform(10, 200),
                    token_cost=np.random.uniform(50, 500),
                    memory_recency=np.random.uniform(0.1, 0.9),
                    source_credibility=np.random.uniform(0.5, 0.95),
                    domain_similarity=np.random.uniform(0.3, 0.8),
                    content_length=np.random.uniform(50, 500),
                    retrieval_depth=np.random.uniform(1, 5),
                    ambiguity_score=np.random.uniform(0.1, 0.6),
                    novelty_score=np.random.uniform(0.1, 0.7),
                    semantic_distance=np.random.uniform(0.1, 0.6),
                )
                np.random.seed(None)

            self.extraction_history.append(state)
            return state

    def to_vector(self, state: ContextualState) -> np.ndarray:
        """将上下文状态转为 16 维向量"""
        return np.array([
            state.relevance, state.uncertainty, state.structural_compatibility,
            state.feedback_history_pos, state.feedback_history_neg, state.false_positive_risk,
            state.latency_ms / 500.0, state.token_cost / 500.0,
            state.memory_recency, state.source_credibility,
            state.domain_similarity, state.content_length / 500.0,
            state.retrieval_depth / 10.0, state.ambiguity_score,
            state.novelty_score, state.semantic_distance,
        ], dtype=np.float32)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_extractions": len(self.extraction_history),
                "feature_count": len(self.FEATURE_NAMES),
                "feature_names": self.FEATURE_NAMES,
            }


# ============================================================================
# RiskSensitiveBanditController
# ============================================================================

class RiskSensitiveBanditController:
    """风险敏感 Bandit 控制器

    7 种动作选择: 无记忆/注入最佳/摘要多候选/高精检索/高召检索/弃权/反馈请求。
    使用 UCB 算法平衡探索与利用，风险敏感效用函数惩罚高风险动作。
    """

    RISK_WEIGHTS = {
        BanditAction.NO_MEMORY: 0.0,
        BanditAction.INJECT_BEST: 0.05,
        BanditAction.SUMMARIZE_MULTI_CANDIDATE: 0.03,
        BanditAction.HIGH_PRECISION_RETRIEVAL: 0.04,
        BanditAction.HIGH_RECALL_RETRIEVAL: 0.06,
        BanditAction.ABSTAIN: 0.01,
        BanditAction.REQUEST_FEEDBACK: 0.02,
    }

    def __init__(self, exploration_coefficient: float = 2.0, risk_aversion: float = 1.0):
        self.exploration_coefficient = exploration_coefficient
        self.risk_aversion = risk_aversion
        self._lock = threading.RLock()
        self.learners: Dict[str, Dict[str, BanditLearner]] = defaultdict(dict)  # pattern_id -> action_name -> learner
        self.decision_history: List[Tuple[str, BanditAction, float]] = []
        self._init_all_learners("__global__")

        logger.info("RiskSensitiveBanditController initialized (c=%.2f, risk_aversion=%.2f)", exploration_coefficient, risk_aversion)

    def _init_all_learners(self, pattern_id: str) -> None:
        for action in BanditAction:
            self.learners[pattern_id][action.value] = BanditLearner(
                action=action, q_value=0.0, pull_count=1, success_count=0, ucb=0.0,
            )

    def _compute_ucb(self, learner: BanditLearner, total_pulls: int) -> float:
        if learner.pull_count == 0:
            return float("inf")
        exploration = self.exploration_coefficient * math.sqrt(math.log(total_pulls) / learner.pull_count)
        risk_penalty = self.risk_aversion * self.RISK_WEIGHTS.get(learner.action, 0.0)
        return learner.q_value + exploration - risk_penalty

    def select_action(self, pattern_id: str, context: ContextualState) -> BanditAction:
        """选择最优 Bandit 动作"""
        with self._lock:
            if pattern_id not in self.learners:
                self._init_all_learners(pattern_id)

            learners = self.learners[pattern_id]
            total_pulls = sum(l.pull_count for l in learners.values())

            best_action = BanditAction.NO_MEMORY
            best_ucb = -float("inf")

            for action_name, learner in learners.items():
                learner.ucb = self._compute_ucb(learner, total_pulls)
                if learner.ucb > best_ucb:
                    best_ucb = learner.ucb
                    best_action = learner.action

            self.decision_history.append((pattern_id, best_action, time.time()))
            return best_action

    def update(self, pattern_id: str, action: BanditAction, reward: float) -> None:
        """更新 Bandit Q 值"""
        with self._lock:
            if pattern_id not in self.learners:
                self._init_all_learners(pattern_id)

            learner = self.learners[pattern_id].get(action.value)
            if not learner:
                return

            learner.pull_count += 1
            if reward > 0:
                learner.success_count += 1
            learner.q_value += (reward - learner.q_value) / learner.pull_count

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_decisions = len(self.decision_history)
            action_counts = defaultdict(int)
            for _, action, _ in self.decision_history:
                action_counts[action.value] += 1

            return {
                "total_decisions": total_decisions,
                "action_distribution": {k: v / max(total_decisions, 1) for k, v in action_counts.items()},
                "patterns_tracked": len(self.learners),
                "avg_q_value": float(np.mean([
                    l.q_value for pl in self.learners.values() for l in pl.values()
                ])),
            }


# ============================================================================
# FalsePositivePenalizer
# ============================================================================

class FalsePositivePenalizer:
    """误报惩罚策略

    惩罚误报记忆注入远多于漏用，确保"宁可不用也不错用"。
    误报惩罚系数默认为漏用惩罚的 5 倍。
    """

    def __init__(self, fp_penalty_multiplier: float = 5.0, base_reward: float = 1.0):
        self.fp_penalty_multiplier = fp_penalty_multiplier
        self.base_reward = base_reward
        self._lock = threading.RLock()
        self.false_positive_count: int = 0
        self.false_negative_count: int = 0
        self.total_injections: int = 0

        logger.info("FalsePositivePenalizer initialized (fp_mult=%.1fx)", fp_penalty_multiplier)

    def compute_reward(self, outcome: FeedbackOutcome, is_injection: bool = True) -> float:
        """根据结果计算奖励"""
        with self._lock:
            if is_injection:
                self.total_injections += 1

            if outcome == FeedbackOutcome.CORRECT:
                return self.base_reward
            elif outcome == FeedbackOutcome.INCORRECT:
                if is_injection:
                    self.false_positive_count += 1
                    return -self.base_reward * self.fp_penalty_multiplier
                else:
                    self.false_negative_count += 1
                    return -self.base_reward
            elif outcome == FeedbackOutcome.IRRELEVANT:
                if is_injection:
                    self.false_positive_count += 1
                    return -self.base_reward * self.fp_penalty_multiplier * 0.5
                return -self.base_reward * 0.3
            elif outcome == FeedbackOutcome.HARMFUL:
                if is_injection:
                    self.false_positive_count += 1
                    return -self.base_reward * self.fp_penalty_multiplier * 2.0
                return -self.base_reward * self.fp_penalty_multiplier
            return 0.0

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "false_positives": self.false_positive_count,
                "false_negatives": self.false_negative_count,
                "total_injections": self.total_injections,
                "fp_rate": self.false_positive_count / max(self.total_injections, 1),
                "fp_penalty_multiplier": self.fp_penalty_multiplier,
            }


# ============================================================================
# AbstentionDecisionEngine
# ============================================================================

class AbstentionDecisionEngine:
    """弃权决策引擎

    当检索置信度低于阈值时主动弃权，不注入可能有害的记忆。
    """

    def __init__(self, confidence_threshold: float = 0.35, high_risk_threshold: float = 0.7):
        self.confidence_threshold = confidence_threshold
        self.high_risk_threshold = high_risk_threshold
        self._lock = threading.RLock()
        self.abstention_history: List[AbstentionDecision] = []

        logger.info("AbstentionDecisionEngine initialized (conf=%.2f, risk=%.2f)", confidence_threshold, high_risk_threshold)

    def should_abstain(self, context: ContextualState, retrieval_confidence: float) -> AbstentionDecision:
        """判断是否应弃权"""
        with self._lock:
            reasons: List[AbstentionReason] = []

            if retrieval_confidence < self.confidence_threshold:
                reasons.append(AbstentionReason.LOW_CONFIDENCE)
            if context.false_positive_risk > self.high_risk_threshold:
                reasons.append(AbstentionReason.HIGH_RISK)
            if context.domain_similarity < 0.2:
                reasons.append(AbstentionReason.DOMAIN_MISMATCH)
            if context.ambiguity_score > 0.6:
                reasons.append(AbstentionReason.UNTESTED_PATTERN)
            if context.false_positive_risk > 0.5 and retrieval_confidence < 0.5:
                reasons.append(AbstentionReason.FALSE_POSITIVE_RISK)

            should = len(reasons) > 0
            primary_reason = reasons[0] if reasons else AbstentionReason.LOW_CONFIDENCE

            decision = AbstentionDecision(
                should_abstain=should,
                reason=primary_reason,
                confidence=retrieval_confidence,
                threshold=self.confidence_threshold,
                risk_context=f"FP risk={context.false_positive_risk:.2f}, domain_sim={context.domain_similarity:.2f}",
            )
            if should:
                self.abstention_history.append(decision)
            return decision

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_abstentions": len(self.abstention_history),
                "by_reason": {
                    r.value: sum(1 for d in self.abstention_history if d.reason == r)
                    for r in AbstentionReason
                },
                "confidence_threshold": self.confidence_threshold,
            }


# ============================================================================
# FeedbackLoopCollector
# ============================================================================

class FeedbackLoopCollector:
    """反馈闭环收集器

    收集注入后结果(正确/错误/无关)，闭环更新 Bandit 策略。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.feedback_records: List[FeedbackRecord] = []

        logger.info("FeedbackLoopCollector initialized")

    def collect(
        self,
        episode_id: str,
        pattern_id: str,
        action: BanditAction,
        outcome: FeedbackOutcome,
        reward_delta: float,
        policy_update: Optional[Dict[str, float]] = None,
    ) -> FeedbackRecord:
        """收集反馈"""
        with self._lock:
            record = FeedbackRecord(
                record_id=f"feedback-{uuid.uuid4().hex[:12]}",
                episode_id=episode_id,
                pattern_id=pattern_id,
                action=action,
                outcome=outcome,
                reward_delta=reward_delta,
                policy_update=policy_update or {},
                timestamp=time.time(),
            )
            self.feedback_records.append(record)
            return record

    def get_outcome_distribution(self) -> Dict[str, float]:
        """获取结果分布"""
        with self._lock:
            counts = defaultdict(int)
            for r in self.feedback_records:
                counts[r.outcome.value] += 1
            total = sum(counts.values()) or 1
            return {k: v / total for k, v in counts.items()}

    def get_cumulative_reward(self) -> float:
        """获取累积奖励"""
        with self._lock:
            return sum(r.reward_delta for r in self.feedback_records)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_feedback_records": len(self.feedback_records),
                "outcome_distribution": self.get_outcome_distribution(),
                "cumulative_reward": self.get_cumulative_reward(),
                "avg_reward_per_feedback": self.get_cumulative_reward() / max(len(self.feedback_records), 1),
            }
