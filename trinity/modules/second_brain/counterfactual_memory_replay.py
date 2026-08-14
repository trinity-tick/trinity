"""
P22-6: Counterfactual Memory Replay — "What-If" Trajectory Simulation
======================================================================

对标方案：Counterfactual Trajectory Analysis & Lesson Extraction (2026).

设计要点：
  - 失败轨迹的反事实推演（"如果当时做了 X 而不是 Y"）
  - 多路径模拟引擎（并行采样 + 对比学习）
  - 反事实经验存储与索引
  - 从反事实中提取通用教训（跨场景可迁移）

核心组件：
  - CounterfactualReplayEngine:  反事实回放总控
  - CounterfactualSimulator:     多路径模拟引擎
  - LessonExtractor:             通用教训提取器
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

class CounterfactualMode(Enum):
    """反事实推演模式。"""
    SINGLE_BRANCH = "single_branch"      # 单分支推演
    MULTI_BRANCH = "multi_branch"        # 多分支并行推演
    ENUMERATIVE = "enumerative"          # 穷举所有替代决策
    SAMPLING = "sampling"                # 随机采样替代


class LessonType(Enum):
    """教训类型。"""
    PRECONDITION = "precondition"         # 前置条件不足
    ACTION_SUBSTITUTION = "action_substitution"  # 应替换动作
    TIMING = "timing"                     # 时机问题
    RESOURCE = "resource"                 # 资源不足
    SEQUENCE = "sequence"                 # 步骤顺序
    GENERAL = "general"                   # 通用教训


class SimulationStatus(Enum):
    """模拟状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ContrastMetric(Enum):
    """对比指标。"""
    OUTCOME_DELTA = "outcome_delta"
    SIMILARITY = "similarity"
    DIVERGENCE = "divergence"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TraceStep:
    """轨迹步骤。"""
    step_index: int
    action: str
    observation: str = ""
    decision_rationale: str = ""
    outcome_contribution: float = 0.0
    alternatives: List[str] = field(default_factory=list)


@dataclass
class FailedTrace:
    """失败轨迹。"""
    trace_id: str
    steps: List[TraceStep] = field(default_factory=list)
    final_outcome: float = 0.0
    failure_point: int = -1          # 失败发生的步骤索引
    failure_description: str = ""
    context: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CounterfactualBranch:
    """反事实分支（一条 if-then 路径）。"""
    branch_id: str
    original_trace_id: str
    intervention_step: int            # 干预点
    original_action: str
    counterfactual_action: str
    simulated_steps: List[TraceStep] = field(default_factory=list)
    simulated_outcome: float = 0.0
    outcome_delta: float = 0.0        # 相对于原始结果的变化
    status: SimulationStatus = SimulationStatus.PENDING
    probability_of_success: float = 0.0


@dataclass
class SimulationResult:
    """多路径模拟结果。"""
    result_id: str
    original_trace_id: str
    branches: List[CounterfactualBranch] = field(default_factory=list)
    best_branch: Optional[CounterfactualBranch] = None
    best_delta: float = 0.0
    lessons: List[str] = field(default_factory=list)


@dataclass
class ExtractedLesson:
    """提取的通用教训。"""
    lesson_id: str
    lesson_type: LessonType
    content: str
    source_traces: List[str] = field(default_factory=list)
    confidence: float = 0.0          # 跨场景置信度
    reusability_score: float = 0.0    # 可迁移性分数
    application_count: int = 0
    embedding: Optional[List[float]] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class LessonIndex:
    """教训索引。"""
    lessons_by_type: Dict[LessonType, List[str]] = field(default_factory=lambda: defaultdict(list))
    lessons_by_pattern: Dict[str, List[str]] = field(default_factory=lambda: defaultdict(list))
    total: int = 0


# ============================================================================
# Constants
# ============================================================================

DEFAULT_BRANCH_LIMIT: int = 5
DEFAULT_SIMULATION_DEPTH: int = 10
LESSON_CONFIDENCE_THRESHOLD: float = 0.3
LESSON_REUSABILITY_THRESHOLD: float = 0.2


# ============================================================================
# Core Components
# ============================================================================

