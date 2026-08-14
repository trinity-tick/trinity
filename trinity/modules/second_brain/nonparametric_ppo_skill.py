"""
P23-2: Skill-Pro — 非参数 PPO 技能学习

对标论文: Skill-Pro (Nonparametric PPO for Procedural Skill Memory, 2026.08)
核心发现: 技能学习应建模为 Skill-MDP（激活/执行/终止条件），
        通过非参数 PPO（语义梯度候选生成 + PPO Gate 验证）学习紧凑的技能策略。
        得分制维护程序性记忆，淘汰低效用技能，保持记忆紧凑。
三元语: Skill-MDP 建模 → 语义梯度候选 → PPO Gate 验证 → 得分制维护 → 紧凑程序性记忆

设计要点:
- SkillMDPState: 技能 MDP 状态枚举（INACTIVE / ACTIVATED / EXECUTING / TERMINATED）
- SkillCondition: 激活/执行/终止条件的数据结构，含前置条件和后置条件
- SkillMDP: 技能马尔可夫决策过程建模，定义状态转移和奖励函数
- SemanticGradientCandidate: 语义梯度驱动的候选技能生成器
- PPOGateVerifier: PPO 门控验证器，评估候选技能的策略价值
- SkillScoreMaintainer: 得分制管理器，维护技能效用分数并淘汰低分技能
- NonparametricPPOLearner: 非参数 PPO 学习器，整合候选生成与门控验证
- SkillProEngine: 统一编排器，线程安全，提供 statistics() 运行时指标
"""

from __future__ import annotations

import logging
import math
import random
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Enums & Constants
# ============================================================================


class SkillMDPState(Enum):
    """技能 MDP 状态"""
    INACTIVE = "inactive"             # 未激活：等待触发条件
    ACTIVATED = "activated"           # 已激活：前置条件满足，待执行
    EXECUTING = "executing"           # 执行中：技能正在运行
    TERMINATED = "terminated"         # 已终止：后置条件满足或异常退出
    SUSPENDED = "suspended"           # 挂起：被更高优先级技能抢占


class ScorePolicy(Enum):
    """得分维护策略"""
    EXPONENTIAL_DECAY = "exponential_decay"    # 指数衰减：分数随时间衰减
    SLIDING_WINDOW = "sliding_window"          # 滑动窗口：仅统计最近 N 次
    BAYESIAN_AVERAGE = "bayesian_average"      # 贝叶斯平均：结合先验
    ELO_RATING = "elo_rating"                  # Elo 评级：两两对比


class CandidateOrigin(Enum):
    """候选技能来源"""
    SEMANTIC_GRADIENT = "semantic_gradient"    # 语义梯度生成
    CROSS_TASK_TRANSFER = "cross_task_transfer"  # 跨任务迁移
    COMPOSITIONAL = "compositional"            # 组合已有技能
    RANDOM_EXPLORATION = "random_exploration"  # 随机探索


class GateVerdict(Enum):
    """PPO Gate 验证裁决"""
    ACCEPT = "accept"                 # 接受：候选技能通过验证
    REJECT = "reject"                 # 拒绝：候选技能未达阈值
    PENDING = "pending"               # 待定：需要更多评估数据
    RETRAIN = "retrain"               # 重训：候选有价值但需调整


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class SkillCondition:
    """技能激活/执行/终止条件"""
    precondition: Dict[str, Any]      # 前置条件：必须满足的键值对
    postcondition: Dict[str, Any]     # 后置条件：执行后应满足的状态
    termination_condition: Dict[str, Any]  # 终止条件：触发终止的守卫
    timeout_seconds: float = 30.0     # 超时时间
    priority: int = 0                 # 优先级（越高越优先）
    max_retries: int = 3              # 最大重试次数


