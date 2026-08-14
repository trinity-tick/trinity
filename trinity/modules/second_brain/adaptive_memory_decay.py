"""
P13-8: Adaptive Memory Decay
=============================

对标 Agent Patterns Catalog — 自适应记忆衰减调度器。

设计要点：
  - 替代现有静态衰减调度，实现基于三信号的个性化衰减曲线
  - RetentionScorer：综合访问频率 / 语义相关性 / 最近访问时间的三维评分器
  - AdaptiveDecayScheduler：为每条记忆计算个性化衰减曲线与保留分
  - ThresholdHandler：按分数将记忆降级 / 融合 / 遗忘
  - reinforce()：在记忆被使用时提升保留分

接口兼容：
  - episodic_rl.py MemoryDecayScheduler：可替换为 AdaptiveDecayScheduler
  - memory_growth.py：衰减后记忆可被 growth 回收
  - memory_streaming.py：衰减/遗忘触发流事件
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class MemoryTier(Enum):
    """记忆层级——从热到冷。"""
    HOT = "hot"             # 活跃使用，保留分 > 0.8
    WARM = "warm"           # 偶尔使用，0.5-0.8
    COOL = "cool"           # 较少使用，0.3-0.5
    COLD = "cold"           # 极少使用，0.1-0.3
    FROZEN = "frozen"       # 冻结（归档），< 0.1
    ARCHIVED = "archived"   # 已归档，可恢复


class DecayAction(Enum):
    """衰减操作。"""
    NONE = "none"                    # 无操作
    WEAKEN = "weaken"                # 削弱保留分
    DEGRADE_TIER = "degrade_tier"    # 降级记忆层级
    MERGE = "merge"                  # 融合到更高级记忆
    FREEZE = "freeze"                # 冻结到冷存储
    ARCHIVE = "archive"              # 归档
    FORGET = "forget"                # 彻底遗忘
    REINFORCE = "reinforce"          # 强化


class AccessPattern(Enum):
    """访问模式。"""
    FREQUENT = "frequent"            # 高频访问（每小时级别）
    REGULAR = "regular"              # 常规访问（每天级别）
    OCCASIONAL = "occasional"        # 偶尔访问（每周级别）
    RARE = "rare"                    # 罕见访问（每月级别）
    DORMANT = "dormant"              # 休眠（超过一个月未访问）


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class RetentionSignal:
    """保留信号——三维评分输入。"""
    memory_id: str
    access_frequency: float = 0.0        # 访问频率（次/天）
    semantic_relevance: float = 0.0      # 语义相关性 (0-1)
    last_access_staleness: float = 0.0   # 最近访问距今（天）
    access_count: int = 0
    access_timestamps: List[float] = field(default_factory=list)
    semantic_embedding: Optional[np.ndarray] = None
    tags: List[str] = field(default_factory=list)


@dataclass
class DecayProfile:
    """衰减剖面——单条记忆的衰减曲线参数。"""
    memory_id: str
    tier: MemoryTier = MemoryTier.WARM
    retention_score: float = 0.8
    base_decay_rate: float = 0.01         # 基础衰减率（每天）
    half_life_days: float = 30.0          # 半衰期（天）
    last_reinforced: float = 0.0
    reinforcement_count: int = 0
    current_action: DecayAction = DecayAction.NONE
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class DecayDecision:
    """衰减决策。"""
    decision_id: str
    memory_id: str
    action: DecayAction
    previous_score: float
    new_score: float
    previous_tier: MemoryTier
    new_tier: MemoryTier
    reason: str = ""
    threshold_applied: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class SchedulerStats:
    """调度器统计信息。"""
    total_memories: int = 0
    hot_count: int = 0
    warm_count: int = 0
    cool_count: int = 0
    cold_count: int = 0
    frozen_count: int = 0
    archived_count: int = 0
    avg_retention_score: float = 0.0
    total_decay_events: int = 0
    total_reinforcements: int = 0
    total_forgotten: int = 0
    last_cycle_time: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# RetentionScorer
# ============================================================================

class RetentionScorer:
    """保留评分器。

    基于三信号的加权评分：
      - 访问频率 (access_frequency): 权重 0.4
      - 语义相关性 (semantic_relevance): 权重 0.35
      - 最近访问新鲜度 (recency): 权重 0.25

    替代静态衰减，动态调制每条记忆的保留分。
    """

    # 默认权重
    DEFAULT_WEIGHTS = {
        "access_frequency": 0.40,
        "semantic_relevance": 0.35,
        "recency": 0.25,
    }

    def __init__(
        self,
        weights: Optional[Dict[str, float]] = None,
        context_embedding: Optional[np.ndarray] = None,
        name: str = "retention_scorer",
    ) -> None:
        self._weights = weights or dict(self.DEFAULT_WEIGHTS)
        self._context_embedding = context_embedding
        self._name = name
        self._lock = threading.RLock()
        self._signals: Dict[str, RetentionSignal] = {}

    def register(self, signal: RetentionSignal) -> None:
        """注册记忆保留信号。"""
        with self._lock:
            self._signals[signal.memory_id] = signal

    def _frequency_score(self, access_frequency: float) -> float:
        """频率分：对数尺度归一化。"""
        return min(1.0, math.log2(access_frequency + 1) / 5)

    def _semantic_score(
        self,
        signal: RetentionSignal,
        context_embedding: Optional[np.ndarray] = None,
    ) -> float:
        """语义相关性评分。"""
        if signal.semantic_relevance > 0:
            return signal.semantic_relevance
        if (
            context_embedding is not None
            and signal.semantic_embedding is not None
        ):
            dot = np.dot(context_embedding, signal.semantic_embedding)
            norm_c = np.linalg.norm(context_embedding)
            norm_s = np.linalg.norm(signal.semantic_embedding)
            if norm_c > 0 and norm_s > 0:
                return max(0.0, min(1.0, float(dot / (norm_c * norm_s))))
        return 0.5  # 默认中等相关性

    def _recency_score(self, staleness_days: float) -> float:
        """新鲜度评分：指数衰减。"""
        return math.exp(-staleness_days / 7.0)  # 7天半衰期

    def score(
        self,
        memory_id: str,
        context_embedding: Optional[np.ndarray] = None,
    ) -> float:
        """计算综合保留分。"""
        signal = self._signals.get(memory_id)
        if signal is None:
            return 0.0

        freq = self._frequency_score(signal.access_frequency)
        sem = self._semantic_score(signal, context_embedding)
        rec = self._recency_score(signal.last_access_staleness)

        total = (
            self._weights["access_frequency"] * freq
            + self._weights["semantic_relevance"] * sem
            + self._weights["recency"] * rec
        )
        return round(total, 4)

    def score_all(
        self, context_embedding: Optional[np.ndarray] = None
    ) -> Dict[str, float]:
        """批量计算所有记忆的保留分。"""
        with self._lock:
            return {
                mid: self.score(mid, context_embedding)
                for mid in self._signals
            }

    def get_access_pattern(
        self, signal: RetentionSignal
    ) -> AccessPattern:
        """根据访问频率判断访问模式。"""
        f = signal.access_frequency
        if f >= 24:
            return AccessPattern.FREQUENT
        if f >= 1:
            return AccessPattern.REGULAR
        if f >= 0.14:
            return AccessPattern.OCCASIONAL
        if f >= 0.03:
            return AccessPattern.RARE
        return AccessPattern.DORMANT

    def get_stats(self) -> Dict[str, Any]:
        """获取评分器统计信息。"""
        with self._lock:
            return {
                "name": self._name,
                "registered_signals": len(self._signals),
                "weights": dict(self._weights),
            }


# ============================================================================
# AdaptiveDecayScheduler
# ============================================================================

class AdaptiveDecayScheduler:
    """自适应衰减调度器。

    为每条记忆维护个性化衰减剖面（DecayProfile），
    按周期运行衰减评估，决定降级/融合/遗忘等操作。

    替代静态衰减调度。
    对标 Agent Patterns Catalog。
    """

    # 层级阈值
    TIER_THRESHOLDS = {
        MemoryTier.HOT: 0.8,
        MemoryTier.WARM: 0.5,
        MemoryTier.COOL: 0.3,
        MemoryTier.COLD: 0.1,
        MemoryTier.FROZEN: 0.0,
    }

    def __init__(
        self,
        scorer: RetentionScorer,
        default_half_life_days: float = 30.0,
        cycle_interval_hours: float = 6.0,
        name: str = "adaptive_decay_scheduler",
    ) -> None:
        self._scorer = scorer
        self._default_half_life = default_half_life_days
        self._cycle_interval = cycle_interval_hours
        self._name = name
        self._lock = threading.RLock()
        self._profiles: Dict[str, DecayProfile] = {}
        self._decisions: List[DecayDecision] = []
        self._total_reinforcements: int = 0
        self._total_forgotten: int = 0
        self._last_cycle: float = 0.0

    def _score_to_tier(self, score: float) -> MemoryTier:
        """保留分映射到记忆层级。"""
        for tier, threshold in sorted(
            self.TIER_THRESHOLDS.items(), key=lambda x: -x[1]
        ):
            if score >= threshold:
                return tier
        return MemoryTier.ARCHIVED

    def _compute_decay_rate(
        self, profile: DecayProfile, signal: RetentionSignal
    ) -> float:
        """计算个性化衰减率。

        基于：基础速率 × (1 - 频率归一化) × (1 - 语义相关性)
        高频率 + 高相关 → 低衰减率；低频率 + 低相关 → 高衰减率。
        """
        freq_norm = min(1.0, signal.access_frequency / 10.0)
        sem_score = signal.semantic_relevance

        # 自适应调制
        adaptive_factor = (1.0 - freq_norm) * (1.0 - sem_score)
        decay = profile.base_decay_rate * (0.1 + 0.9 * adaptive_factor)
        return max(0.001, min(0.5, decay))

    def register(
        self,
        memory_id: str,
        signal: RetentionSignal,
        base_decay_rate: Optional[float] = None,
    ) -> DecayProfile:
        """注册记忆并创建衰减剖面。"""
        self._scorer.register(signal)
        score = self._scorer.score(memory_id)
        tier = self._score_to_tier(score)

        profile = DecayProfile(
            memory_id=memory_id,
            tier=tier,
            retention_score=score,
            base_decay_rate=base_decay_rate or 0.01,
            half_life_days=self._default_half_life,
        )
        with self._lock:
            self._profiles[memory_id] = profile
        return profile

    def reinforce(self, memory_id: str, boost: float = 0.15) -> Optional[DecayProfile]:
        """强化记忆——提升保留分。

        在记忆被成功检索或使用后调用。
        boost 参数控制单次强化的保留分提升幅度。

        Args:
            memory_id: 记忆 ID
            boost: 保留分提升幅度（0-1）

        Returns:
            更新后的衰减剖面，或 None（记忆未注册）
        """
        with self._lock:
            profile = self._profiles.get(memory_id)
            if profile is None:
                return None

            previous = profile.retention_score
            # 强化：分数提升但有上限
            new_score = min(1.0, previous + boost)
            # 衰减率随着强化降低（新鲜度提升）
            new_decay = max(0.001, profile.base_decay_rate * 0.5)

            profile.retention_score = new_score
            profile.base_decay_rate = new_decay
            profile.last_reinforced = time.time()
            profile.reinforcement_count += 1
            profile.updated_at = time.time()

            new_tier = self._score_to_tier(new_score)
            old_tier = profile.tier
            profile.tier = new_tier

            self._total_reinforcements += 1

            decision = DecayDecision(
                decision_id=str(uuid.uuid4())[:12],
                memory_id=memory_id,
                action=DecayAction.REINFORCE,
                previous_score=previous,
                new_score=new_score,
                previous_tier=old_tier,
                new_tier=new_tier,
                reason=f"Reinforced by {boost:.0%}",
            )
            self._decisions.append(decision)

            logger.debug(
                "Reinforced memory %s: %.4f → %.4f (tier: %s → %s)",
                memory_id, previous, new_score, old_tier.value, new_tier.value,
            )
            return profile

    def run_cycle(
        self,
        context_embedding: Optional[np.ndarray] = None,
    ) -> SchedulerStats:
        """运行一次衰减评估周期。

        对所有已注册记忆重新评分，根据阈值决定降级/融合/遗忘操作。
        """
        scores = self._scorer.score_all(context_embedding)
        decisions: List[DecayDecision] = []
        forgotten = 0

        with self._lock:
            for memory_id, profile in list(self._profiles.items()):
                new_score = scores.get(memory_id, profile.retention_score)
                signal = self._scorer._signals.get(memory_id)

                # 应用时间衰减
                if signal:
                    days_since_last = (
                        time.time() - profile.updated_at
                    ) / 86400.0
                    decay_rate = self._compute_decay_rate(profile, signal)
                    time_decay = math.exp(-decay_rate * max(days_since_last, 0))
                    new_score = new_score * time_decay

                new_tier = self._score_to_tier(new_score)
                old_tier = profile.tier

                # 决定操作
                action = self._determine_action(profile, new_score, new_tier)

                if action == DecayAction.FORGET:
                    forgotten += 1

                decision = DecayDecision(
                    decision_id=str(uuid.uuid4())[:12],
                    memory_id=memory_id,
                    action=action,
                    previous_score=profile.retention_score,
                    new_score=new_score,
                    previous_tier=old_tier,
                    new_tier=new_tier,
                    reason=f"Decay cycle: score {profile.retention_score:.4f} → {new_score:.4f}",
                )
                decisions.append(decision)

                # 更新剖面
                profile.retention_score = new_score
                profile.tier = new_tier
                profile.updated_at = time.time()

            self._decisions.extend(decisions)
            self._total_forgotten += forgotten
            self._last_cycle = time.time()

            # 构建统计
            stats = self._build_stats()

        logger.info(
            "Decay cycle complete: %d memories, %d decisions, %d forgotten",
            len(self._profiles), len(decisions), forgotten,
        )
        return stats

    def _determine_action(
        self,
        profile: DecayProfile,
        new_score: float,
        new_tier: MemoryTier,
    ) -> DecayAction:
        """根据分数和层级决定操作。"""
        if new_tier.value == profile.tier.value:
            if new_score < profile.retention_score * 0.9:
                return DecayAction.WEAKEN
            return DecayAction.NONE

        # 层级降级
        tier_order = [MemoryTier.HOT, MemoryTier.WARM, MemoryTier.COOL,
                      MemoryTier.COLD, MemoryTier.FROZEN, MemoryTier.ARCHIVED]
        old_idx = tier_order.index(profile.tier) if profile.tier in tier_order else 0
        new_idx = tier_order.index(new_tier) if new_tier in tier_order else 5

        if new_idx > old_idx:
            # 降级
            if new_tier == MemoryTier.FROZEN:
                return DecayAction.FREEZE
            if new_tier == MemoryTier.ARCHIVED:
                return DecayAction.ARCHIVE
            if new_idx - old_idx >= 2:
                return DecayAction.DEGRADE_TIER
            if new_score < 0.15:
                return DecayAction.FORGET
            return DecayAction.DEGRADE_TIER

        return DecayAction.NONE

    def _build_stats(self) -> SchedulerStats:
        """构建统计信息。"""
        counts = {t: 0 for t in MemoryTier}
        total_score = 0.0
        for p in self._profiles.values():
            counts[p.tier] += 1
            total_score += p.retention_score

        n = max(len(self._profiles), 1)
        return SchedulerStats(
            total_memories=len(self._profiles),
            hot_count=counts[MemoryTier.HOT],
            warm_count=counts[MemoryTier.WARM],
            cool_count=counts[MemoryTier.COOL],
            cold_count=counts[MemoryTier.COLD],
            frozen_count=counts[MemoryTier.FROZEN],
            archived_count=counts[MemoryTier.ARCHIVED],
            avg_retention_score=round(total_score / n, 4),
            total_decay_events=len(self._decisions),
            total_reinforcements=self._total_reinforcements,
            total_forgotten=self._total_forgotten,
            last_cycle_time=self._last_cycle,
        )

    def get_profile(self, memory_id: str) -> Optional[DecayProfile]:
        """获取记忆的衰减剖面。"""
        with self._lock:
            return self._profiles.get(memory_id)

    def get_stats(self) -> Dict[str, Any]:
        """获取调度器统计信息。"""
        stats = self._build_stats()
        return dataclasses.asdict(stats)


# ============================================================================
# ThresholdHandler
# ============================================================================

class ThresholdHandler:
    """阈值处理器。

    按分数阈值决定记忆操作：
      - 降级：保留分下降 → 移入更冷层级
      - 融合：多条低频低分记忆合并为一条
      - 遗忘：保留分低于遗忘阈值时彻底移除

    与 AdaptiveDecayScheduler 配合使用。
    """

    def __init__(
        self,
        forget_threshold: float = 0.05,
        merge_threshold: float = 0.2,
        name: str = "threshold_handler",
    ) -> None:
        self._forget_threshold = forget_threshold
        self._merge_threshold = merge_threshold
        self._name = name
        self._lock = threading.RLock()
        self._handled: List[DecayDecision] = []

    def handle(self, decision: DecayDecision) -> bool:
        """执行衰减决策。

        Args:
            decision: 衰减决策

        Returns:
            True 如果操作已执行
        """
        with self._lock:
            self._handled.append(decision)

            if decision.action == DecayAction.FORGET:
                logger.info(
                    "Forgetting memory %s (score %.4f < %.4f)",
                    decision.memory_id, decision.new_score, self._forget_threshold,
                )
                return True

            if decision.new_score < self._merge_threshold:
                logger.info(
                    "Marking memory %s for merge (score %.4f < %.4f)",
                    decision.memory_id, decision.new_score, self._merge_threshold,
                )
                return True

            if decision.action in (DecayAction.DEGRADE_TIER, DecayAction.FREEZE, DecayAction.ARCHIVE):
                logger.info(
                    "Degrading memory %s: %s → %s",
                    decision.memory_id,
                    decision.previous_tier.value, decision.new_tier.value,
                )
                return True

            return False

    def get_stats(self) -> Dict[str, Any]:
        """获取处理器统计信息。"""
        with self._lock:
            return {
                "name": self._name,
                "forget_threshold": self._forget_threshold,
                "merge_threshold": self._merge_threshold,
                "total_handled": len(self._handled),
            }


# ============================================================================
# reinforce() — Module-Level Function
# ============================================================================

def reinforce(
    scheduler: AdaptiveDecayScheduler,
    memory_id: str,
    boost: float = 0.15,
) -> Optional[DecayProfile]:
    """快捷强化函数——在记忆被使用时调用。

    每次记忆被检索/访问/成功使用后调用 reinforce()，
    自动提升保留分并降低衰减率，使活跃记忆更持久。

    Args:
        scheduler: AdaptiveDecayScheduler 实例
        memory_id: 要强化的记忆 ID
        boost: 保留分提升幅度（默认 0.15）

    Returns:
        更新后的衰减剖面，或 None
    """
    return scheduler.reinforce(memory_id, boost)


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """返回模块级统计信息。"""
    return {
        "module": "P13-8 Adaptive Memory Decay",
        "benchmark": "Agent Patterns Catalog",
        "classes": 3,
        "enums": 3,
        "dataclasses": 4,
        "key_metric": "3-signal retention scoring (freq/semantic/recency)",
        "functions": ["reinforce"],
        "thread_safe": True,
        "replaces": "legacy static decay scheduler",
    }
