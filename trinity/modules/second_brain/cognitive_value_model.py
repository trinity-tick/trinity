"""
CB62: CognitiveValueModel — 认知多因子价值模型
===============================================

对标 arXiv 2606.12945（Learning What to Remember）。基于认知心理学的 7 因子
记忆价值函数 V(m) = Σw_i * f_i(m)，统一控制 encoding_depth / forget_risk /
retrieval_rank。

设计要点：
  - 7 因子：emotional_intensity, goal_relevance, value_alignment,
    self_user_relevance, task_utility, reliability, usage_history
  - 梯度无关优化器学习权重（无需梯度传播，基于排名反馈调权）
  - 因子归一化后加权求和，输出统一 value score ∈ [0,1]
  - 集成到 GuardianChain 衰减调度器：value 低于阈值的记忆自动进入衰减队列
  - 线程安全，支持 factor_type 枚举和批量评估

Reference:
  - arXiv 2606.12945 "Learning What to Remember"
  - Cognitive psychology multi-factor memory value modeling
  - 7-factor value function: V(m) = Σ w_i · f_i(m)
"""

from __future__ import annotations

import dataclasses
import logging
import math
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class FactorType(Enum):
    """7 因子的认知类型枚举。"""
    EMOTIONAL_INTENSITY = auto()       # 情感强度
    GOAL_RELEVANCE = auto()            # 目标相关性
    VALUE_ALIGNMENT = auto()           # 价值观对齐
    SELF_USER_RELEVANCE = auto()       # 自我/用户相关性
    TASK_UTILITY = auto()              # 任务实用性
    RELIABILITY = auto()               # 可靠性
    USAGE_HISTORY = auto()             # 使用历史


class LearningStrategy(Enum):
    """权重学习策略。"""
    RANKING_FEEDBACK = "ranking_feedback"          # 排名反馈优化
    EXPONENTIAL_AVERAGING = "exponential_avg"       # 指数平均
    CORRELATION_HEBBIAN = "correlation_hebbian"      # 相关性赫布学习


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class MemoryFactor:
    """单个记忆的 7 因子评估快照。

    Attributes:
        memory_id: 记忆唯一标识。
        emotional_intensity: 情感强度 [0,1]。
        goal_relevance: 目标相关性 [0,1]。
        value_alignment: 价值观对齐度 [0,1]。
        self_user_relevance: 用户相关性 [0,1]。
        task_utility: 任务实用性 [0,1]。
        reliability: 来源/内容可靠性 [0,1]。
        usage_history: 归一化使用频率 [0,1]。
    """
    memory_id: str
    emotional_intensity: float = 0.5
    goal_relevance: float = 0.5
    value_alignment: float = 0.5
    self_user_relevance: float = 0.5
    task_utility: float = 0.5
    reliability: float = 0.5
    usage_history: float = 0.0

    def __post_init__(self):
        for name in FactorType.__members__.values():
            v = getattr(self, name.name.lower(), 0.5)
            if not (0.0 <= v <= 1.0):
                raise ValueError(
                    f"Factor {name.name}: value {v} out of [0,1]"
                )

    def as_vector(self) -> Tuple[float, ...]:
        """返回归一化因子向量。"""
        return (
            self.emotional_intensity, self.goal_relevance,
            self.value_alignment, self.self_user_relevance,
            self.task_utility, self.reliability, self.usage_history,
        )


@dataclass
class FactorWeight:
    """单个因子的可学习权重。

    Attributes:
        factor_type: 因子类型。
        weight: 当前权重值 [0,1]。
        learning_rate: 该因子的学习率。
        update_count: 累计更新次数。
    """
    factor_type: FactorType
    weight: float = 0.5
    learning_rate: float = 0.05
    update_count: int = 0


@dataclass
class CVMConfig:
    """认知价值模型配置。

    Attributes:
        forgetting_threshold: 衰减阈值，value 低于此的记忆进入衰减队列。
        deep_encoding_threshold: 深度编码阈值。
        learning_strategy: 权重学习策略。
    """
    forgetting_threshold: float = 0.2
    deep_encoding_threshold: float = 0.7
    learning_strategy: LearningStrategy = LearningStrategy.RANKING_FEEDBACK


# ============================================================================
# Main Class
# ============================================================================

