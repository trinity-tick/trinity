"""
ExploratoryMemoryOptimizer — EMPO2 Self-Generated Tips + Dual-Mode Rollout
===========================================================================
ICLR 2026 · P41-3

实现 EMPO2 探索记忆优化器: self_generated_tips 每 episode 反思生成经验 tips 回填,
dual_mode_rollout 记忆增强/无记忆模式间按概率采样, hybrid_off_policy_update 将
记忆增强轨迹的高质量行为蒸馏进无记忆策略, intrinsic_reward 按状态新颖度补发内在奖励。

设计要点:
  - SelfGeneratedTips: LLM 反思轨迹→经验 tips
  - DualModeRollout: ε-采样双模式
  - HybridOffPolicyUpdate: 记忆增强→无记忆策略蒸馏
  - IntrinsicReward: 状态新颖度计数驱动探索
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RolloutMode(Enum):
    """Rollout 模式。"""
    MEMORY_AUGMENTED = auto()
    MEMORY_FREE = auto()


class TipCategory(Enum):
    """Tips 类别。"""
    STRATEGY = auto()
    CAUTION = auto()
    HEURISTIC = auto()
    EXPLORATION = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ExplorationBonus:
    """探索奖励——状态新颖度。"""
    state_hash: str
    visit_count: int = 0
    intrinsic_reward: float = 0.0
    novelty_decay: float = 0.99  # 每访问一次衰减
    timestamp: float = field(default_factory=time.time)


@dataclass
class TipEntry:
    """一条经验 tip——从反思中生成的经验条目。"""
    tip_id: str
    category: TipCategory
    content: str
    episode_id: str
    confidence: float = 0.0
    reuse_count: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RolloutTrajectory:
    """一次 rollout 的轨迹。"""
    trajectory_id: str
    mode: RolloutMode
    steps: List[Dict[str, Any]]  # [{"state":..., "action":..., "reward":...}, ...]
    total_reward: float = 0.0
    tip_ids: List[str] = field(default_factory=list)  # 使用的 tip
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# SelfGeneratedTips
# ---------------------------------------------------------------------------

class SelfGeneratedTips:
    """每 episode 后反思轨迹生成经验 tips 回填缓冲区。

    Parameters
    ----------
    capacity : int
        Tips 容量。
    """

    def __init__(self, capacity: int = 100) -> None:
        self.capacity = capacity
        self._tips: Dict[str, TipEntry] = {}
        self._lock = threading.RLock()
        self._tip_count: int = 0

    def generate_tips(
        self,
        trajectory: List[Dict[str, Any]],
        episode_id: str,
        insights: Optional[List[str]] = None,
    ) -> List[TipEntry]:
        """反思轨迹生成 tips。

        Parameters
        ----------
        trajectory : List[Dict[str, Any]]
            Episode 轨迹。
        episode_id : str
            Episode 标识。
        insights : Optional[List[str]]
            外部提供的反思洞察 (如 LLM 输出)。

        Returns
        -------
        List[TipEntry]
            生成的 tips。
        """
        with self._lock:
            generated: List[TipEntry] = []
            if not insights:
                insights = _default_reflection(trajectory)

            for i, insight in enumerate(insights):
                self._tip_count += 1
                tip = TipEntry(
                    tip_id=f"tip_{self._tip_count}_{int(time.time()*1e6)}",
                    category=_classify_tip(insight),
                    content=insight,
                    episode_id=episode_id,
                    confidence=0.7,
                )
                self._tips[tip.tip_id] = tip
                generated.append(tip)

            # 容量控制
            while len(self._tips) > self.capacity:
                oldest = min(self._tips.items(), key=lambda x: x[1].reuse_count)
                del self._tips[oldest[0]]

            return generated

    def retrieve_tips(self, k: int = 5) -> List[TipEntry]:
        """检索最可信的 tips。"""
        return sorted(self._tips.values(), key=lambda t: (t.confidence, t.reuse_count), reverse=True)[:k]

    def statistics(self) -> Dict[str, Any]:
        return {"total_tips": len(self._tips)}


# ---------------------------------------------------------------------------
# DualModeRollout
# ---------------------------------------------------------------------------

class DualModeRollout:
    """按概率在记忆增强/无记忆模式间采样。

    Parameters
    ----------
    memory_augmented_prob : float
        选择记忆增强模式的概率 (0~1)。
    """

    def __init__(self, memory_augmented_prob: float = 0.7) -> None:
        self.memory_augmented_prob = memory_augmented_prob
        self._history: deque = deque(maxlen=50)
        self._lock = threading.RLock()

    def sample_mode(self) -> RolloutMode:
        """按概率采样 rollout 模式。"""
        if np.random.random() < self.memory_augmented_prob:
            return RolloutMode.MEMORY_AUGMENTED
        return RolloutMode.MEMORY_FREE

    def record_trajectory(self, traj: RolloutTrajectory) -> None:
        with self._lock:
            self._history.append(traj)

    def mode_statistics(self) -> Dict[str, int]:
        count = {m.name: 0 for m in RolloutMode}
        for t in self._history:
            count[t.mode.name] = count.get(t.mode.name, 0) + 1
        return count


# ---------------------------------------------------------------------------
# HybridOffPolicyUpdate
# ---------------------------------------------------------------------------

class HybridOffPolicyUpdate:
    """将记忆增强轨迹的高质量行为蒸馏进无记忆策略。

    Parameters
    ----------
    quality_threshold : float
        高质量轨迹阈值 (reward 高于此值才蒸馏)。
    distillation_rate : float
        蒸馏更新率。
    """

    def __init__(self, quality_threshold: float = 0.6, distillation_rate: float = 0.01) -> None:
        self.quality_threshold = quality_threshold
        self.distillation_rate = distillation_rate
        self._policy_cache: Dict[str, float] = {}
        self._lock = threading.RLock()
        self._distill_count: int = 0

    def distill(
        self,
        augmented_trajectories: List[RolloutTrajectory],
        policy_weights: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """从记忆增强轨迹蒸馏高质量行为。

        Parameters
        ----------
        augmented_trajectories : List[RolloutTrajectory]
            记忆增强模式的轨迹。
        policy_weights : Optional[Dict[str, Any]]
            当前无记忆策略权重。

        Returns
        -------
        Dict[str, Any]
            蒸馏统计。
        """
        with self._lock:
            # 筛选高质量轨迹
            high_quality = [t for t in augmented_trajectories
                            if t.mode == RolloutMode.MEMORY_AUGMENTED
                            and t.total_reward >= self.quality_threshold]

            distilled_actions: Dict[str, float] = {}
            for traj in high_quality:
                for step in traj.steps:
                    action = step.get("action", "")
                    reward = step.get("reward", 0.0)
                    if action:
                        prev = distilled_actions.get(action, 0.0)
                        distilled_actions[action] = prev + reward * self.distillation_rate

            # 合并到策略缓存
            for action, delta in distilled_actions.items():
                self._policy_cache[action] = self._policy_cache.get(action, 0.5) + delta
                self._policy_cache[action] = np.clip(self._policy_cache[action], 0.0, 1.0)

            self._distill_count += len(high_quality)

            return {
                "distilled_from": len(high_quality),
                "total_distilled": self._distill_count,
                "updated_actions": len(distilled_actions),
            }

    def get_policy_bias(self, action: str) -> float:
        """获取蒸馏后的策略偏差。"""
        return self._policy_cache.get(action, 0.0)


# ---------------------------------------------------------------------------
# IntrinsicReward
# ---------------------------------------------------------------------------

class IntrinsicReward:
    """按状态新颖度补发内在奖励驱动探索。

    Parameters
    ----------
    novelty_scale : float
        新颖度奖励缩放。
    decay_rate : float
        全局访问计数衰减率。
    """

    def __init__(self, novelty_scale: float = 0.1, decay_rate: float = 0.99) -> None:
        self.novelty_scale = novelty_scale
        self.decay_rate = decay_rate
        self._visit_counts: Dict[str, ExplorationBonus] = {}
        self._lock = threading.RLock()

    def compute_intrinsic_reward(self, state: Dict[str, Any]) -> float:
        """计算内在奖励——新颖度越高的状态奖励越大。

        Returns
        -------
        float
            内在奖励值。
        """
        state_hash = _hash_state(state)
        with self._lock:
            if state_hash not in self._visit_counts:
                bonus = ExplorationBonus(
                    state_hash=state_hash,
                    visit_count=1,
                    intrinsic_reward=self.novelty_scale,
                )
                self._visit_counts[state_hash] = bonus
                return self.novelty_scale

            bonus = self._visit_counts[state_hash]
            bonus.visit_count += 1
            bonus.intrinsic_reward = self.novelty_scale / np.sqrt(bonus.visit_count)
            return bonus.intrinsic_reward


# ---------------------------------------------------------------------------
# ExploratoryMemoryOptimizer
# ---------------------------------------------------------------------------

class ExploratoryMemoryOptimizer:
    """EMPO2 探索记忆优化器。

    Parameters
    ----------
    tip_capacity : int
        SelfGeneratedTips 容量。
    memory_prob : float
        记忆增强模式概率。
    quality_threshold : float
        高质量轨迹阈值。
    novelty_scale : float
        新颖度奖励缩放。
    """

    def __init__(
        self,
        tip_capacity: int = 100,
        memory_prob: float = 0.7,
        quality_threshold: float = 0.6,
        novelty_scale: float = 0.1,
    ) -> None:
        self.self_generated_tips = SelfGeneratedTips(capacity=tip_capacity)
        self.dual_mode_rollout = DualModeRollout(memory_augmented_prob=memory_prob)
        self.hybrid_off_policy_update = HybridOffPolicyUpdate(quality_threshold=quality_threshold)
        self.intrinsic_reward = IntrinsicReward(novelty_scale=novelty_scale)
        self._episode_count: int = 0
        self._lock = threading.RLock()

        logger.info(
            "ExploratoryMemoryOptimizer initialized [tips=%d mem_prob=%.2f q=%.2f nov=%.2f]",
            tip_capacity, memory_prob, quality_threshold, novelty_scale,
        )

    # ------------------------------------------------------------------
    # Self-Generated Tips
    # ------------------------------------------------------------------

    def generate_tips_from_episode(
        self, trajectory: List[Dict[str, Any]], insights: Optional[List[str]] = None
    ) -> List[TipEntry]:
        """每 episode 后反思生成经验 tips。"""
        self._episode_count += 1
        return self.self_generated_tips.generate_tips(
            trajectory=trajectory,
            episode_id=f"ep_{self._episode_count}",
            insights=insights,
        )

    # ------------------------------------------------------------------
    # Dual-Mode Rollout
    # ------------------------------------------------------------------

    def sample_rollout_mode(self) -> RolloutMode:
        """采样 rollout 模式。"""
        return self.dual_mode_rollout.sample_mode()

    def record_trajectory(self, traj: RolloutTrajectory) -> None:
        """记录轨迹到双模式历史。"""
        self.dual_mode_rollout.record_trajectory(traj)

    # ------------------------------------------------------------------
    # Hybrid Off-Policy Update
    # ------------------------------------------------------------------

    def distill_to_policy(
        self, augmented_trajectories: List[RolloutTrajectory]
    ) -> Dict[str, Any]:
        """将记忆增强轨迹的行为蒸馏进无记忆策略。"""
        return self.hybrid_off_policy_update.distill(augmented_trajectories)

    # ------------------------------------------------------------------
    # Intrinsic Reward
    # ------------------------------------------------------------------

    def compute_intrinsic_reward(self, state: Dict[str, Any]) -> float:
        """按状态新颖度补发内在奖励。"""
        return self.intrinsic_reward.compute_intrinsic_reward(state)

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "episodes": self._episode_count,
                "tips": self.self_generated_tips.statistics()["total_tips"],
                "rollout_modes": self.dual_mode_rollout.mode_statistics(),
                "distilled": self.hybrid_off_policy_update._distill_count,
                "visited_states": len(self.intrinsic_reward._visit_counts),
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_state(state: Dict[str, Any]) -> str:
    import hashlib, json
    s = json.dumps(state, sort_keys=True, default=str)
    return hashlib.md5(s.encode()).hexdigest()


def _default_reflection(trajectory: List[Dict[str, Any]]) -> List[str]:
    """默认反思——从轨迹中提取模式。"""
    insights = []
    if not trajectory:
        return insights

    rewards = [s.get("reward", 0.0) for s in trajectory]
    if any(r > 0 for r in rewards):
        best_idx = np.argmax(rewards)
        best_action = trajectory[best_idx].get("action", "unknown")
        insights.append(f"High-reward action '{best_action}' led to reward {rewards[best_idx]:.2f}")

    if rewards and min(rewards) < 0:
        worst_idx = np.argmin(rewards)
        worst_action = trajectory[worst_idx].get("action", "unknown")
        insights.append(f"Low-reward action '{worst_action}' should be avoided (r={rewards[worst_idx]:.2f})")

    return insights


def _classify_tip(insight: str) -> TipCategory:
    low = insight.lower()
    if "avoid" in low or "caution" in low or "don't" in low:
        return TipCategory.CAUTION
    if "explore" in low or "novel" in low or "try" in low:
        return TipCategory.EXPLORATION
    if "should" in low or "rule" in low or "if" in low:
        return TipCategory.HEURISTIC
    return TipCategory.STRATEGY