@dataclass
class SkillMDP:
    """技能马尔可夫决策过程"""
    skill_id: str                     # 技能唯一标识
    name: str                         # 技能名称
    conditions: SkillCondition        # 激活/执行/终止条件
    state: SkillMDPState = SkillMDPState.INACTIVE
    transition_log: List[Tuple[SkillMDPState, SkillMDPState, float]] = field(default_factory=list)
    accumulated_reward: float = 0.0
    execution_count: int = 0
    last_executed_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def transition(self, new_state: SkillMDPState, reward: float = 0.0) -> None:
        """执行状态转移并记录奖励"""
        old_state = self.state
        self.transition_log.append((old_state, new_state, reward))
        self.state = new_state
        self.accumulated_reward += reward

    def average_reward(self) -> float:
        """平均每次转移的奖励"""
        if not self.transition_log:
            return 0.0
        return self.accumulated_reward / len(self.transition_log)


@dataclass
class SemanticGradientCandidate:
    """语义梯度驱动的候选技能"""
    skill_mdp: SkillMDP               # 技能 MDP
    origin: CandidateOrigin           # 候选来源
    semantic_distance: float          # 语义距离（到最近邻技能）
    gradient_direction: List[float]   # 语义梯度方向向量
    prior_score: float                # 先验效用分数
    generation_timestamp: float = field(default_factory=time.time)


@dataclass
class PPOGateResult:
    """PPO Gate 验证结果"""
    candidate: SemanticGradientCandidate
    verdict: GateVerdict
    policy_value: float               # PPO 策略价值估计
    advantage_estimate: float         # 优势函数估计
    kl_divergence: float              # KL 散度（与旧策略的距离）
    confidence_interval: Tuple[float, float]  # 置信区间 (lower, upper)
    gate_timestamp: float = field(default_factory=time.time)


@dataclass
class SkillScore:
    """技能效用分数"""
    skill_id: str
    utility: float                    # 当前效用分数
    confidence: float                 # 分数置信度 [0, 1]
    sample_count: int                 # 评估样本数
    last_updated: float               # 最后更新时间戳
    score_history: List[float] = field(default_factory=list)


# ============================================================================
# Core Classes
# ============================================================================


class SkillMDPManager:
    """Skill-MDP 管理器

    维护所有技能 MDP 的生命周期：激活检测、状态转移、奖励累积。
    线程安全，使用 RLock 保护技能注册表。
    """

    def __init__(self) -> None:
        self._skills: OrderedDict[str, SkillMDP] = OrderedDict()
        self._lock = threading.RLock()

    def register(self, skill_mdp: SkillMDP) -> None:
        """注册新技能 MDP"""
        with self._lock:
            self._skills[skill_mdp.skill_id] = skill_mdp
            logger.debug("Skill registered: %s", skill_mdp.skill_id)

    def unregister(self, skill_id: str) -> bool:
        """注销技能"""
        with self._lock:
            return self._skills.pop(skill_id, None) is not None

    def get(self, skill_id: str) -> Optional[SkillMDP]:
        return self._skills.get(skill_id)

    def check_activation(self, skill_id: str, context: Dict[str, Any]) -> bool:
        """检查技能前置条件是否满足"""
        skill = self._skills.get(skill_id)
        if not skill or skill.state != SkillMDPState.INACTIVE:
            return False
        precond = skill.conditions.precondition
        for key, expected_value in precond.items():
            if context.get(key) != expected_value:
                return False
        return True

    def activate(self, skill_id: str) -> bool:
        """激活技能：INACTIVE → ACTIVATED"""
        with self._lock:
            skill = self._skills.get(skill_id)
            if not skill or skill.state != SkillMDPState.INACTIVE:
                return False
            skill.transition(SkillMDPState.ACTIVATED, reward=0.1)
            return True

    def execute(self, skill_id: str, success: bool = True) -> bool:
        """执行技能：ACTIVATED → EXECUTING → TERMINATED"""
        with self._lock:
            skill = self._skills.get(skill_id)
            if not skill or skill.state != SkillMDPState.ACTIVATED:
                return False
            skill.transition(SkillMDPState.EXECUTING, reward=0.0)
            reward = 1.0 if success else -0.5
            skill.transition(SkillMDPState.TERMINATED, reward=reward)
            skill.execution_count += 1
            skill.last_executed_at = time.time()
            return True

    def list_active(self) -> List[SkillMDP]:
        return [s for s in self._skills.values() if s.state != SkillMDPState.INACTIVE]

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_skills": len(self._skills),
            "active_count": len(self.list_active()),
            "total_executions": sum(s.execution_count for s in self._skills.values()),
        }


