"""
P20-6: Trajectory-Informed Self-Improvement — IBM Research 2026
================================================================

对标论文：Trajectory-Informed Memory Generation for Self-Improving Agent Systems
(IBM Research 2026, AppWorld benchmark +14.3pp).

设计要点：
  - 轨迹智能抽取器：语义分析推理模式
  - 决策归因分析器：识别失败/恢复/低效的决策点
  - 上下文学习生成器：从成功生成策略提示、从失败生成恢复提示、从低效生成优化提示
  - 多维度自适应记忆检索：基于任务相似度注入相关学习

核心组件：
  - TrajectoryIntelligenceExtractor:  轨迹语义分析
  - DecisionAttributionAnalyzer:      决策归因分析
  - ContextualLearningGenerator:      三类学习指引生成
  - AdaptiveMemoryRetriever:          多维度相似度检索
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class DecisionType(Enum):
    """决策点类型。"""
    FAILURE = "failure"         # 导致失败的决策
    RECOVERY = "recovery"       # 从失败恢复的决策
    INEFFICIENT = "inefficient"  # 低效但成功的决策


class GuidanceType(Enum):
    """学习指引类型。"""
    STRATEGY = "strategy"        # 策略提示（来自成功模式）
    RECOVERY = "recovery"        # 恢复提示（来自失败处理）
    OPTIMIZATION = "optimization"  # 优化提示（来自低效改进）


class ReasoningPattern(Enum):
    """推理模式。"""
    DECOMPOSITION = "decomposition"    # 任务分解
    PLANNING = "planning"              # 规划
    REFLECTION = "reflection"          # 反思
    CORRECTION = "correction"          # 纠错
    OPTIMIZATION = "optimization"      # 优化
    HEURISTIC = "heuristic"            # 启发式


class SimilarityDimension(Enum):
    """相似度维度。"""
    TASK_TYPE = "task_type"            # 任务类型
    DECISION_PATTERN = "decision_pattern"  # 决策模式
    ERROR_PROFILE = "error_profile"    # 错误画像
    TOOL_USAGE = "tool_usage"          # 工具使用
    DOMAIN = "domain"                  # 领域


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ReasoningStep:
    """推理步骤。"""
    step_id: str
    content: str
    pattern: ReasoningPattern
    confidence: float = 0.5
    evidence: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class DecisionAttribution:
    """决策归因结果。"""
    attribution_id: str
    decision_text: str
    decision_type: DecisionType
    root_cause: str = ""
    contributing_factors: List[str] = field(default_factory=list)
    severity: float = 0.0
    recoverable: bool = True
    timestamp: float = field(default_factory=time.time)


@dataclass
class LearningGuidance:
    """学习指引。"""
    guidance_id: str
    guidance_type: GuidanceType
    content: str
    provenance_trajectory_id: str = ""
    applicability_score: float = 0.5
    success_rate_after: float = 0.0
    usage_count: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class SimilarityProfile:
    """多维度相似度画像。"""
    profile_id: str
    dimensions: Dict[SimilarityDimension, float] = field(default_factory=dict)
    overall_similarity: float = 0.0
    top_matches: List[Tuple[str, float]] = field(default_factory=list)


@dataclass
class Trajectory:
    """Agent 执行轨迹。"""
    trajectory_id: str
    task_description: str
    steps: List[ReasoningStep] = field(default_factory=list)
    outcomes: List[Dict[str, Any]] = field(default_factory=list)
    success: bool = False
    total_latency_ms: float = 0.0
    tool_calls: int = 0
    error_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Core Components
# ============================================================================

class TrajectoryIntelligenceExtractor:
    """轨迹智能抽取器。

    语义分析 Agent 推理模式：分解/规划/反思/纠错/优化/启发式。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.trajectories: List[Trajectory] = []
        self.pattern_counter: Dict[ReasoningPattern, int] = defaultdict(int)

    def extract(self, trajectory: Trajectory) -> List[ReasoningStep]:
        """提取轨迹中的推理模式。"""
        with self._lock:
            self.trajectories.append(trajectory)
            steps: List[ReasoningStep] = []

            for step in trajectory.steps:
                # 更新模式计数
                self.pattern_counter[step.pattern] += 1

                # 增强分析
                enhanced = ReasoningStep(
                    step_id=step.step_id,
                    content=step.content,
                    pattern=step.pattern,
                    confidence=self._pattern_confidence(step),
                )
                steps.append(enhanced)

            return steps

    def _pattern_confidence(self, step: ReasoningStep) -> float:
        """基于模式频率的置信度。"""
        total = sum(self.pattern_counter.values()) or 1
        freq = self.pattern_counter.get(step.pattern, 0)
        return min(freq / total * 3, 0.95)

    def dominant_pattern(self) -> ReasoningPattern:
        """主导推理模式。"""
        if not self.pattern_counter:
            return ReasoningPattern.HEURISTIC
        return max(self.pattern_counter, key=self.pattern_counter.get)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_trajectories": len(self.trajectories),
                "pattern_distribution": {k.value: v for k, v in self.pattern_counter.items()},
            }


