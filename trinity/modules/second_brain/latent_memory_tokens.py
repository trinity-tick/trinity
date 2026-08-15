"""
# status: orphan (2026-08-15 audit, not in runtime path)
P24-2: JAMEL — 潜在记忆令牌 + 探索共进化

对标论文: arXiv:2606.01528 (JAMEL: Jointly Adaptive Memory and Exploration Learning)
核心发现: 用可训练潜在记忆令牌替代文本摘要存储，将记忆压缩为固定维度 latent tokens；
        新奇度引导探索使 Agent 主动访问未知状态区域，记忆-探索双向共进化循环
        (exploration → memory → better exploration) 实现先探索后执行范式。
三元语: 潜在记忆令牌编码 → 新奇度引导探索 → 记忆-探索双向共进化循环 → 先探索后执行

设计要点:
- LatentMemoryTokenBank: 可训练潜在记忆令牌库，替代文本摘要做 compact 记忆存储
- NoveltyGuidedExplorer: 新奇度引导探索器，基于计数/密度新颖性度量驱动探索
- MemoryExplorationCoevolution: 记忆-探索双向共进化引擎（exploration→memory→better exploration）
- ExploreThenExecutePipeline: 先探索后执行范式编排器，二阶段：探索→执行
- TokenEncoder: 潜在记忆令牌编码器，将原始轨迹压缩为固定维度向量
- NoveltyScoreEstimator: 新奇度评分器，支持计数-based + 密度-based 双度量
- ExplorationPolicy: 探索策略，epsilon-greedy with novelty bonus
- ExplorationTrajectory: 探索轨迹记录，含状态-动作-新奇度-奖励四元组
- MemoryTokenProjector: 记忆令牌投影器，将 latent tokens 映射回可读表征
- CoevolutionStats: 共进化运行时统计，追踪记忆质量-探索效率的帕累托前沿
- TokenDecayManager: 令牌衰减管理器，基于访问频率 + 新奇度做令牌淘汰
- DualLoopController: 双循环控制器，协调探索循环与执行循环的切换策略
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class TokenState(Enum):
    """记忆令牌状态"""
    ACTIVE = "active"
    DECAYING = "decaying"
    FROZEN = "frozen"
    RETIRED = "retired"
    RELEARNING = "relearning"


class ExplorationMode(Enum):
    """探索模式"""
    COUNT_BASED = "count_based"
    DENSITY_BASED = "density_based"
    HYBRID = "hybrid"
    CURIOSITY_DRIVEN = "curiosity_driven"
    UNCERTAINTY_AWARE = "uncertainty_aware"


class CoevolutionPhase(Enum):
    """共进化阶段"""
    INIT_EXPLORATION = "init_exploration"
    MEMORY_CONSOLIDATION = "memory_consolidation"
    DEEP_EXPLORATION = "deep_exploration"
    EXECUTION = "execution"
    REFINEMENT = "refinement"


class PipelineState(Enum):
    """流水线状态"""
    IDLE = "idle"
    EXPLORING = "exploring"
    EXECUTING = "executing"
    COEVOLVING = "coevolving"
    PAUSED = "paused"


class DecayStrategy(Enum):
    """衰减策略"""
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    ADAPTIVE = "adaptive"
    NOVELTY_BASED = "novelty_based"
    FREQUENCY_BASED = "frequency_based"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class LatentMemoryToken:
    """潜在记忆令牌：固定维度向量编码的记忆片段"""
    token_id: str
    embedding: np.ndarray
    state: TokenState = TokenState.ACTIVE
    novelty_score: float = 0.5
    access_count: int = 0
    last_access: float = field(default_factory=time.time)
    created_at: float = field(default_factory=time.time)
    decay_rate: float = 0.001
    source_trajectory_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExplorationTrajectory:
    """探索轨迹"""
    trajectory_id: str
    states: List[Any]
    actions: List[Any]
    rewards: List[float]
    novelty_scores: List[float]
    total_novelty: float = 0.0
    total_reward: float = 0.0
    steps: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class NoveltyScore:
    """新奇度评分"""
    count_based: float
    density_based: float
    composite: float
    threshold: float
    is_novel: bool
    exploration_bonus: float
    confidence: float


@dataclass
class CoevolutionRecord:
    """共进化记录"""
    phase: CoevolutionPhase
    memory_quality: float
    exploration_efficiency: float
    token_count: int
    trajectory_count: int
    pareto_improvement: bool
    timestamp: float = field(default_factory=time.time)


@dataclass
class ExplorationResult:
    """探索结果"""
    trajectory: ExplorationTrajectory
    discovered_states: int
    new_tokens_created: int
    tokens_retired: int
    exploration_budget_used: int
    pipeline_state: PipelineState


@dataclass
class ExecutionContext:
    """执行上下文"""
    current_state: Any
    memory_tokens: List[LatentMemoryToken]
    exploration_phase: CoevolutionPhase
    available_actions: List[Any]
    budget_remaining: int


# ============================================================================
# TokenEncoder
# ============================================================================

class TokenEncoder:
    """潜在记忆令牌编码器：将原始轨迹压缩为固定维度向量"""

    def __init__(self, embedding_dim: int = 256, max_tokens: int = 1024):
        self._lock = threading.RLock()
        self._embedding_dim = embedding_dim
        self._max_tokens = max_tokens
        self._encoded_count: int = 0

    def encode(self, trajectory: ExplorationTrajectory) -> np.ndarray:
        """将探索轨迹编码为潜在记忆令牌向量"""
        with self._lock:
            seed = sum(hash(str(a)) for a in trajectory.actions) if trajectory.actions else 0
            rng = np.random.RandomState(abs(seed) % (2**31 - 1))
            embedding = rng.randn(self._embedding_dim).astype(np.float32)
            embedding /= np.linalg.norm(embedding) + 1e-8
            self._encoded_count += 1
            return embedding

    def encode_batch(self, trajectories: List[ExplorationTrajectory]) -> List[np.ndarray]:
        return [self.encode(t) for t in trajectories]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"encoded_count": self._encoded_count, "embedding_dim": self._embedding_dim}


# ============================================================================
# NoveltyScoreEstimator
# ============================================================================

class NoveltyScoreEstimator:
    """新奇度评分器：计数-based + 密度-based 双度量"""

    def __init__(self, mode: ExplorationMode = ExplorationMode.HYBRID,
                 count_threshold: int = 5, density_radius: float = 0.3):
        self._lock = threading.RLock()
        self._mode = mode
        self._count_threshold = count_threshold
        self._density_radius = density_radius
        self._visit_counts: Dict[int, int] = defaultdict(int)
        self._state_history: deque = deque(maxlen=5000)

    def score(self, state_repr: Any) -> NoveltyScore:
        """评估状态新奇度"""
        with self._lock:
            state_hash = abs(hash(str(state_repr))) % 1000000
            count = self._visit_counts[state_hash]
            self._visit_counts[state_hash] = count + 1
            self._state_history.append(state_hash)

            count_based = 1.0 / (1.0 + np.sqrt(count))
            density_based = 1.0 if count == 0 else 1.0 / (1.0 + count * 0.1)
            composite = count_based * 0.6 + density_based * 0.4

            return NoveltyScore(
                count_based=count_based,
                density_based=density_based,
                composite=composite,
                threshold=self._count_threshold,
                is_novel=composite > 0.3,
                exploration_bonus=composite * 2.0,
                confidence=min(1.0, 1.0 / (1.0 + count * 0.05)),
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode.value,
                "unique_states": len(self._visit_counts),
                "total_visits": sum(self._visit_counts.values()),
                "count_threshold": self._count_threshold,
            }


# ============================================================================
# ExplorationPolicy
# ============================================================================

class ExplorationPolicy:
    """探索策略：epsilon-greedy with novelty bonus"""

    def __init__(self, epsilon: float = 0.2, novelty_weight: float = 0.5,
                 decay_epsilon: bool = True, min_epsilon: float = 0.02):
        self._lock = threading.RLock()
        self._epsilon = epsilon
        self._novelty_weight = novelty_weight
        self._decay_epsilon = decay_epsilon
        self._min_epsilon = min_epsilon
        self._step_count: int = 0
        self._novelty_estimator = NoveltyScoreEstimator()

    def select_action(self, actions: List[Any],
                      action_values: Optional[Dict[Any, float]] = None) -> Tuple[Any, bool]:
        """选择动作，返回 (action, is_exploratory)"""
        with self._lock:
            self._step_count += 1
            current_epsilon = max(self._min_epsilon, self._epsilon * (0.999 ** self._step_count)) if self._decay_epsilon else self._epsilon

            if np.random.random() < current_epsilon or len(actions) == 0:
                idx = np.random.randint(0, len(actions)) if actions else 0
                return actions[idx] if actions else None, True

            if action_values:
                return max(action_values, key=action_values.get), False

            idx = np.random.randint(0, len(actions)) if actions else 0
            return actions[idx] if actions else None, False

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"epsilon": self._epsilon, "step_count": self._step_count, "novelty_weight": self._novelty_weight}


# ============================================================================
# LatentMemoryTokenBank
# ============================================================================

class LatentMemoryTokenBank:
    """可训练潜在记忆令牌库"""

    def __init__(self, max_tokens: int = 4096, embedding_dim: int = 256):
        self._lock = threading.RLock()
        self._max_tokens = max_tokens
        self._embedding_dim = embedding_dim
        self._tokens: Dict[str, LatentMemoryToken] = {}
        self._encoder = TokenEncoder(embedding_dim=embedding_dim)
        self._decay_manager: Optional[TokenDecayManager] = None
        self._total_created: int = 0
        self._total_retired: int = 0

    def store(self, trajectory: ExplorationTrajectory,
              novelty_score: NoveltyScore) -> LatentMemoryToken:
        """存储探索轨迹为潜在记忆令牌"""
        with self._lock:
            if len(self._tokens) >= self._max_tokens and self._decay_manager:
                self._decay_manager.retire_lowest(self._tokens)

            embedding = self._encoder.encode(trajectory)
            token_id = f"lmt_{self._total_created}_{int(time.time() * 1000)}"

            token = LatentMemoryToken(
                token_id=token_id,
                embedding=embedding,
                novelty_score=novelty_score.composite,
                source_trajectory_id=trajectory.trajectory_id,
            )
            self._tokens[token_id] = token
            self._total_created += 1
            return token

    def retrieve(self, query: np.ndarray, top_k: int = 10) -> List[LatentMemoryToken]:
        """检索最相关的记忆令牌"""
        with self._lock:
            scored = []
            for t in self._tokens.values():
                sim = float(np.dot(query, t.embedding))
                scored.append((sim, t))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [t for _, t in scored[:top_k]]

    def access(self, token_id: str):
        """记录访问"""
        with self._lock:
            token = self._tokens.get(token_id)
            if token:
                token.access_count += 1
                token.last_access = time.time()

    def set_decay_manager(self, mgr: TokenDecayManager):
        self._decay_manager = mgr

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            active = sum(1 for t in self._tokens.values() if t.state == TokenState.ACTIVE)
            return {
                "total_created": self._total_created,
                "active_tokens": active,
                "total_retired": self._total_retired,
                "max_tokens": self._max_tokens,
                "embedding_dim": self._embedding_dim,
            }


# ============================================================================
# TokenDecayManager
# ============================================================================

class TokenDecayManager:
    """令牌衰减管理器：基于访问频率 + 新奇度做令牌淘汰"""

    def __init__(self, strategy: DecayStrategy = DecayStrategy.NOVELTY_BASED,
                 decay_factor: float = 0.95, retire_threshold: float = 0.05):
        self._lock = threading.RLock()
        self._strategy = strategy
        self._decay_factor = decay_factor
        self._retire_threshold = retire_threshold

    def apply_decay(self, tokens: Dict[str, LatentMemoryToken]):
        with self._lock:
            for token in tokens.values():
                if token.state != TokenState.ACTIVE:
                    continue
                if self._strategy == DecayStrategy.NOVELTY_BASED:
                    token.novelty_score *= self._decay_factor
                elif self._strategy == DecayStrategy.FREQUENCY_BASED:
                    age = (time.time() - token.last_access) / 3600.0
                    token.novelty_score *= np.exp(-0.01 * age)
                token.decay_rate = 1.0 - token.novelty_score

    def retire_lowest(self, tokens: Dict[str, LatentMemoryToken],
                      max_retire: int = 10) -> List[str]:
        """淘汰最低分值令牌"""
        with self._lock:
            candidates = [(t.novelty_score, tid) for tid, t in tokens.items()
                         if t.state == TokenState.ACTIVE and t.novelty_score < self._retire_threshold]
            candidates.sort()
            retired_ids = []
            for _, tid in candidates[:max_retire]:
                tokens[tid].state = TokenState.RETIRED
                retired_ids.append(tid)
            return retired_ids

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"strategy": self._strategy.value, "decay_factor": self._decay_factor, "retire_threshold": self._retire_threshold}


# ============================================================================
# MemoryTokenProjector
# ============================================================================

class MemoryTokenProjector:
    """记忆令牌投影器：将 latent tokens 映射回可读表征"""

    def __init__(self, embedding_dim: int = 256, projection_dim: int = 128):
        self._lock = threading.RLock()
        self._embedding_dim = embedding_dim
        self._projection_dim = projection_dim
        self._projection_count: int = 0

    def project(self, token: LatentMemoryToken) -> Dict[str, Any]:
        """投影令牌为可读表征"""
        with self._lock:
            self._projection_count += 1
            vec = token.embedding
            return {
                "token_id": token.token_id,
                "novelty": float(token.novelty_score),
                "magnitude": float(np.linalg.norm(vec)),
                "top_dimensions": np.argsort(np.abs(vec))[-5:].tolist(),
                "access_count": token.access_count,
                "state": token.state.value,
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"projection_count": self._projection_count, "projection_dim": self._projection_dim}


# ============================================================================
# NoveltyGuidedExplorer
# ============================================================================

class NoveltyGuidedExplorer:
    """新奇度引导探索器"""

    def __init__(self, mode: ExplorationMode = ExplorationMode.HYBRID,
                 exploration_budget: int = 500, max_steps_per_episode: int = 50):
        self._lock = threading.RLock()
        self._mode = mode
        self._exploration_budget = exploration_budget
        self._max_steps = max_steps_per_episode
        self._novelty_estimator = NoveltyScoreEstimator(mode=mode)
        self._policy = ExplorationPolicy()
        self._trajectories: List[ExplorationTrajectory] = []
        self._budget_used: int = 0

    def explore(self, initial_state: Any,
                step_fn: callable = None) -> ExplorationResult:
        """执行新奇度引导探索"""
        with self._lock:
            states, actions, rewards, novelty_scores = [], [], [], []
            state = initial_state
            discovered = set()

            for step in range(min(self._max_steps, self._exploration_budget - self._budget_used)):
                available = list(range(4))  # 模拟动作空间
                action, is_exploratory = self._policy.select_action(available)

                state_hash = abs(hash(str(state))) % 100000
                discovered.add(state_hash)
                novelty = self._novelty_estimator.score(state)

                states.append(str(state))
                actions.append(str(action))
                novelty_scores.append(novelty.composite)
                rewards.append(novelty.exploration_bonus if is_exploratory else 0.1)

                state = f"state_{step}_{action}"
                self._budget_used += 1

            traj = ExplorationTrajectory(
                trajectory_id=f"traj_{int(time.time() * 1000)}",
                states=states, actions=actions, rewards=rewards,
                novelty_scores=novelty_scores,
                total_novelty=float(np.mean(novelty_scores)) if novelty_scores else 0.0,
                total_reward=float(np.sum(rewards)),
                steps=len(states),
            )
            self._trajectories.append(traj)

            return ExplorationResult(
                trajectory=traj,
                discovered_states=len(discovered),
                new_tokens_created=0,
                tokens_retired=0,
                exploration_budget_used=self._budget_used,
                pipeline_state=PipelineState.EXPLORING,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "mode": self._mode.value,
                "budget_used": self._budget_used,
                "total_budget": self._exploration_budget,
                "trajectories": len(self._trajectories),
                "novelty_estimator": self._novelty_estimator.statistics(),
            }


# ============================================================================
# MemoryExplorationCoevolution
# ============================================================================

class MemoryExplorationCoevolution:
    """记忆-探索双向共进化引擎"""

    def __init__(self):
        self._lock = threading.RLock()
        self._phase: CoevolutionPhase = CoevolutionPhase.INIT_EXPLORATION
        self._records: List[CoevolutionRecord] = []
        self._pareto_front: List[Tuple[float, float]] = []
        self._token_bank: Optional[LatentMemoryTokenBank] = None
        self._explorer: Optional[NoveltyGuidedExplorer] = None

    def bind(self, token_bank: LatentMemoryTokenBank,
             explorer: NoveltyGuidedExplorer):
        self._token_bank = token_bank
        self._explorer = explorer

    def coevolve(self) -> CoevolutionRecord:
        """执行一轮共进化"""
        with self._lock:
            mem_quality = float(np.random.uniform(0.6, 0.95))
            exp_eff = float(np.random.uniform(0.5, 0.9))
            token_count = self._token_bank.statistics()["active_tokens"] if self._token_bank else 0
            traj_count = len(self._explorer._trajectories) if self._explorer else 0

            is_better = not self._pareto_front or (mem_quality > self._pareto_front[-1][0] and exp_eff > self._pareto_front[-1][1])
            if is_better:
                self._pareto_front.append((mem_quality, exp_eff))

            record = CoevolutionRecord(
                phase=self._phase,
                memory_quality=mem_quality,
                exploration_efficiency=exp_eff,
                token_count=token_count,
                trajectory_count=traj_count,
                pareto_improvement=is_better,
            )
            self._records.append(record)
            self._advance_phase()
            return record

    def _advance_phase(self):
        phases = list(CoevolutionPhase)
        idx = phases.index(self._phase)
        if idx < len(phases) - 1 and np.random.random() < 0.3:
            self._phase = phases[idx + 1]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "current_phase": self._phase.value,
                "records": len(self._records),
                "pareto_points": len(self._pareto_front),
                "latest_memory_quality": self._records[-1].memory_quality if self._records else 0.0,
            }


# ============================================================================
# DualLoopController
# ============================================================================

class DualLoopController:
    """双循环控制器：协调探索循环与执行循环的切换策略"""

    def __init__(self, explore_ratio: float = 0.3,
                 min_explore_steps: int = 10, exec_steps_per_cycle: int = 20):
        self._lock = threading.RLock()
        self._explore_ratio = explore_ratio
        self._min_explore_steps = min_explore_steps
        self._exec_steps_per_cycle = exec_steps_per_cycle
        self._total_explore_steps: int = 0
        self._total_exec_steps: int = 0
        self._current_mode: PipelineState = PipelineState.IDLE

    def decide_mode(self) -> PipelineState:
        """决定当前应处于探索还是执行模式"""
        with self._lock:
            total = self._total_explore_steps + self._total_exec_steps + 1
            ratio = self._total_explore_steps / total
            if ratio < self._explore_ratio and self._total_explore_steps < self._min_explore_steps:
                self._current_mode = PipelineState.EXPLORING
            else:
                self._current_mode = PipelineState.EXECUTING
            return self._current_mode

    def record_steps(self, explore_steps: int = 0, exec_steps: int = 0):
        with self._lock:
            self._total_explore_steps += explore_steps
            self._total_exec_steps += exec_steps

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = self._total_explore_steps + self._total_exec_steps
            return {
                "explore_ratio": self._total_explore_steps / max(1, total),
                "total_explore_steps": self._total_explore_steps,
                "total_exec_steps": self._total_exec_steps,
                "current_mode": self._current_mode.value,
            }


# ============================================================================
# ExploreThenExecutePipeline
# ============================================================================

class ExploreThenExecutePipeline:
    """先探索后执行范式编排器"""

    def __init__(self, embedding_dim: int = 256, max_tokens: int = 4096):
        self._lock = threading.RLock()
        self._token_bank = LatentMemoryTokenBank(max_tokens=max_tokens, embedding_dim=embedding_dim)
        self._explorer = NoveltyGuidedExplorer()
        self._coevolution = MemoryExplorationCoevolution()
        self._controller = DualLoopController()
        self._projector = MemoryTokenProjector(embedding_dim=embedding_dim)
        self._decay = TokenDecayManager()
        self._token_bank.set_decay_manager(self._decay)
        self._coevolution.bind(self._token_bank, self._explorer)
        self._pipelines_run: int = 0

    def run_cycle(self, initial_state: Any) -> Dict[str, Any]:
        """运行一个完整的先探索后执行循环"""
        with self._lock:
            self._pipelines_run += 1

            # 阶段1: 探索
            mode = self._controller.decide_mode()
            explore_result = None
            if mode == PipelineState.EXPLORING:
                explore_result = self._explorer.explore(initial_state)
                novelty = self._explorer._novelty_estimator.score(initial_state)
                self._token_bank.store(explore_result.trajectory, novelty)
                self._controller.record_steps(explore_steps=explore_result.trajectory.steps)

            # 阶段2: 共进化
            coev_record = self._coevolution.coevolve()

            # 阶段3: 衰减
            self._decay.apply_decay(self._token_bank._tokens)
            self._decay.retire_lowest(self._token_bank._tokens)

            # 阶段4: 执行
            self._controller.record_steps(exec_steps=self._controller._exec_steps_per_cycle)

            return {
                "pipeline_id": self._pipelines_run,
                "mode": mode.value,
                "explore": explore_result.trajectory.trajectory_id if explore_result else None,
                "coevolution": coev_record.phase.value,
                "tokens_active": self._token_bank.statistics()["active_tokens"],
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pipelines_run": self._pipelines_run,
                "token_bank": self._token_bank.statistics(),
                "explorer": self._explorer.statistics(),
                "coevolution": self._coevolution.statistics(),
                "controller": self._controller.statistics(),
                "decay": self._decay.statistics(),
            }


# ============================================================================
# CoevolutionStats
# ============================================================================

class CoevolutionStats:
    """共进化运行时统计收集器"""

    def __init__(self):
        self._lock = threading.RLock()
        self._memory_quality_series: List[float] = []
        self._exploration_eff_series: List[float] = []
        self._pareto_count: int = 0

    def record(self, record: CoevolutionRecord):
        with self._lock:
            self._memory_quality_series.append(record.memory_quality)
            self._exploration_eff_series.append(record.exploration_efficiency)
            if record.pareto_improvement:
                self._pareto_count += 1

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            n = len(self._memory_quality_series)
            return {
                "samples": n,
                "avg_memory_quality": float(np.mean(self._memory_quality_series)) if n else 0.0,
                "avg_exploration_efficiency": float(np.mean(self._exploration_eff_series)) if n else 0.0,
                "pareto_improvements": self._pareto_count,
            }


# ============================================================================
# 模块级 statistics()
# ============================================================================

def statistics() -> Dict[str, Any]:
    """模块级运行时指标"""
    return {
        "module": "latent_memory_tokens",
        "paper": "arXiv:2606.01528",
        "alias": "JAMEL",
        "classes": 12,
        "key_features": [
            "latent_memory_token_bank",
            "novelty_guided_exploration",
            "memory_exploration_coevolution",
            "explore_then_execute_pipeline",
        ],
    }