class SemanticGradientCandidateGenerator:
    """语义梯度候选生成器

    基于已有技能语义空间的梯度方向，生成新的候选技能 MDP。
    通过语义距离控制探索-利用平衡。
    """

    def __init__(self, exploration_rate: float = 0.15,
                 max_candidates_per_round: int = 20) -> None:
        self._exploration_rate = exploration_rate
        self._max_candidates_per_round = max_candidates_per_round
        self._generation_count: int = 0

    def generate(self, existing_skills: List[SkillMDP],
                 context: Dict[str, Any]) -> List[SemanticGradientCandidate]:
        """从现有技能中生成候选技能"""
        self._generation_count += 1
        candidates: List[SemanticGradientCandidate] = []

        if not existing_skills:
            return candidates

        for skill in existing_skills[:self._max_candidates_per_round]:
            # 模拟语义梯度方向：在技能条件空间中做微扰
            gradient = [random.uniform(-self._exploration_rate, self._exploration_rate)
                        for _ in range(min(len(skill.metadata), 8) or 4)]

            # 生成候选技能的条件变体
            new_conditions = SkillCondition(
                precondition=dict(skill.conditions.precondition),
                postcondition=dict(skill.conditions.postcondition),
                termination_condition=dict(skill.conditions.termination_condition),
                timeout_seconds=skill.conditions.timeout_seconds * random.uniform(0.8, 1.2),
                priority=skill.conditions.priority + random.choice([-1, 0, 1]),
            )

            new_mdp = SkillMDP(
                skill_id=f"{skill.skill_id}_var_{self._generation_count}_{len(candidates)}",
                name=f"{skill.name} [variant]",
                conditions=new_conditions,
                metadata={"parent_skill_id": skill.skill_id},
            )

            semantic_distance = sum(abs(g) for g in gradient)
            origin = (CandidateOrigin.RANDOM_EXPLORATION if random.random() < 0.3
                      else CandidateOrigin.SEMANTIC_GRADIENT)

            candidates.append(SemanticGradientCandidate(
                skill_mdp=new_mdp,
                origin=origin,
                semantic_distance=semantic_distance,
                gradient_direction=gradient,
                prior_score=0.5,
            ))

        return candidates

    def statistics(self) -> Dict[str, Any]:
        return {"generation_count": self._generation_count, "exploration_rate": self._exploration_rate}


class PPOGateVerifier:
    """PPO 门控验证器

    对候选技能执行 PPO 风格的门控验证：计算策略价值、优势估计和 KL 散度。
    基于阈值决定接受/拒绝/待定/重训。
    """

    def __init__(self, acceptance_threshold: float = 0.6,
                 kl_penalty_coefficient: float = 0.01,
                 clip_epsilon: float = 0.2) -> None:
        self._acceptance_threshold = acceptance_threshold
        self._kl_penalty_coefficient = kl_penalty_coefficient
        self._clip_epsilon = clip_epsilon
        self._gate_count: int = 0
        self._accept_count: int = 0

    def verify(self, candidate: SemanticGradientCandidate,
               reference_policy_value: float = 0.5) -> PPOGateResult:
        """PPO Gate 验证"""
        self._gate_count += 1

        # 策略价值估计：基于先验分数 + 语义距离惩罚
        policy_value = candidate.prior_score - self._kl_penalty_coefficient * candidate.semantic_distance
        policy_value = max(0.0, min(1.0, policy_value))

        # 优势估计
        advantage = policy_value - reference_policy_value

        # KL 散度（模拟）：语义距离作为与旧策略分布的近似 KL
        kl_div = candidate.semantic_distance * 0.1

        # 置信区间
        margin = 0.05 + kl_div * 0.1
        ci_lower = max(0.0, policy_value - margin)
        ci_upper = min(1.0, policy_value + margin)

        # 裁决
        if policy_value >= self._acceptance_threshold and kl_div < 0.5:
            verdict = GateVerdict.ACCEPT
            self._accept_count += 1
        elif policy_value >= self._acceptance_threshold * 0.7:
            verdict = GateVerdict.PENDING
        elif policy_value >= 0.3:
            verdict = GateVerdict.RETRAIN
        else:
            verdict = GateVerdict.REJECT

        return PPOGateResult(
            candidate=candidate,
            verdict=verdict,
            policy_value=policy_value,
            advantage_estimate=advantage,
            kl_divergence=kl_div,
            confidence_interval=(ci_lower, ci_upper),
        )

    def statistics(self) -> Dict[str, Any]:
        return {
            "gate_count": self._gate_count,
            "accept_count": self._accept_count,
            "accept_rate": self._accept_count / max(self._gate_count, 1),
        }


