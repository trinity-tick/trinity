"""
# status: orphan (2026-08-15 audit, not in runtime path)
P14-6: Hindsight Self-Reflection (对标 RetroAgent · Shanghai AI Lab/NUS)
=============================================================================

核心设计（RetroAgent: Post-hoc Trajectory Analysis + Dual Feedback Loops）：
  - TrajectoryTrace：完整轨迹链——步步记录 action / observation / reward
  - RetrospectiveScore：事后分析——数值评分 + 文本教训
  - DualFeedbackLoop：双重内在反馈——内在奖励信号 + 文本记忆反思
  - SimUtilUCBRetriever：效用感知记忆检索——utility × similarity × UCB
  - ExplorationTracker：探索阶段避免重复已失败路径

兼容性：
  - 与 episodic_rl.py（EpisodicRLScorer）接口兼容
  - 与 workflow_memory.py（WorkflowSynthesizer）轨迹模式兼容
  - 与 episodic_reflection.py（EpisodicReflectionPipeline）事后反思管线兼容

Reference:
  - RetroAgent: Post-hoc Trajectory Analysis with Dual Feedback Loops (Shanghai AI Lab / NUS)
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ────────────────────────────────────────────────────

class ExperienceType(Enum):
    """经验类型。"""
    SUCCESS = "success"          # 成功经验
    FAILURE = "failure"          # 失败经验
    PARTIAL = "partial"          # 部分成功
    UNKNOWN = "unknown"          # 未分类


class ReflectionSeverity(Enum):
    """反思严重程度。"""
    CRITICAL = "critical"        # 严重：必须避免重复
    MAJOR = "major"              # 重要：显著影响效果
    MINOR = "minor"              # 次要：小幅度改善
    INFO = "info"                # 信息：记录但不强制


class FeedbackType(Enum):
    """反馈类型。"""
    INTRINSIC_REWARD = "intrinsic_reward"    # 内在奖励信号
    TEXTUAL_MEMORY = "textual_memory"        # 文本记忆反思
    UTILITY_SCORE = "utility_score"          # 效用评分


class ExplorationPhase(Enum):
    """探索阶段。"""
    EXPLORE = "explore"          # 探索阶段——鼓励尝试
    EXPLOIT = "exploit"          # 利用阶段——优先已知好路径
    BALANCED = "balanced"        # 平衡阶段


# ── 数据结构 ──────────────────────────────────────────────────────

@dataclass
class TrajectoryStep:
    """轨迹中的单步。"""
    step_id: str
    step_index: int
    action: str
    observation: str
    reward: float = 0.0
    state_hash: Optional[str] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TrajectoryTrace:
    """完整轨迹。"""
    trace_id: str
    session_id: str
    task_description: str
    steps: List[TrajectoryStep] = field(default_factory=list)
    outcome: ExperienceType = ExperienceType.UNKNOWN
    total_reward: float = 0.0
    final_score: float = 0.0
    duration_seconds: float = 0.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    tags: List[str] = field(default_factory=list)


@dataclass
class RetrospectiveScore:
    """事后分析评分。"""
    trajectory_id: str
    numeric_score: float                         # 0.0 ~ 1.0
    textual_lesson: str                          # 文本教训/洞见
    severity: ReflectionSeverity = ReflectionSeverity.INFO
    failure_root_cause: Optional[str] = None     # 失败根因（如有）
    success_factors: List[str] = field(default_factory=list)
    improvement_suggestions: List[str] = field(default_factory=list)
    analyzed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    confidence: float = 0.8


@dataclass
class FeedbackSignal:
    """反馈信号。"""
    feedback_id: str
    feedback_type: FeedbackType
    trajectory_id: str
    value: Any                                  # 数值或文本
    weight: float = 1.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    source: str = "dual_loop"


@dataclass
class SimUtilScore:
    """SimUtil-UCB 综合评分。"""
    memory_id: str
    utility: float          # 效用评分（该记忆历史帮助程度）
    similarity: float       # 与当前查询的相似度
    ucb_bonus: float        # UCB 探索奖励
    combined_score: float   # 加权组合 = α·utility + β·similarity + γ·ucb
    retrieval_count: int = 0


@dataclass
class ExplorationState:
    """探索状态追踪。"""
    phase: ExplorationPhase = ExplorationPhase.EXPLORE
    failed_paths: Set[str] = field(default_factory=set)
    visited_states: Dict[str, int] = field(default_factory=dict)  # state_hash → visit count
    successful_paths: Set[str] = field(default_factory=set)
    total_episodes: int = 0
    explore_rate: float = 0.3


# ── 双重反馈循环 ─────────────────────────────────────────────────

class DualFeedbackLoop:
    """内在奖励 + 文本记忆双重反馈循环。"""

    def __init__(
        self,
        intrinsic_weight: float = 0.3,
        textual_weight: float = 0.7,
        decay_factor: float = 0.95,
    ):
        self._intrinsic_weight = intrinsic_weight
        self._textual_weight = textual_weight
        self._decay_factor = decay_factor
        self._signals: List[FeedbackSignal] = []
        self._reflection_buffer: List[RetrospectiveScore] = []
        self._lock = threading.RLock()
        logger.info(
            "DualFeedbackLoop initialized (intrinsic=%.2f, textual=%.2f)",
            intrinsic_weight, textual_weight,
        )

    def emit_intrinsic_reward(
        self,
        trajectory_id: str,
        reward: float,
        weight: float = 1.0,
    ) -> str:
        fid = f"fb_{uuid.uuid4().hex[:12]}"
        signal = FeedbackSignal(
            feedback_id=fid,
            feedback_type=FeedbackType.INTRINSIC_REWARD,
            trajectory_id=trajectory_id,
            value=reward,
            weight=weight,
        )
        with self._lock:
            self._signals.append(signal)
        return fid

    def emit_textual_reflection(
        self,
        trajectory_id: str,
        lesson: str,
        weight: float = 1.0,
    ) -> str:
        fid = f"fb_{uuid.uuid4().hex[:12]}"
        signal = FeedbackSignal(
            feedback_id=fid,
            feedback_type=FeedbackType.TEXTUAL_MEMORY,
            trajectory_id=trajectory_id,
            value=lesson,
            weight=weight,
        )
        with self._lock:
            self._signals.append(signal)
            # Also store as reflection
            reflection = RetrospectiveScore(
                trajectory_id=trajectory_id,
                numeric_score=0.5,  # default neutral
                textual_lesson=lesson,
                severity=ReflectionSeverity.INFO,
            )
            self._reflection_buffer.append(reflection)
        return fid

    def get_composite_signal(self, trajectory_id: str) -> Optional[float]:
        with self._lock:
            intrinsic = [
                s.value for s in self._signals
                if s.trajectory_id == trajectory_id and s.feedback_type == FeedbackType.INTRINSIC_REWARD
            ]
            textual = [
                s.weight for s in self._signals
                if s.trajectory_id == trajectory_id and s.feedback_type == FeedbackType.TEXTUAL_MEMORY
            ]
            if not intrinsic and not textual:
                return None
            avg_intrinsic = np.mean(intrinsic) if intrinsic else 0.0
            avg_textual_weight = np.mean(textual) if textual else 0.0
            composite = (
                self._intrinsic_weight * avg_intrinsic +
                self._textual_weight * avg_textual_weight
            )
            return float(composite)

    def get_reflections(
        self,
        min_severity: ReflectionSeverity = ReflectionSeverity.INFO,
        limit: int = 20,
    ) -> List[RetrospectiveScore]:
        with self._lock:
            filtered = [r for r in self._reflection_buffer
                        if r.severity.value <= min_severity.value]
            return sorted(filtered, key=lambda r: r.analyzed_at, reverse=True)[:limit]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_signals": len(self._signals),
                "total_reflections": len(self._reflection_buffer),
                "intrinsic_weight": self._intrinsic_weight,
                "textual_weight": self._textual_weight,
            }


# ── SimUtil-UCB 效用感知检索器 ───────────────────────────────────

class SimUtilUCBRetriever:
    """
    SimUtil-UCB: utility × similarity + UCB exploration bonus.
    效用感知记忆检索——在利用已知好记忆与探索未知之间平衡。
    """

    _ALPHA = 0.6   # utility weight
    _BETA = 0.3    # similarity weight
    _GAMMA = 0.1   # UCB bonus weight

    def __init__(
        self,
        alpha: float = _ALPHA,
        beta: float = _BETA,
        gamma: float = _GAMMA,
    ):
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._memory_utilities: Dict[str, float] = {}      # memory_id → utility
        self._retrieval_counts: Dict[str, int] = defaultdict(int)
        self._total_retrievals: int = 0
        self._lock = threading.RLock()
        logger.info("SimUtilUCBRetriever initialized (α=%.2f, β=%.2f, γ=%.2f)", alpha, beta, gamma)

    def update_utility(self, memory_id: str, reward: float):
        """更新某记忆的效用估值（指数滑动平均）。"""
        with self._lock:
            old = self._memory_utilities.get(memory_id, 0.5)
            self._memory_utilities[memory_id] = 0.9 * old + 0.1 * reward

    def compute_scores(
        self,
        query_vector: np.ndarray,
        memories: Dict[str, np.ndarray],     # memory_id → vector
    ) -> List[SimUtilScore]:
        with self._lock:
            scores: List[SimUtilScore] = []
            for mid, mem_vec in memories.items():
                utility = self._memory_utilities.get(mid, 0.5)
                similarity = float(np.dot(mem_vec, query_vector))
                count = self._retrieval_counts.get(mid, 0)
                if self._total_retrievals > 0:
                    ucb_bonus = math.sqrt(2 * math.log(self._total_retrievals + 1) / (count + 1))
                else:
                    ucb_bonus = 1.0

                combined = (
                    self._alpha * utility +
                    self._beta * similarity +
                    self._gamma * ucb_bonus
                )
                scores.append(SimUtilScore(
                    memory_id=mid,
                    utility=utility,
                    similarity=similarity,
                    ucb_bonus=ucb_bonus,
                    combined_score=combined,
                    retrieval_count=count,
                ))
            scores.sort(key=lambda s: s.combined_score, reverse=True)
            return scores

    def record_retrieval(self, memory_id: str):
        with self._lock:
            self._retrieval_counts[memory_id] += 1
            self._total_retrievals += 1

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_retrievals": self._total_retrievals,
                "tracked_memories": len(self._memory_utilities),
                "alpha": self._alpha,
                "beta": self._beta,
                "gamma": self._gamma,
                "avg_utility": float(np.mean(list(self._memory_utilities.values()))) if self._memory_utilities else 0.0,
            }


# ── 探索追踪器 ────────────────────────────────────────────────────

class ExplorationTracker:
    """探索阶段避免重复已失败路径。"""

    _FAILED_PATH_THRESHOLD = 2  # 同一路径失败超过此数，降低探索概率

    def __init__(self, initial_explore_rate: float = 0.3):
        self._state = ExplorationState(explore_rate=initial_explore_rate)
        self._path_signatures: Dict[str, int] = defaultdict(int)  # signature → failure count
        self._lock = threading.RLock()
        logger.info("ExplorationTracker initialized (rate=%.2f)", initial_explore_rate)

    def register_success(self, state_hash: str):
        with self._lock:
            self._state.successful_paths.add(state_hash)
            self._state.visited_states[state_hash] = self._state.visited_states.get(state_hash, 0) + 1
            self._state.total_episodes += 1
            self._update_phase()

    def register_failure(self, state_hash: str, path_signature: str):
        with self._lock:
            self._state.failed_paths.add(state_hash)
            self._state.visited_states[state_hash] = self._state.visited_states.get(state_hash, 0) + 1
            self._path_signatures[path_signature] += 1
            self._state.total_episodes += 1
            self._update_phase()

    def should_explore(self, path_signature: str) -> bool:
        """判断是否应该探索（而非利用）给定路径。"""
        with self._lock:
            # If this path has failed too many times, avoid it
            fail_count = self._path_signatures.get(path_signature, 0)
            if fail_count >= self._FAILED_PATH_THRESHOLD:
                return False
            # Otherwise, explore with probability explore_rate
            return np.random.random() < self._state.explore_rate

    def get_explore_probability(self, path_signature: str) -> float:
        with self._lock:
            fail_count = self._path_signatures.get(path_signature, 0)
            if fail_count >= self._FAILED_PATH_THRESHOLD:
                return 0.0
            return self._state.explore_rate

    def _update_phase(self):
        total = self._state.total_episodes
        if total < 10:
            self._state.phase = ExplorationPhase.EXPLORE
            self._state.explore_rate = 0.5
        elif total < 50:
            self._state.phase = ExplorationPhase.BALANCED
            self._state.explore_rate = max(0.1, 0.3 - 0.004 * (total - 10))
        else:
            self._state.phase = ExplorationPhase.EXPLOIT
            self._state.explore_rate = max(0.05, 0.1 - 0.001 * (total - 50))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "phase": self._state.phase.value,
                "explore_rate": self._state.explore_rate,
                "total_episodes": self._state.total_episodes,
                "successful_paths": len(self._state.successful_paths),
                "failed_paths": len(self._state.failed_paths),
                "avoided_paths": sum(1 for v in self._path_signatures.values() if v >= self._FAILED_PATH_THRESHOLD),
            }


# ── 事后自反思（顶层调度器）──────────────────────────────────────

class HindsightSelfReflection:
    """事后轨迹分析 + 双重反馈循环顶层调度。"""

    _VERSION = "1.0.0"

    def __init__(
        self,
        intrinsic_weight: float = 0.3,
        textual_weight: float = 0.7,
        initial_explore_rate: float = 0.3,
    ):
        self._dual_feedback = DualFeedbackLoop(
            intrinsic_weight=intrinsic_weight,
            textual_weight=textual_weight,
        )
        self._retriever = SimUtilUCBRetriever()
        self._tracker = ExplorationTracker(initial_explore_rate=initial_explore_rate)
        self._trajectories: Dict[str, TrajectoryTrace] = {}
        self._reflections: List[RetrospectiveScore] = []
        self._lock = threading.RLock()
        self._version = self._VERSION
        logger.info("HindsightSelfReflection v%s initialized", self._version)

    # ── 轨迹管理 ─────────────────────────────────────────────────

    def record_step(
        self,
        trace_id: str,
        step_index: int,
        action: str,
        observation: str,
        reward: float = 0.0,
        state_hash: Optional[str] = None,
    ) -> str:
        step_id = f"step_{uuid.uuid4().hex[:12]}"
        step = TrajectoryStep(
            step_id=step_id,
            step_index=step_index,
            action=action,
            observation=observation,
            reward=reward,
            state_hash=state_hash or hashlib.md5(f"{action}:{observation}".encode()).hexdigest()[:16],
        )
        with self._lock:
            trace = self._trajectories.get(trace_id)
            if not trace:
                trace = TrajectoryTrace(
                    trace_id=trace_id,
                    session_id="default",
                    task_description="",
                )
                self._trajectories[trace_id] = trace
            trace.steps.append(step)
            trace.total_reward += reward
        return step_id

    def finalize_trajectory(
        self,
        trace_id: str,
        outcome: ExperienceType,
        final_score: float = 0.0,
        duration: float = 0.0,
    ) -> Optional[TrajectoryTrace]:
        """关闭轨迹并进行事后分析。"""
        with self._lock:
            trace = self._trajectories.get(trace_id)
            if not trace:
                return None
            trace.outcome = outcome
            trace.final_score = final_score
            trace.duration_seconds = duration

            # Generate reflection
            reflection = self._analyze_trajectory(trace)
            self._reflections.append(reflection)

            # Update exploration tracker
            if trace.steps:
                last_step = trace.steps[-1]
                path_sig = hashlib.md5(
                    "→".join(s.action for s in trace.steps).encode()
                ).hexdigest()[:16]
                if outcome == ExperienceType.SUCCESS:
                    self._tracker.register_success(last_step.state_hash or "unknown")
                else:
                    self._tracker.register_failure(last_step.state_hash or "unknown", path_sig)

            # Update utility for retriever
            reward = 1.0 if outcome == ExperienceType.SUCCESS else 0.1 if outcome == ExperienceType.PARTIAL else 0.0
            for step in trace.steps:
                self._retriever.update_utility(step.step_id, reward)

            return trace

    def _analyze_trajectory(self, trace: TrajectoryTrace) -> RetrospectiveScore:
        """事后分析轨迹并生成评分与教训。"""
        if trace.outcome == ExperienceType.SUCCESS:
            score = max(0.7, trace.final_score)
            lesson = f"SUCCESS: Task '{trace.task_description[:60]}' completed with score {score:.2f}. "
            lesson += "Key pattern: consistent positive reward across steps."
            severity = ReflectionSeverity.MINOR
            success_factors = ["consistent_action_selection", "positive_reward_accumulation"]
            failure_cause = None
        elif trace.outcome == ExperienceType.FAILURE:
            score = min(0.3, trace.final_score)
            lesson = f"FAILURE: Task '{trace.task_description[:60]}' ended with score {score:.2f}. "
            # Find lowest-reward step as root cause
            if trace.steps:
                worst = min(trace.steps, key=lambda s: s.reward)
                lesson += f"Root cause: step {worst.step_index} action '{worst.action}' received reward {worst.reward:.2f}."
                failure_cause = f"step_{worst.step_index}: low reward action '{worst.action}'"
            else:
                failure_cause = "no_steps_recorded"
            severity = ReflectionSeverity.CRITICAL if score < 0.2 else ReflectionSeverity.MAJOR
            success_factors = []
        else:
            score = 0.5
            lesson = f"PARTIAL: Task '{trace.task_description[:60]}' had mixed results (score={trace.final_score:.2f})."
            severity = ReflectionSeverity.INFO
            success_factors = []
            failure_cause = None

        return RetrospectiveScore(
            trajectory_id=trace.trace_id,
            numeric_score=score,
            textual_lesson=lesson,
            severity=severity,
            failure_root_cause=failure_cause,
            success_factors=success_factors,
        )

    # ── 反馈与检索 ───────────────────────────────────────────────

    def reflect(
        self,
        trajectory_id: str,
        override_lesson: Optional[str] = None,
    ) -> Optional[RetrospectiveScore]:
        """手动触发事后反思。"""
        with self._lock:
            trace = self._trajectories.get(trajectory_id)
            if not trace:
                return None
            reflection = self._analyze_trajectory(trace)
            if override_lesson:
                reflection.textual_lesson = override_lesson
            self._reflections.append(reflection)
            self._dual_feedback.emit_textual_reflection(
                trajectory_id=trajectory_id,
                lesson=reflection.textual_lesson,
            )
            return reflection

    def retrieve_memories(
        self,
        query_vector: np.ndarray,
        memory_vectors: Dict[str, np.ndarray],
        top_k: int = 10,
    ) -> List[SimUtilScore]:
        """SimUtil-UCB 效用感知检索。"""
        scores = self._retriever.compute_scores(query_vector, memory_vectors)
        for s in scores[:top_k]:
            self._retriever.record_retrieval(s.memory_id)
        return scores[:top_k]

    def should_explore(self, path_signature: str) -> bool:
        return self._tracker.should_explore(path_signature)

    # ── 查询 ──────────────────────────────────────────────────────

    def get_trajectory(self, trace_id: str) -> Optional[TrajectoryTrace]:
        with self._lock:
            return self._trajectories.get(trace_id)

    def get_reflections(
        self,
        min_severity: ReflectionSeverity = ReflectionSeverity.INFO,
        limit: int = 20,
    ) -> List[RetrospectiveScore]:
        with self._lock:
            filtered = [r for r in self._reflections if r.severity.value <= min_severity.value]
            return sorted(filtered, key=lambda r: r.analyzed_at, reverse=True)[:limit]

    # ── 属性 ───────────────────────────────────────────────────────

    @property
    def feedback_loop(self) -> DualFeedbackLoop:
        return self._dual_feedback

    @property
    def retriever(self) -> SimUtilUCBRetriever:
        return self._retriever

    @property
    def tracker(self) -> ExplorationTracker:
        return self._tracker

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "version": self._version,
                "trajectories": len(self._trajectories),
                "reflections": len(self._reflections),
                "feedback_loop": self._dual_feedback.statistics(),
                "retriever": self._retriever.statistics(),
                "exploration_tracker": self._tracker.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """返回模块级统计信息。"""
    return {
        "module": "P14-6 Hindsight Self-Reflection",
        "benchmark": "RetroAgent (Shanghai AI Lab / NUS)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 6,
        "key_metric": "Dual feedback loops / SimUtil-UCB retrieval / Failed-path avoidance",
        "thread_safe": True,
    }