class DecisionAttributionAnalyzer:
    """决策归因分析器。

    识别失败/恢复/低效的决策点，定位根因。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.attributions: List[DecisionAttribution] = []

    def analyze(self, trajectory: Trajectory) -> List[DecisionAttribution]:
        """分析轨迹中的决策归因。"""
        with self._lock:
            attributions: List[DecisionAttribution] = []

            for i, step in enumerate(trajectory.steps):
                # 失败决策：纠正/反思模式且最终失败
                if step.pattern in (ReasoningPattern.CORRECTION, ReasoningPattern.REFLECTION) and not trajectory.success:
                    attr = DecisionAttribution(
                        attribution_id=str(uuid.uuid4())[:8],
                        decision_text=step.content[:200],
                        decision_type=DecisionType.FAILURE,
                        root_cause=self._infer_root_cause(step, trajectory),
                        severity=0.8,
                    )
                    attributions.append(attr)

                # 恢复决策：纠正后轨迹成功
                elif step.pattern == ReasoningPattern.CORRECTION and trajectory.success and trajectory.error_count > 0:
                    attr = DecisionAttribution(
                        attribution_id=str(uuid.uuid4())[:8],
                        decision_text=step.content[:200],
                        decision_type=DecisionType.RECOVERY,
                        root_cause="Identified error and corrected",
                        severity=0.4,
                    )
                    attributions.append(attr)

                # 低效决策：启发式但高延迟
                elif step.pattern == ReasoningPattern.HEURISTIC and trajectory.total_latency_ms > 5000:
                    attr = DecisionAttribution(
                        attribution_id=str(uuid.uuid4())[:8],
                        decision_text=step.content[:200],
                        decision_type=DecisionType.INEFFICIENT,
                        root_cause="Heuristic approach with high latency",
                        severity=0.3,
                    )
                    attributions.append(attr)

            if not attributions and trajectory.steps:
                # 兜底：至少生成一个归因
                last = trajectory.steps[-1]
                dt = DecisionType.INEFFICIENT if trajectory.success else DecisionType.FAILURE
                attributions.append(DecisionAttribution(
                    attribution_id=str(uuid.uuid4())[:8],
                    decision_text=last.content[:200],
                    decision_type=dt,
                    root_cause="Insufficient evidence for precise attribution",
                ))

            self.attributions.extend(attributions)
            return attributions

    def _infer_root_cause(self, step: ReasoningStep, trajectory: Trajectory) -> str:
        """推断根因。"""
        if step.pattern == ReasoningPattern.CORRECTION:
            return "Previous decision led to error requiring correction"
        elif step.pattern == ReasoningPattern.REFLECTION:
            return "Reflection triggered by suboptimal outcome"
        elif trajectory.error_count > 1:
            return f"Multiple errors ({trajectory.error_count}) during execution"
        return "Unknown root cause"

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = defaultdict(int)
            for a in self.attributions:
                type_counts[a.decision_type.value] += 1
            return {
                "total_attributions": len(self.attributions),
                "by_type": dict(type_counts),
            }


class ContextualLearningGenerator:
    """上下文学习生成器。

    从成功轨迹生成策略提示，从失败生成恢复提示，从低效生成优化提示。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.guidances: List[LearningGuidance] = []

    def generate(self, trajectory: Trajectory,
                 attributions: List[DecisionAttribution]) -> List[LearningGuidance]:
        """基于轨迹和归因生成三类学习指引。"""
        with self._lock:
            guidances: List[LearningGuidance] = []
            tid = trajectory.trajectory_id

            if trajectory.success and trajectory.error_count == 0:
                # 成功轨迹 → 策略提示
                strategy = self._extract_strategy(trajectory)
                guidances.append(LearningGuidance(
                    guidance_id=str(uuid.uuid4())[:8],
                    guidance_type=GuidanceType.STRATEGY,
                    content=strategy,
                    provenance_trajectory_id=tid,
                    applicability_score=0.85,
                    success_rate_after=1.0,
                ))

            for attr in attributions:
                if attr.decision_type == DecisionType.FAILURE:
                    recovery = self._generate_recovery_tip(attr, trajectory)
                    guidances.append(LearningGuidance(
                        guidance_id=str(uuid.uuid4())[:8],
                        guidance_type=GuidanceType.RECOVERY,
                        content=recovery,
                        provenance_trajectory_id=tid,
                        applicability_score=0.7,
                    ))
                elif attr.decision_type == DecisionType.INEFFICIENT:
                    optimization = self._generate_optimization_tip(attr, trajectory)
                    guidances.append(LearningGuidance(
                        guidance_id=str(uuid.uuid4())[:8],
                        guidance_type=GuidanceType.OPTIMIZATION,
                        content=optimization,
                        provenance_trajectory_id=tid,
                        applicability_score=0.6,
                    ))

            self.guidances.extend(guidances)
            return guidances

    def _extract_strategy(self, trajectory: Trajectory) -> str:
        """从成功轨迹提取策略。"""
        steps_summary = "; ".join(
            f"[{s.pattern.value}] {s.content[:50]}"
            for s in trajectory.steps[:3]
        )
        return f"Strategy for '{trajectory.task_description[:80]}': {steps_summary}. Pattern: {trajectory.steps[0].pattern.value if trajectory.steps else 'unknown'}, success achieved in {trajectory.total_latency_ms:.0f}ms."

    def _generate_recovery_tip(self, attr: DecisionAttribution, trajectory: Trajectory) -> str:
        """生成恢复提示。"""
        return (f"Recovery tip: When encountering '{attr.root_cause[:100]}' "
                f"in tasks similar to '{trajectory.task_description[:60]}', "
                f"consider alternative approach. Error pattern: {trajectory.error_count} errors.")

    def _generate_optimization_tip(self, attr: DecisionAttribution, trajectory: Trajectory) -> str:
        """生成优化提示。"""
        return (f"Optimization: Task '{trajectory.task_description[:60]}' took {trajectory.total_latency_ms:.0f}ms "
                f"with {trajectory.tool_calls} tool calls. Consider reducing tool calls or caching results.")

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = defaultdict(int)
            for g in self.guidances:
                type_counts[g.guidance_type.value] += 1
            return {
                "total_guidances": len(self.guidances),
                "by_type": dict(type_counts),
            }