class SkillScoreMaintainer:
    """得分制技能维护器

    维护每个技能的效用分数，支持多种衰减策略。
    低分技能自动标记为淘汰候选，保持程序性记忆紧凑。
    """

    def __init__(self, score_policy: ScorePolicy = ScorePolicy.EXPONENTIAL_DECAY,
                 decay_rate: float = 0.001,
                 pruning_threshold: float = 0.15,
                 min_samples: int = 5) -> None:
        self._score_policy = score_policy
        self._decay_rate = decay_rate
        self._pruning_threshold = pruning_threshold
        self._min_samples = min_samples
        self._scores: Dict[str, SkillScore] = {}
        self._lock = threading.RLock()

    def initialize_score(self, skill_id: str, initial_utility: float = 0.5) -> SkillScore:
        with self._lock:
            score = SkillScore(
                skill_id=skill_id,
                utility=initial_utility,
                confidence=0.1,
                sample_count=0,
                last_updated=time.time(),
            )
            self._scores[skill_id] = score
            return score

    def update(self, skill_id: str, reward: float) -> Optional[SkillScore]:
        """根据执行奖励更新技能分数"""
        with self._lock:
            score = self._scores.get(skill_id)
            if not score:
                return None

            score.sample_count += 1
            score.score_history.append(reward)

            if self._score_policy == ScorePolicy.EXPONENTIAL_DECAY:
                alpha = 0.1
                score.utility = score.utility * (1 - alpha) + reward * alpha
                elapsed = time.time() - score.last_updated
                score.utility *= math.exp(-self._decay_rate * elapsed)
            elif self._score_policy == ScorePolicy.SLIDING_WINDOW:
                window = score.score_history[-20:]
                score.utility = sum(window) / len(window) if window else score.utility
            elif self._score_policy == ScorePolicy.BAYESIAN_AVERAGE:
                prior = 0.5
                prior_weight = 5
                score.utility = (prior * prior_weight + sum(score.score_history)) / (prior_weight + score.sample_count)

            score.confidence = min(1.0, score.sample_count / self._min_samples)
            score.last_updated = time.time()
            return score

    def get_pruning_candidates(self) -> List[str]:
        """获取应淘汰的低分技能 ID 列表"""
        with self._lock:
            candidates = []
            for skill_id, score in self._scores.items():
                if score.sample_count >= self._min_samples and score.utility < self._pruning_threshold:
                    candidates.append(skill_id)
            return candidates

    def prune(self, skill_ids: List[str]) -> int:
        """淘汰低分技能"""
        with self._lock:
            count = 0
            for sid in skill_ids:
                if sid in self._scores:
                    del self._scores[sid]
                    count += 1
            logger.info("Pruned %d low-utility skills", count)
            return count

    def statistics(self) -> Dict[str, Any]:
        return {
            "tracked_skills": len(self._scores),
            "average_utility": sum(s.utility for s in self._scores.values()) / max(len(self._scores), 1),
            "pruning_candidates": len(self.get_pruning_candidates()),
        }


# ============================================================================
# Engine
# ============================================================================