class CounterfactualSimulator:
    """多路径反事实模拟引擎。

    对失败轨迹的干预点，并行采样多个替代行动路径，
    模拟每条路径的前向推演结果。
    """

    def __init__(self, branch_limit: int = DEFAULT_BRANCH_LIMIT,
                 max_depth: int = DEFAULT_SIMULATION_DEPTH):
        self._lock = threading.RLock()
        self.branch_limit = branch_limit
        self.max_depth = max_depth
        self.results: List[SimulationResult] = []

    def simulate(self, failed_trace: FailedTrace,
                 intervention_step: int,
                 alternative_actions: List[str]) -> SimulationResult:
        """多路径并行模拟。"""
        with self._lock:
            result = SimulationResult(
                result_id=str(uuid.uuid4())[:8],
                original_trace_id=failed_trace.trace_id,
            )

            # 限制分支数
            actions = alternative_actions[:self.branch_limit]

            # 并行模拟各分支
            for alt_action in actions:
                branch = CounterfactualBranch(
                    branch_id=str(uuid.uuid4())[:8],
                    original_trace_id=failed_trace.trace_id,
                    intervention_step=intervention_step,
                    original_action=(
                        failed_trace.steps[intervention_step].action
                        if intervention_step < len(failed_trace.steps) else ""
                    ),
                    counterfactual_action=alt_action,
                    status=SimulationStatus.RUNNING,
                )

                # 模拟前向推演
                self._forward_simulate(branch, failed_trace, intervention_step)
                branch.status = SimulationStatus.COMPLETED

                result.branches.append(branch)

            # 找最佳分支
            best = max(result.branches, key=lambda b: b.outcome_delta, default=None)
            if best:
                result.best_branch = best
                result.best_delta = best.outcome_delta

            self.results.append(result)
            return result

    def _forward_simulate(self, branch: CounterfactualBranch,
                          failed_trace: FailedTrace, intervention_point: int):
        """前向推演模拟（简化为概率模型）。"""
        # 干预前：复制原始轨迹
        steps_before = failed_trace.steps[:intervention_point]

        # 干预点：替换行动
        modified_step = TraceStep(
            step_index=intervention_point,
            action=branch.counterfactual_action,
            observation=f"Counterfactual: did '{branch.counterfactual_action}' "
                        f"instead of '{branch.original_action}'",
        )

        # 干预后：随机模拟
        steps_after = []
        cumulative_outcome = 0.5  # 中性起点
        for i in range(intervention_point + 1, min(intervention_point + self.max_depth - 1, len(failed_trace.steps) + 3)):
            # 基于原始步骤的变化产生随机结果
            success_bias = 0.1 * (i - intervention_point)  # 越远离干预点，效果递减
            outcome = cumulative_outcome + random.uniform(-0.1, 0.3) + success_bias
            outcome = max(0.0, min(1.0, outcome))
            cumulative_outcome = outcome

            steps_after.append(TraceStep(
                step_index=i,
                action=f"simulated_action_{i}",
                observation=f"Simulated outcome at step {i}",
                outcome_contribution=outcome,
            ))

        branch.simulated_steps = steps_before + [modified_step] + steps_after
        branch.simulated_outcome = cumulative_outcome
        branch.outcome_delta = round(
            cumulative_outcome - failed_trace.final_outcome, 4)
        branch.probability_of_success = min(
            cumulative_outcome / max(failed_trace.final_outcome, 0.01), 1.0)

    def contrast(self, branches: List[CounterfactualBranch]) -> Dict[str, float]:
        """对比学习：找出最佳策略差异。"""
        if not branches:
            return {}
        sorted_branches = sorted(branches, key=lambda b: b.outcome_delta, reverse=True)
        best = sorted_branches[0]
        worst = sorted_branches[-1] if len(sorted_branches) > 1 else best
        return {
            "best_delta": best.outcome_delta,
            "worst_delta": worst.outcome_delta,
            "gap": round(best.outcome_delta - worst.outcome_delta, 4),
            "avg_delta": round(
                sum(b.outcome_delta for b in branches) / len(branches), 4),
        }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_simulations": len(self.results),
                "total_branches": sum(len(r.branches) for r in self.results),
                "branch_limit": self.branch_limit,
                "max_depth": self.max_depth,
            }


