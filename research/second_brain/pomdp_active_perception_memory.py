"""
# status: orphan (2026-08-15 audit, not in runtime path)
POMDPActivePerceptionMemory — OmniAgent Observation→Thought→Action Loop
========================================================================
arXiv 2606.19341 · P39-3

实现 POMDP 主动感知记忆循环: Observation→Thought→Action 迭代,
将原始观察蒸馏为持久文本记忆; TAURA 基于轮次熵的加权优势估计
用于信用分配; decouple_memory() 解耦推理复杂度与输入长度。

设计要点:
  - POMDPState: 包含 observation/thought/action 三元组的信念状态
  - TAURAEstimator: 基于轮次熵的加权优势估计器
  - PerceptionCycle: 单轮感知循环的完整轨迹
  - DecoupledMemoryBlock: 解耦后的持久文本记忆 (与原始输入长度无关)
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class POMDPPhase(Enum):
    """POMDP 循环阶段。"""
    OBSERVE = auto()
    THINK = auto()
    ACT = auto()
    IDLE = auto()


class ActionType(Enum):
    """动作类型。"""
    RETRIEVE = auto()
    QUERY_EXTERNAL = auto()
    REPORT = auto()
    NO_OP = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class POMDPObservation:
    """一次原始观察——外部或内部信息片段。"""
    obs_id: str
    content: str
    source: str = ""
    entropy: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class POMDPState:
    """POMDP 信念状态——当前轮次的观察-思考-行动三元组。"""
    state_id: str
    cycle_index: int
    observation: Optional[POMDPObservation] = None
    thought: str = ""
    action: ActionType = ActionType.NO_OP
    action_detail: str = ""
    phase: POMDPPhase = POMDPPhase.IDLE
    entropy: float = 0.0
    confidence: float = 1.0


@dataclass
class TAURATrajectory:
    """TAURA 轨迹——一条完整的回合轨迹, 含每轮状态与收益。"""
    trajectory_id: str
    states: List[POMDPState] = field(default_factory=list)
    total_reward: float = 0.0
    entropy_weights: List[float] = field(default_factory=list)


@dataclass
class PerceptionCycle:
    """一次完整的 Observation→Thought→Action 循环。"""
    cycle_id: str
    index: int
    observation: POMDPObservation
    thought: str
    action: ActionType
    action_detail: str
    reward: float = 0.0
    advantage: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class DecoupledMemoryBlock:
    """解耦后的持久文本记忆——独立于输入长度。"""
    block_id: str
    abstract_summary: str
    key_facts: List[str] = field(default_factory=list)
    original_cycles: List[int] = field(default_factory=list)
    compress_ratio: float = 1.0
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# TAURAEstimator
# ---------------------------------------------------------------------------

class TAURAEstimator:
    """TAURA (Turn-entropy Augmented weighted advantage) 信用分配估计器。

    为感知循环中的每一步计算加权优势, 基于轮次熵自适应调整权重;
    高熵轮次(不确定性大)权重降低, 低熵轮次权重提高。

    Parameters
    ----------
    gamma : float
        折扣因子, 默认 0.99。
    lambda_ : float
        GAE 参数, 默认 0.95。
    """

    def __init__(self, gamma: float = 0.99, lambda_: float = 0.95) -> None:
        self.gamma = gamma
        self.lambda_ = lambda_
        logger.info("TAURAEstimator initialized [γ=%.3f λ=%.3f]", gamma, lambda_)

    def compute_advantages(
        self,
        cycles: List[PerceptionCycle],
        value_func: Optional[Callable[[POMDPState], float]] = None,
    ) -> List[float]:
        """计算 TAURA 加权优势。

        Parameters
        ----------
        cycles : List[PerceptionCycle]
            一个回合的感知循环列表。
        value_func : Optional[Callable]
            V(s) 估计函数, 默认用 zero baseline。

        Returns
        -------
        List[float]
            每步的加权优势值。
        """
        if not cycles:
            return []

        n = len(cycles)
        advantages = np.zeros(n, dtype=np.float64)

        # Compute TD errors
        rewards = np.array([c.reward for c in cycles])
        entropies = np.array([c.observation.entropy for c in cycles])
        # TAURA weight: higher entropy → lower weight
        max_ent = float(np.max(entropies)) if np.max(entropies) > 1e-8 else 1.0
        entropy_weights = 1.0 - (entropies / (max_ent + 1e-8))
        entropy_weights = np.clip(entropy_weights, 0.1, 1.0)

        # GAE + entropy weighting
        last_adv = 0.0
        for t in reversed(range(n)):
            # Value estimate: if no value_func, use running avg reward
            v_t = value_func(cycles[t]) if value_func else float(np.mean(rewards[: t + 1]) if t >= 0 else 0.0)  # type: ignore[call-overload]
            v_next = value_func(cycles[t + 1]) if value_func and t + 1 < n else 0.0  # type: ignore[call-overload]
            delta = rewards[t] + self.gamma * v_next - v_t
            last_adv = delta + self.gamma * self.lambda_ * last_adv
            advantages[t] = float(last_adv * entropy_weights[t])

        return advantages.tolist()

    def compute_trajectory_score(self, trajectory: TAURATrajectory) -> Dict[str, Any]:
        """计算整条轨迹的综合评分。

        Returns
        -------
        Dict[str, Any]
            含 total_reward / mean_advantage / entropy_profile。
        """
        if not trajectory.states:
            return {"total_reward": 0.0, "mean_advantage": 0.0, "entropy_profile": []}

        # Use stored entropy weights or recompute from states
        entropies = [s.entropy for s in trajectory.states]
        max_ent = float(np.max(entropies)) if entropies and max(entropies) > 1e-8 else 1.0
        return {
            "total_reward": trajectory.total_reward,
            "mean_entropy": float(np.mean(entropies)) if entropies else 0.0,
            "max_entropy": max_ent,
            "entropy_profile": entropies[:10],
        }


# ---------------------------------------------------------------------------
# POMDPActivePerceptionMemory
# ---------------------------------------------------------------------------

class POMDPActivePerceptionMemory:
    """OmniAgent 主动感知记忆系统。

    Parameters
    ----------
    max_cycles_per_episode : int
        单回合最大循环次数。
    embedding_dim : int
        嵌入向量维度。
    """

    def __init__(self, max_cycles_per_episode: int = 64, embedding_dim: int = 384) -> None:
        self.max_cycles_per_episode = max_cycles_per_episode
        self.embedding_dim = embedding_dim
        self._taura = TAURAEstimator()
        self._lock = threading.RLock()

        self._cycles: List[PerceptionCycle] = []
        self._decoupled: List[DecoupledMemoryBlock] = []
        self._cycle_index: int = 0
        self._observation_count: int = 0
        self._current_states: deque = deque(maxlen=8)

        logger.info("POMDPActivePerceptionMemory initialized [max_cycles=%d]", max_cycles_per_episode)

    # ------------------------------------------------------------------
    # POMDP Loop
    # ------------------------------------------------------------------

    def observe(self, content: str, source: str = "", entropy: float = 0.0) -> POMDPObservation:
        """接收一次新观察。

        Parameters
        ----------
        content : str
            观察内容。
        source : str
            来源。
        entropy : float
            观察的不确定性 (0.0~1.0)。

        Returns
        -------
        POMDPObservation
        """
        with self._lock:
            self._observation_count += 1
            obs = POMDPObservation(
                obs_id=f"obs_{self._observation_count}",
                content=content,
                source=source,
                entropy=float(np.clip(entropy, 0.0, 1.0)),
            )
            state = POMDPState(
                state_id=f"state_{self._observation_count}",
                cycle_index=self._cycle_index,
                observation=obs,
                phase=POMDPPhase.OBSERVE,
                entropy=entropy,
            )
            self._current_states.append(state)
            return obs

    def think(self, thought: str, confidence: float = 0.5) -> None:
        """基于当前观察生成思考。

        Parameters
        ----------
        thought : str
            推理/思考内容。
        confidence : float
            置信度 (0.0~1.0)。
        """
        with self._lock:
            if not self._current_states:
                return
            state: POMDPState = self._current_states[-1]
            state.thought = thought
            state.confidence = float(np.clip(confidence, 0.0, 1.0))
            state.phase = POMDPPhase.THINK

    def act(self, action: ActionType, detail: str = "", reward: float = 0.0) -> PerceptionCycle:
        """执行动作, 完成一轮 O→T→A 循环。

        Parameters
        ----------
        action : ActionType
            动作类型。
        detail : str
            动作详情。
        reward : float
            环境反馈收益。

        Returns
        -------
        PerceptionCycle
            完成的感知循环。
        """
        with self._lock:
            if not self._current_states:
                raise RuntimeError("No observation to act on")

            state: POMDPState = self._current_states.pop()
            obs = state.observation
            if obs is None:
                raise RuntimeError("State has no observation")

            state.action = action
            state.action_detail = detail
            state.phase = POMDPPhase.ACT

            cycle = PerceptionCycle(
                cycle_id=f"cycle_{self._cycle_index}_{obs.obs_id}",
                index=self._cycle_index,
                observation=obs,
                thought=state.thought,
                action=action,
                action_detail=detail,
                reward=reward,
            )
            self._cycles.append(cycle)
            self._cycle_index += 1
            return cycle

    def run_cycle(
        self,
        observation: str,
        thought: str,
        action: ActionType,
        detail: str = "",
        reward: float = 0.0,
        entropy: float = 0.0,
    ) -> PerceptionCycle:
        """一步完成 observe → think → act 的便捷方法。"""
        self.observe(observation, entropy=entropy)
        self.think(thought)
        return self.act(action, detail, reward)

    # ------------------------------------------------------------------
    # TAURA Credit Assignment
    # ------------------------------------------------------------------

    def assign_credit(self, episode_cycles: Optional[List[PerceptionCycle]] = None) -> List[float]:
        """为指定回合的循环计算 TAURA 优势。

        Parameters
        ----------
        episode_cycles : Optional[List[PerceptionCycle]]
            指定回合; 默认用最近 max_cycles_per_episode 个循环。

        Returns
        -------
        List[float]
            每步优势值。
        """
        with self._lock:
            cycles = episode_cycles or self._cycles[-self.max_cycles_per_episode:]
            advantages = self._taura.compute_advantages(cycles)

            # 回写优势
            for c, adv in zip(cycles, advantages):
                c.advantage = adv

            return advantages

    def get_top_cycles(self, n: int = 5) -> List[PerceptionCycle]:
        """返回优势最高的 n 个循环。"""
        with self._lock:
            scored = [(c, c.advantage) for c in self._cycles if c.advantage > 0]
            scored.sort(key=lambda x: x[1], reverse=True)
            return [c for c, _ in scored[:n]]

    # ------------------------------------------------------------------
    # Decouple Memory (Test-Time Scaling)
    # ------------------------------------------------------------------

    def decouple_memory(
        self,
        cycles: Optional[List[PerceptionCycle]] = None,
        max_original_length: int = 2000,
    ) -> DecoupledMemoryBlock:
        """将原始观察解耦为压缩的持久文本记忆。

        - 蒸馏: 从多轮观察中提取摘要和关键事实
        - 解耦: 得到与原始输入长度无关的紧凑记忆块
        - 扩展: 增加推理轮次不增加记忆存储开销

        Parameters
        ----------
        cycles : Optional[List[PerceptionCycle]]
            要解耦的循环; 默认用最近一个 episode。
        max_original_length : int
            原始输入长度估算上限 (用于计算压缩比)。

        Returns
        -------
        DecoupledMemoryBlock
            解耦的持久记忆块。
        """
        with self._lock:
            cycles = cycles or self._cycles[-self.max_cycles_per_episode:]
            if not cycles:
                return DecoupledMemoryBlock(
                    block_id=f"dmb_{int(time.time()*1e6)}",
                    abstract_summary="",
                    compress_ratio=1.0,
                )

            # 提取摘要: 合并所有 thought, 截断到 ~300 字符
            all_thoughts = ". ".join(c.thought for c in cycles if c.thought)
            summary = all_thoughts[:500].strip()
            if len(all_thoughts) > 500:
                summary += "..."

            # 提取关键事实: 从 observation 和 thought 提取
            facts: List[str] = []
            for c in cycles:
                obs_text = c.observation.content if c.observation else ""
                # Simple key fact extraction: sentences with named entities or numeric data
                for sentence in obs_text.replace("!", ".").replace("?", ".").split("."):
                    s = sentence.strip()
                    if s and any(ch.isdigit() for ch in s):
                        facts.append(s[:120])
                        if len(facts) >= 10:
                            break
                if len(facts) >= 10:
                    break

            original_length = sum(len(c.observation.content) if c.observation else 0 for c in cycles)
            compressed_length = len(summary) + sum(len(f) for f in facts)
            compress_ratio = compressed_length / max(original_length, 1)

            block = DecoupledMemoryBlock(
                block_id=f"dmb_{int(time.time()*1e6)}",
                abstract_summary=summary,
                key_facts=facts[:10],
                original_cycles=[c.index for c in cycles],
                compress_ratio=compress_ratio,
            )
            self._decoupled.append(block)
            logger.info("Decoupled: %d cycles → %d chars (ratio %.2f)", len(cycles), compressed_length, compress_ratio)
            return block

    def list_decoupled(self) -> List[DecoupledMemoryBlock]:
        return list(self._decoupled)

    # ------------------------------------------------------------------
    # Test-Time Scaling
    # ------------------------------------------------------------------

    def scale_reasoning_rounds(self, additional_rounds: int) -> int:
        """增加推理轮次 (测试时扩展)。

        更多推理轮次可提升性能 (OmniAgent 论文核心发现)。

        Parameters
        ----------
        additional_rounds : int
            追加的推理轮次。

        Returns
        -------
        int
            新的最大轮次。
        """
        with self._lock:
            new_max = self.max_cycles_per_episode + additional_rounds
            logger.info("Test-time scaling: %d → %d rounds", self.max_cycles_per_episode, new_max)
            self.max_cycles_per_episode = new_max
            return new_max

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_cycles": len(self._cycles),
                "total_observations": self._observation_count,
                "max_cycles_per_episode": self.max_cycles_per_episode,
                "decoupled_blocks": len(self._decoupled),
                "avg_reward": float(np.mean([c.reward for c in self._cycles])) if self._cycles else 0.0,
                "mean_compress_ratio": (
                    float(np.mean([b.compress_ratio for b in self._decoupled])) if self._decoupled else 1.0
                ),
                "top_advantage_cycles": [c.cycle_id for c in self.get_top_cycles(3)],
            }