class NonparametricPPOLearner:
    """非参数 PPO 学习器

    整合语义梯度候选生成 + PPO Gate 验证的学习循环。
    """

    def __init__(self, exploration_rate: float = 0.15,
                 acceptance_threshold: float = 0.6) -> None:
        self._candidate_generator = SemanticGradientCandidateGenerator(
            exploration_rate=exploration_rate)
        self._gate_verifier = PPOGateVerifier(
            acceptance_threshold=acceptance_threshold)
        self._iteration_count: int = 0
        self._accepted_candidates: List[PPOGateResult] = []

    def learn_iteration(self, existing_skills: List[SkillMDP],
                        context: Dict[str, Any],
                        reference_policy_value: float = 0.5) -> List[PPOGateResult]:
        """执行一次学习迭代：生成候选 → PPO Gate 验证"""
        self._iteration_count += 1

        candidates = self._candidate_generator.generate(existing_skills, context)
        results: List[PPOGateResult] = []

        for candidate in candidates:
            result = self._gate_verifier.verify(candidate, reference_policy_value)
            results.append(result)
            if result.verdict == GateVerdict.ACCEPT:
                self._accepted_candidates.append(result)
                logger.info("PPO Gate accepted skill: %s (value=%.3f)",
                            candidate.skill_mdp.skill_id, result.policy_value)

        return results

    def statistics(self) -> Dict[str, Any]:
        return {
            "iteration_count": self._iteration_count,
            "accepted_count": len(self._accepted_candidates),
            "generator": self._candidate_generator.statistics(),
            "gate": self._gate_verifier.statistics(),
        }


class SkillProEngine:
    """Skill-Pro 统一编排器

    整合 Skill-MDP 管理 → 语义梯度候选 → PPO Gate 验证 → 得分制维护
    的完整技能学习流水线。线程安全。
    """

    def __init__(self, exploration_rate: float = 0.15,
                 acceptance_threshold: float = 0.6,
                 score_policy: ScorePolicy = ScorePolicy.EXPONENTIAL_DECAY,
                 pruning_threshold: float = 0.15) -> None:
        self._lock = threading.RLock()
        self._mdp_manager = SkillMDPManager()
        self._learner = NonparametricPPOLearner(
            exploration_rate=exploration_rate,
            acceptance_threshold=acceptance_threshold,
        )
        self._score_maintainer = SkillScoreMaintainer(
            score_policy=score_policy,
            pruning_threshold=pruning_threshold,
        )

    def register_skill(self, skill_id: str, name: str,
                       conditions: SkillCondition) -> SkillMDP:
        """注册新技能"""
        mdp = SkillMDP(skill_id=skill_id, name=name, conditions=conditions)
        with self._lock:
            self._mdp_manager.register(mdp)
            self._score_maintainer.initialize_score(skill_id)
        return mdp

    def execute_skill(self, skill_id: str, context: Dict[str, Any],
                      success: bool = True) -> bool:
        """执行技能并更新分数"""
        if not self._mdp_manager.check_activation(skill_id, context):
            return False
        if not self._mdp_manager.activate(skill_id):
            return False
        result = self._mdp_manager.execute(skill_id, success)
        if result:
            reward = 1.0 if success else -0.5
            self._score_maintainer.update(skill_id, reward)
        return result

    def learn_skills(self, context: Dict[str, Any]) -> List[PPOGateResult]:
        """学习新技能"""
        with self._lock:
            existing = list(self._mdp_manager._skills.values())
            return self._learner.learn_iteration(existing, context)

    def prune_low_utility_skills(self) -> int:
        """淘汰低效用技能"""
        candidates = self._score_maintainer.get_pruning_candidates()
        count = self._score_maintainer.prune(candidates)
        for sid in candidates:
            self._mdp_manager.unregister(sid)
        return count

    def statistics(self) -> Dict[str, Any]:
        """聚合运行时统计"""
        return {
            "mdp_manager": self._mdp_manager.statistics(),
            "learner": self._learner.statistics(),
            "score_maintainer": self._score_maintainer.statistics(),
        }


# ============================================================================
# Module-level statistics helper
# ============================================================================

def statistics(engine: Optional[SkillProEngine] = None) -> Dict[str, Any]:
    """模块级统计接口"""
    if engine is not None:
        return engine.statistics()
    return {"status": "no engine initialized"}