class AdaptiveMemoryRetriever:
    """多维度自适应记忆检索。

    基于任务相似度从多维度检索并注入相关学习指引。
    """

    def __init__(self, similarity_threshold: float = 0.3):
        self._lock = threading.RLock()
        self.similarity_threshold = similarity_threshold
        self.guidances: List[LearningGuidance] = []

    def index(self, guidances: List[LearningGuidance]):
        """索引学习指引。"""
        with self._lock:
            self.guidances.extend(guidances)

    def retrieve(self, task_description: str, top_k: int = 5) -> List[LearningGuidance]:
        """多维度相似度检索。"""
        with self._lock:
            profile = self._compute_similarity(task_description)
            scored = []

            for g in self.guidances:
                score = profile.overall_similarity * g.applicability_score
                if score >= self.similarity_threshold:
                    scored.append((score, g))

            scored.sort(key=lambda x: x[0], reverse=True)
            return [g for _, g in scored[:top_k]]

    def _compute_similarity(self, task: str) -> SimilarityProfile:
        """多维度相似度计算。"""
        task_lower = task.lower()
        dims: Dict[SimilarityDimension, float] = {}

        # 任务类型相似度（基于关键词）
        task_keywords = {"deploy": 0.8, "search": 0.6, "analyze": 0.7, "create": 0.7,
                         "delete": 0.8, "update": 0.6, "list": 0.4}
        dims[SimilarityDimension.TASK_TYPE] = max(
            (v for k, v in task_keywords.items() if k in task_lower), default=0.3)

        # 错误画像相似度
        error_keywords = ["error", "fail", "timeout", "crash"]
        dims[SimilarityDimension.ERROR_PROFILE] = (
            0.8 if any(k in task_lower for k in error_keywords) else 0.2)

        # 决策模式相似度
        dims[SimilarityDimension.DECISION_PATTERN] = 0.5

        # 工具使用相似度
        tool_keywords = ["tool", "api", "database", "search", "code"]
        dims[SimilarityDimension.TOOL_USAGE] = (
            0.7 if any(k in task_lower for k in tool_keywords) else 0.3)

        overall = sum(dims.values()) / max(len(dims), 1)

        return SimilarityProfile(
            profile_id=str(uuid.uuid4())[:8],
            dimensions=dims,
            overall_similarity=round(overall, 4),
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_indexed": len(self.guidances),
                "threshold": self.similarity_threshold,
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P20-6 Trajectory-Informed Self-Improvement",
        "benchmark": "IBM Research 2026 — Trajectory Intelligence + Decision Attribution + Contextual Learning",
        "classes": 4,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "Extract Patterns→Attribute Decisions→Generate 3-Type Guidance→Adaptive Retrieve",
        "key_metric": "Up to +14.3pp on AppWorld via trajectory-informed self-improvement (IBM 2026)",
        "thread_safe": True,
    }