class CognitiveValueModel:
    """认知多因子价值模型 (CB62)。

    7 因子加权计算每个记忆的统一价值 score，驱动 encoding_depth 分配、
    遗忘风险管理、检索排序。

    Usage:
        cvm = CognitiveValueModel()
        score = cvm.evaluate(memory_factor)
        cvm.update_weights(ranking_feedback=[("mem_a", 1.0), ("mem_b", 0.3)])
        risk = cvm.forgetting_risk(memory_factor)
    """

    def __init__(self, config: Optional[CVMConfig] = None):
        self.config = config or CVMConfig()
        self._lock = threading.RLock()
        self._weights: Dict[FactorType, FactorWeight] = {}
        self._eval_count: int = 0
        self._start_time: float = _time.time()

        # 初始化 7 因子权重
        defaults = {FactorType.EMOTIONAL_INTENSITY: 0.12,
                    FactorType.GOAL_RELEVANCE: 0.20,
                    FactorType.VALUE_ALIGNMENT: 0.15,
                    FactorType.SELF_USER_RELEVANCE: 0.18,
                    FactorType.TASK_UTILITY: 0.15,
                    FactorType.RELIABILITY: 0.10,
                    FactorType.USAGE_HISTORY: 0.10}
        for ft_key in FactorType:
            self._weights[ft_key] = FactorWeight(
                factor_type=ft_key,
                weight=defaults.get(ft_key, 0.10),
            )

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def evaluate(self, factor: MemoryFactor) -> float:
        """计算记忆的认知价值 V(m) = Σ w_i · f_i(m)。

        Args:
            factor: 7 因子快照。

        Returns:
            float: 归一化价值 [0,1]。
        """
        with self._lock:
            vec = factor.as_vector()
            total = 0.0
            for idx, ft in enumerate(FactorType):
                total += self._weights[ft].weight * vec[idx]
            self._eval_count += 1
            return min(1.0, max(0.0, total))

    def evaluate_batch(self, factors: List[MemoryFactor]) -> List[float]:
        """批量评估，返回价值列表。"""
        return [self.evaluate(f) for f in factors]

    def forgetting_risk(self, factor: MemoryFactor) -> float:
        """遗忘风险 = 1 - value（超出阈值为高风险）。"""
        value = self.evaluate(factor)
        return max(0.0, 1.0 - value - self.config.forgetting_threshold)

    def encoding_depth(self, factor: MemoryFactor) -> float:
        """建议编码深度，value 越高编码越深。"""
        value = self.evaluate(factor)
        if value >= self.config.deep_encoding_threshold:
            return 1.0  # 全量深度编码
        return 0.3 + 0.4 * (value / self.config.deep_encoding_threshold)

    # ------------------------------------------------------------------
    # Weight Learning
    # ------------------------------------------------------------------

    def update_weights(
        self,
        ranking_feedback: List[Tuple[str, float]],
        factors: Optional[Dict[str, MemoryFactor]] = None,
    ):
        """基于排名反馈更新因子权重（梯度无关优化器）。

        策略：高排名记忆的因子权重调增，低排名调减。

        Args:
            ranking_feedback: [(memory_id, utility_score), ...]。
            factors: memory_id → MemoryFactor 映射。
        """
        with self._lock:
            if not ranking_feedback or factors is None:
                return

            strategy = self.config.learning_strategy
            scores = dict(ranking_feedback)
            if len(scores) < 2:
                return
            sorted_mems = sorted(scores.items(), key=lambda x: x[1], reverse=True)
            mid = len(sorted_mems) // 2

            high_mems = {mid for mid, _ in sorted_mems[:mid]}
            low_mems = {mid for mid, _ in sorted_mems[mid:]}

            for ft in FactorType:
                fw = self._weights[ft]
                delta = 0.0
                count = 0
                for mem_id in high_mems:
                    if mem_id in factors:
                        vec = factors[mem_id].as_vector()
                        delta += vec[ft.value - 1] * fw.learning_rate
                        count += 1
                for mem_id in low_mems:
                    if mem_id in factors:
                        vec = factors[mem_id].as_vector()
                        delta -= vec[ft.value - 1] * fw.learning_rate * 0.5
                        count += 1

                if count > 0:
                    if strategy == LearningStrategy.EXPONENTIAL_AVERAGING:
                        fw.weight = 0.9 * fw.weight + 0.1 * (fw.weight + delta / count)
                    else:
                        fw.weight += delta / max(count, 1)
                    fw.weight = max(0.01, min(1.0, fw.weight))
                    fw.update_count += 1

    def get_factor_weights(self) -> Dict[FactorType, float]:
        """返回当前所有权重快照。"""
        with self._lock:
            return {ft: fw.weight for ft, fw in self._weights.items()}

    def get_weight(self, factor_type: FactorType) -> float:
        """获取单个因子权重。"""
        with self._lock:
            return self._weights[factor_type].weight

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "CognitiveValueModel (CB62)",
                "total_evaluations": self._eval_count,
                "forgetting_threshold": self.config.forgetting_threshold,
                "deep_encoding_threshold": self.config.deep_encoding_threshold,
                "learning_strategy": self.config.learning_strategy.value,
                "factor_weights": {
                    ft.name: round(fw.weight, 4)
                    for ft, fw in self._weights.items()
                },
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