class LessonExtractor:
    """通用教训提取器。

    从反事实模拟结果中提取跨场景可迁移的教训。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.lessons: Dict[str, ExtractedLesson] = {}
        self.index = LessonIndex()
        self._pattern_cache: Dict[str, float] = {}

    def extract(self, sim_result: SimulationResult,
                failed_trace: FailedTrace) -> List[ExtractedLesson]:
        """从模拟结果提取教训。"""
        with self._lock:
            lessons: List[ExtractedLesson] = []

            # 最佳分支 → 行动替换教训
            if sim_result.best_branch:
                orig = sim_result.best_branch.original_action
                alt = sim_result.best_branch.counterfactual_action
                if orig and alt and orig != alt:
                    content = (f"When encountering '{failed_trace.failure_description}', "
                              f"doing '{alt}' instead of '{orig}' improves outcome "
                              f"by {sim_result.best_delta:.2%}")
                    lesson = self._create_lesson(
                        LessonType.ACTION_SUBSTITUTION, content,
                        [failed_trace.trace_id],
                        confidence=min(0.5 + sim_result.best_delta, 1.0),
                    )
                    lessons.append(lesson)

            # 前置条件检测
            if failed_trace.failure_point >= 0:
                content = (f"Failure at step {failed_trace.failure_point}: "
                          f"precondition may not be satisfied — "
                          f"ensure prerequisites before this action type")
                lesson = self._create_lesson(
                    LessonType.PRECONDITION, content,
                    [failed_trace.trace_id],
                    confidence=0.6,
                )
                lessons.append(lesson)

            # 时机教训（如果多个分支都改善 → 时机问题）
            if len(sim_result.branches) >= 2:
                high_delta_branches = [b for b in sim_result.branches if b.outcome_delta > 0.2]
                if len(high_delta_branches) >= 2:
                    content = ("Multiple alternative actions improve the outcome, "
                              "suggesting the original action was taken at the wrong time")
                    lesson = self._create_lesson(
                        LessonType.TIMING, content,
                        [failed_trace.trace_id],
                        confidence=0.5,
                    )
                    lessons.append(lesson)

            return lessons

    def _create_lesson(self, lesson_type: LessonType, content: str,
                       source_traces: List[str], confidence: float) -> ExtractedLesson:
        """创建并索引教训。"""
        lesson = ExtractedLesson(
            lesson_id=str(uuid.uuid4())[:8],
            lesson_type=lesson_type,
            content=content,
            source_traces=source_traces,
            confidence=round(confidence, 4),
            reusability_score=round(confidence * 0.8, 4),
        )
        self.lessons[lesson.lesson_id] = lesson
        self.index.lessons_by_type[lesson_type].append(lesson.lesson_id)
        self.index.total += 1
        return lesson

    def get_lessons_by_type(self, lesson_type: LessonType) -> List[ExtractedLesson]:
        """按类型检索教训。"""
        ids = self.index.lessons_by_type.get(lesson_type, [])
        return [self.lessons[lid] for lid in ids if lid in self.lessons]

    def apply_lesson(self, lesson_id: str):
        """标记教训被应用（增加可迁移性分数）。"""
        with self._lock:
            lesson = self.lessons.get(lesson_id)
            if lesson:
                lesson.application_count += 1
                lesson.reusability_score = min(
                    1.0, lesson.reusability_score + 0.05)

    def top_lessons(self, k: int = 5) -> List[ExtractedLesson]:
        """Top-k 高置信度教训。"""
        sorted_lessons = sorted(
            self.lessons.values(),
            key=lambda l: (l.confidence, l.reusability_score),
            reverse=True,
        )
        return sorted_lessons[:k]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_lessons": self.index.total,
                "by_type": {t.value: len(ids) for t, ids in self.index.lessons_by_type.items()},
                "avg_confidence": round(
                    sum(l.confidence for l in self.lessons.values()) /
                    max(len(self.lessons), 1), 4),
                "top_lesson": self.top_lessons(1)[0].content[:80] if self.lessons else "",
            }


class CounterfactualReplayEngine:
    """反事实记忆回放总控。

    集成失败轨迹注册、多路径模拟、教训提取的一体化引擎。
    """

    def __init__(self, branch_limit: int = DEFAULT_BRANCH_LIMIT,
                 max_depth: int = DEFAULT_SIMULATION_DEPTH):
        self._lock = threading.RLock()
        self.failed_traces: Dict[str, FailedTrace] = {}
        self.simulator = CounterfactualSimulator(branch_limit, max_depth)
        self.extractor = LessonExtractor()
        self.replay_history: List[SimulationResult] = []

    def register_failure(self, trace: FailedTrace):
        """注册失败轨迹。"""
        with self._lock:
            self.failed_traces[trace.trace_id] = trace

    def replay(self, trace_id: str, intervention_step: int,
               alternative_actions: List[str]) -> SimulationResult:
        """回放失败轨迹的反事实。"""
        with self._lock:
            trace = self.failed_traces.get(trace_id)
            if not trace:
                raise ValueError(f"Trace {trace_id} not found")

            # 多路径模拟
            sim_result = self.simulator.simulate(trace, intervention_step, alternative_actions)

            # 提取教训
            lessons = self.extractor.extract(sim_result, trace)
            sim_result.lessons = [l.content for l in lessons]

            self.replay_history.append(sim_result)
            return sim_result

    def get_lessons_for_trace(self, trace_id: str) -> List[ExtractedLesson]:
        """获取某轨迹的所有教训。"""
        lesson_ids: Set[str] = set()
        for r in self.replay_history:
            if r.original_trace_id == trace_id:
                for l in self.extractor.lessons.values():
                    if trace_id in l.source_traces:
                        lesson_ids.add(l.lesson_id)
        return [self.extractor.lessons[lid] for lid in lesson_ids]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_failures": len(self.failed_traces),
                "total_replays": len(self.replay_history),
                "simulator": self.simulator.statistics(),
                "extractor": self.extractor.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P22-6 Counterfactual Memory Replay",
        "benchmark": "What-If Trajectory Simulation + Contrastive Learning + Lesson Extraction (2026)",
        "classes": 3,
        "enums": 4,
        "dataclasses": 6,
        "key_pattern": "FailedTrace→MultiBranch→ForwardSim→Contrast→ExtractLessons→ReusableKnowledge",
        "key_metric": "Multi-path counterfactual branching + cross-task lesson reusability scoring",
        "thread_safe": True,
    }
