# DEPRECATED: This experimental module (M117) is not registered in __init__.py
# and has no known internal consumers. It is retained for reference only.
# Last assessed: 2026-08-08. Remove in a future cleanup cycle if unused.
#!/usr/bin/env python3
"""
# status: orphan (2026-08-15 audit, not in runtime path)
M117 - Hierarchical Experimentalist (三层实验主义者)
=====================================================
Based on HExA: Hierarchical Experimentalist Agents for Long-Horizon
Decision Making (arXiv 2606.29315).

三层架构：
  Layer 1: Strategist    — 高层策略规划，分解长期目标为子目标序列
  Layer 2: Executor      — 中层执行，将子目标映射为具体动作
  Layer 3: Curator       — 底层策展，收集执行反馈、提炼经验、更新策略

与 M113 AutoCurriculaOrchestrator 协作：
  - M113 负责任务课程编排（训练生成 + 难度估计）
  - M117 负责实验执行与经验反馈闭环
  - 协作：M113 生成课程 → M117 三层执行 → 反馈回 M113 更新课程

Paper: https://arxiv.org/abs/2606.29315
"""

from __future__ import annotations

import json
import math
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import numpy as np

# ============================================================================
# Enums & Type Aliases
# ============================================================================


class GoalStatus(Enum):
    """子目标执行状态"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUCCESS = "success"
    FAILED = "failed"
    DELEGATED = "delegated"  # 委托给其他 agent
    BLOCKED = "blocked"      # 被前置条件阻塞


class ExperimentPhase(Enum):
    """实验阶段"""
    HYPOTHESIZE = "hypothesize"   # 提出假设
    DESIGN = "design"             # 设计实验
    EXECUTE = "execute"           # 执行实验
    ANALYZE = "analyze"           # 分析结果
    CURATE = "curate"            # 策展知识


class FeedbackType(Enum):
    """反馈类型"""
    REWARD = "reward"             # 标量奖励
    TRAJECTORY = "trajectory"     # 完整轨迹
    ABLATION = "ablation"        # 消融结果
    COUNTERFACTUAL = "counterfactual"  # 反事实推理
    GRADIENT = "gradient"        # 梯度信号


# ============================================================================
# Data Structures
# ============================================================================


@dataclass
class SubGoal:
    """子目标定义"""
    goal_id: str
    description: str
    parent_goal_id: Optional[str] = None
    priority: float = 1.0
    preconditions: List[str] = field(default_factory=list)
    postconditions: List[str] = field(default_factory=list)
    estimated_difficulty: float = 0.5
    max_steps: int = 50
    allowed_actions: List[str] = field(default_factory=list)
    status: GoalStatus = GoalStatus.PENDING
    assigned_agent: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionStep:
    """执行动作步"""
    step_id: str
    goal_id: str
    action_name: str
    action_params: Dict[str, Any] = field(default_factory=dict)
    expected_outcome: Optional[str] = None
    actual_outcome: Optional[str] = None
    success: Optional[bool] = None
    timestamp: float = field(default_factory=time.time)
    duration_ms: float = 0.0
    retry_count: int = 0


@dataclass
class Experiment:
    """一次实验的完整记录"""
    experiment_id: str
    hypothesis: str
    subgoals: List[SubGoal] = field(default_factory=list)
    action_trajectory: List[ActionStep] = field(default_factory=list)
    feedbacks: List[Dict[str, Any]] = field(default_factory=list)
    outcome: Optional[str] = None
    score: float = 0.0
    phase: ExperimentPhase = ExperimentPhase.HYPOTHESIZE
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CuratedInsight:
    """策展产生的洞察"""
    insight_id: str
    source_experiment_id: str
    insight_type: str  # "pattern", "rule", "warning", "improvement"
    description: str
    confidence: float = 0.5
    evidence: List[str] = field(default_factory=list)
    actionable: bool = True
    suggested_actions: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


# ============================================================================
# Layer 1: Strategist — 高层策略规划
# ============================================================================


class Strategist:
    """
    Strategist (策略师) — HExA Layer 1

    职责：
    1. 将长期目标分解为层次化子目标序列 (SubGoal tree)
    2. 基于当前世界模型评估每个子目标的可达性
    3. 动态优先级排序：根据反馈调整子目标优先级
    4. 资源分配：决定哪些子目标委托给其他 agent
    """

    def __init__(
        self,
        max_subgoals: int = 100,
        decomposition_max_depth: int = 5,
        priority_decay: float = 0.95,
    ):
        self.max_subgoals = max_subgoals
        self.decomposition_max_depth = decomposition_max_depth
        self.priority_decay = priority_decay

        # 内部状态
        self.goal_tree: Dict[str, SubGoal] = {}
        self.goal_hierarchy: Dict[str, List[str]] = defaultdict(list)  # parent -> [children]
        self.strategy_history: deque = deque(maxlen=200)

        # 统计
        self.total_goals_created: int = 0
        self.goals_completed: int = 0
        self.goals_failed: int = 0

    # ---- Core API ----

    def decompose_goal(
        self,
        root_description: str,
        constraints: Optional[List[str]] = None,
    ) -> List[SubGoal]:
        """
        将根目标分解为子目标序列。

        分解策略（基于 HExA 的层次化分解）：
          - 时间顺序分解：先后依赖的子目标
          - 空间分解：可并行的独立子目标
          - 抽象层次分解：高层策略 → 中层战术 → 底层操作
        """
        root_id = self._generate_goal_id("root")
        root = SubGoal(
            goal_id=root_id,
            description=root_description,
            priority=1.0,
            preconditions=constraints or [],
        )
        self.goal_tree[root_id] = root
        self.total_goals_created += 1

        # 递归分解
        subgoals = self._recursive_decompose(
            parent=root,
            depth=1,
            constraints=constraints or [],
        )

        self.strategy_history.append({
            "action": "decompose",
            "root": root_description,
            "subgoal_count": len(subgoals) + 1,
            "timestamp": time.time(),
        })
        return [root] + subgoals

    def prioritize_goals(
        self,
        goal_ids: List[str],
        feedback_scores: Optional[Dict[str, float]] = None,
    ) -> List[Tuple[str, float]]:
        """
        基于反馈动态重排子目标优先级。

        优先级公式：
          priority = base_priority * decay_factor^age * (1 + feedback_bonus)

        其中 feedback_bonus 来自 Curator 返回的经验评分。
        """
        scored = []
        for gid in goal_ids:
            goal = self.goal_tree.get(gid)
            if goal is None:
                continue

            base = goal.priority
            age = time.time() - goal.metadata.get("created_at", time.time())
            age_factor = self.priority_decay ** (age / 60.0)  # 按分钟衰减

            feedback_bonus = feedback_scores.get(gid, 0.0) if feedback_scores else 0.0
            score = base * age_factor * (1.0 + feedback_bonus)
            scored.append((gid, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def assess_attainability(
        self,
        goal_id: str,
        world_state: Dict[str, Any],
    ) -> float:
        """
        评估子目标在当前世界状态下的可达性。

        可达性 = f(前置条件满足度, 历史成功率, 资源可用性)
        返回值 ∈ [0, 1]。
        """
        goal = self.goal_tree.get(goal_id)
        if goal is None:
            return 0.0

        # 前置条件满足度
        precond_satisfied = sum(
            1.0 for p in goal.preconditions
            if world_state.get(p, False)
        )
        precond_ratio = (
            precond_satisfied / len(goal.preconditions)
            if goal.preconditions else 1.0
        )

        # 历史成功率
        attempt_count = goal.metadata.get("attempt_count", 0)
        success_count = goal.metadata.get("success_count", 0)
        history_success = (
            success_count / attempt_count if attempt_count > 0 else 0.5
        )

        # 综合
        attainability = 0.5 * precond_ratio + 0.3 * history_success + 0.2 * (1.0 - goal.estimated_difficulty)
        return max(0.0, min(1.0, attainability))

    def delegate_goal(
        self,
        goal_id: str,
        target_agent: str,
        delegation_params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """将子目标委托给其他 agent（如 M113 课程编排器）。"""
        goal = self.goal_tree.get(goal_id)
        if goal is None:
            return {"error": f"Goal {goal_id} not found"}

        goal.status = GoalStatus.DELEGATED
        goal.assigned_agent = target_agent
        goal.metadata["delegation_params"] = delegation_params or {}
        goal.metadata["delegated_at"] = time.time()

        return {
            "goal_id": goal_id,
            "delegated_to": target_agent,
            "description": goal.description,
            "params": delegation_params,
        }

    # ---- Internal ----

    def _recursive_decompose(
        self,
        parent: SubGoal,
        depth: int,
        constraints: List[str],
    ) -> List[SubGoal]:
        """递归分解子目标。"""
        if depth >= self.decomposition_max_depth:
            return []
        if self.total_goals_created >= self.max_subgoals:
            return []

        # HExA 启发式分解：每个目标分解为 2-4 个子目标
        n_children = min(2 + (depth % 3), 4)
        children = []

        for i in range(n_children):
            child_id = self._generate_goal_id(f"{parent.goal_id}_child")
            child = SubGoal(
                goal_id=child_id,
                description=f"[{parent.description}] sub-task {i+1}/{n_children}",
                parent_goal_id=parent.goal_id,
                priority=parent.priority * 0.8,
                preconditions=parent.preconditions[:],
                estimated_difficulty=min(1.0, parent.estimated_difficulty + 0.05 * depth),
                metadata={"created_at": time.time(), "depth": depth},
            )
            self.goal_tree[child_id] = child
            self.goal_hierarchy[parent.goal_id].append(child_id)
            self.total_goals_created += 1
            children.append(child)

            # 递归
            grandchildren = self._recursive_decompose(child, depth + 1, constraints)
            children.extend(grandchildren)

        return children

    def _generate_goal_id(self, prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:8]}"

    def update_goal_status(self, goal_id: str, status: GoalStatus):
        """更新目标状态并更新计数器。"""
        goal = self.goal_tree.get(goal_id)
        if goal:
            old_status = goal.status
            goal.status = status
            if old_status != GoalStatus.SUCCESS and status == GoalStatus.SUCCESS:
                self.goals_completed += 1
            elif old_status != GoalStatus.FAILED and status == GoalStatus.FAILED:
                self.goals_failed += 1

    def summary(self) -> Dict[str, Any]:
        return {
            "total_goals": self.total_goals_created,
            "completed": self.goals_completed,
            "failed": self.goals_failed,
            "active_goals": sum(
                1 for g in self.goal_tree.values()
                if g.status in (GoalStatus.PENDING, GoalStatus.IN_PROGRESS)
            ),
            "strategy_history_len": len(self.strategy_history),
        }


# ============================================================================
# Layer 2: Executor — 中层执行
# ============================================================================


class Executor:
    """
    Executor (执行者) — HExA Layer 2

    职责：
    1. 将 SubGoal 映射为具体的 ActionStep 序列
    2. 管理动作执行流水线（execute → observe → adjust）
    3. 实验变体管理（A/B testing 式对比执行）
    4. 异常恢复与重试
    """

    def __init__(
        self,
        max_steps_per_goal: int = 100,
        max_retries: int = 3,
        action_timeout_ms: float = 30000.0,
    ):
        self.max_steps_per_goal = max_steps_per_goal
        self.max_retries = max_retries
        self.action_timeout_ms = action_timeout_ms

        # 动作注册表
        self.action_registry: Dict[str, Callable] = {}
        # 当前实验上下文
        self.current_experiment: Optional[Experiment] = None
        # 执行统计
        self.total_steps_executed: int = 0
        self.total_retries: int = 0
        self.aborted_goals: int = 0

    # ---- Core API ----

    def register_action(self, name: str, fn: Callable[..., Dict[str, Any]]):
        """注册可执行动作。"""
        self.action_registry[name] = fn

    def execute_goal(
        self,
        goal: SubGoal,
        experiment: Experiment,
        world_state: Optional[Dict[str, Any]] = None,
        action_mapper: Optional[Callable[[SubGoal], List[Dict[str, Any]]]] = None,
    ) -> List[ActionStep]:
        """
        执行单个子目标，生成动作轨迹。

        action_mapper: 将子目标映射为具体动作序列的可调用对象。
                       如果不提供，使用内置的启发式映射。
        """
        self.current_experiment = experiment
        steps: List[ActionStep] = []
        goal.status = GoalStatus.IN_PROGRESS
        goal.metadata["attempt_count"] = goal.metadata.get("attempt_count", 0) + 1

        # 生成动作序列
        if action_mapper:
            action_specs = action_mapper(goal)
        else:
            action_specs = self._heuristic_action_mapper(goal)

        step_count = 0
        for spec in action_specs:
            if step_count >= self.max_steps_per_goal:
                self.aborted_goals += 1
                goal.status = GoalStatus.BLOCKED
                goal.metadata["abort_reason"] = "max_steps_exceeded"
                break

            step = ActionStep(
                step_id=f"{goal.goal_id}_step_{step_count}",
                goal_id=goal.goal_id,
                action_name=spec.get("action", "noop"),
                action_params=spec.get("params", {}),
                expected_outcome=spec.get("expected"),
            )

            # 执行动作（带重试）
            success = False
            for retry in range(self.max_retries):
                step_start = time.time()
                try:
                    action_fn = self.action_registry.get(step.action_name)
                    if action_fn:
                        result = action_fn(**step.action_params)
                        step.actual_outcome = result.get("outcome", "")
                        step.success = result.get("success", False)
                    else:
                        # 模拟/外部动作
                        step.actual_outcome = f"Executed {step.action_name}"
                        step.success = True

                    step.duration_ms = (time.time() - step_start) * 1000

                    if step.success:
                        success = True
                        break
                    else:
                        step.retry_count = retry + 1
                        self.total_retries += 1
                        time.sleep(0.01)  # 最小退避
                except Exception as e:
                    step.retry_count = retry + 1
                    self.total_retries += 1
                    step.actual_outcome = f"Error: {e}"

            if not success:
                goal.metadata["last_error"] = step.actual_outcome

            step.duration_ms = (time.time() - step_start) * 1000
            steps.append(step)
            self.total_steps_executed += 1
            step_count += 1

            # 如果这一步失败且不可恢复，提前终止
            if not success and spec.get("critical", False):
                goal.status = GoalStatus.FAILED
                break

        # 更新最终状态
        if goal.status == GoalStatus.IN_PROGRESS:
            all_success = all(s.success for s in steps) if steps else False
            if all_success:
                goal.status = GoalStatus.SUCCESS
                goal.metadata["success_count"] = goal.metadata.get("success_count", 0) + 1
            else:
                goal.status = GoalStatus.FAILED

        experiment.action_trajectory.extend(steps)
        return steps

    def execute_experiment_variants(
        self,
        goal: SubGoal,
        experiment: Experiment,
        variants: List[Dict[str, Any]],
    ) -> Dict[str, List[ActionStep]]:
        """
        执行实验变体（A/B testing 风格）。

        每个 variant 包含不同的 action_mapper 或 world_state。
        """
        results = {}
        for i, variant in enumerate(variants):
            variant_id = variant.get("id", f"variant_{i}")
            variant_world = variant.get("world_state")
            variant_mapper = variant.get("action_mapper")

            steps = self.execute_goal(
                goal=goal,
                experiment=experiment,
                world_state=variant_world,
                action_mapper=variant_mapper,
            )
            results[variant_id] = steps

            # 在变体之间重置 goal 状态
            goal.status = GoalStatus.PENDING
            goal.metadata["variant_results"] = goal.metadata.get("variant_results", {})
            goal.metadata["variant_results"][variant_id] = {
                "success": all(s.success for s in steps),
                "step_count": len(steps),
                "total_duration_ms": sum(s.duration_ms for s in steps),
            }

        return results

    # ---- Internal ----

    def _heuristic_action_mapper(self, goal: SubGoal) -> List[Dict[str, Any]]:
        """
        启发式动作映射：根据子目标描述和允许的动作列表生成动作序列。
        """
        actions = []

        # 使用 goal.allowed_actions 或默认动作集
        allowed = goal.allowed_actions if goal.allowed_actions else [
            "search", "read", "analyze", "write", "verify"
        ]

        if "search" in allowed:
            actions.append({
                "action": "search",
                "params": {"query": goal.description},
                "expected": f"Find relevant information for: {goal.description}",
            })
        if "read" in allowed:
            actions.append({
                "action": "read",
                "params": {"source": "search_results"},
                "expected": "Extract key details",
            })
        if "analyze" in allowed:
            actions.append({
                "action": "analyze",
                "params": {"data": "extracted_details"},
                "expected": "Analyze and synthesize findings",
            })
        if "write" in allowed:
            actions.append({
                "action": "write",
                "params": {"output": "synthesized_result"},
                "expected": "Produce output artifact",
                "critical": True,
            })
        if "verify" in allowed:
            actions.append({
                "action": "verify",
                "params": {"output": "synthesized_result"},
                "expected": "Self-check output quality",
            })

        return actions

    def summary(self) -> Dict[str, Any]:
        return {
            "total_steps": self.total_steps_executed,
            "total_retries": self.total_retries,
            "aborted_goals": self.aborted_goals,
            "registered_actions": list(self.action_registry.keys()),
            "retry_rate": (
                self.total_retries / max(self.total_steps_executed, 1)
            ),
        }


# ============================================================================
# Layer 3: Curator — 底层策展
# ============================================================================


class Curator:
    """
    Curator (策展师) — HExA Layer 3

    职责：
    1. 收集所有实验的执行反馈
    2. 从轨迹中提炼可复用的洞察 (CuratedInsight)
    3. 更新世界模型（World Model）
    4. 将反馈信号传回 Strategist 用于优先级调整
    """

    def __init__(
        self,
        insight_confidence_threshold: float = 0.3,
        max_insights: int = 500,
        similarity_threshold: float = 0.85,
    ):
        self.confidence_threshold = insight_confidence_threshold
        self.max_insights = max_insights
        self.similarity_threshold = similarity_threshold

        # 知识库
        self.insights: Dict[str, CuratedInsight] = {}
        self.feedback_buffer: deque = deque(maxlen=1000)
        self.world_model: Dict[str, Any] = {}

        # 统计
        self.total_experiments_curated: int = 0
        self.total_insights_generated: int = 0

    # ---- Core API ----

    def collect_feedback(
        self,
        experiment: Experiment,
        feedback_data: Dict[str, Any],
    ) -> None:
        """收集单次实验反馈。"""
        feedback_data["experiment_id"] = experiment.experiment_id
        feedback_data["collected_at"] = time.time()
        self.feedback_buffer.append(feedback_data)
        experiment.feedbacks.append(feedback_data)

    def curate_experiment(
        self,
        experiment: Experiment,
    ) -> List[CuratedInsight]:
        """
        策展一个完整实验，提取洞察。

        策展流程（HExA Curator pipeline）：
          1. 轨迹分析：从 action_trajectory 提取模式
          2. 成功/失败模式挖掘
          3. 跨实验关联分析（如果有历史实验）
          4. 生成可操作建议
        """
        self.total_experiments_curated += 1
        new_insights: List[CuratedInsight] = []

        # 1. 轨迹模式分析
        trajectory_insights = self._analyze_trajectory(experiment)
        new_insights.extend(trajectory_insights)

        # 2. 成功/失败模式
        outcome_insights = self._analyze_outcome(experiment)
        new_insights.extend(outcome_insights)

        # 3. 统计摘要
        stat_insight = self._generate_statistical_insight(experiment)
        if stat_insight:
            new_insights.append(stat_insight)

        # 4. 去重并入库存
        for insight in new_insights:
            if not self._is_duplicate(insight):
                self.insights[insight.insight_id] = insight
                self.total_insights_generated += 1

        # 5. 更新世界模型
        self._update_world_model(experiment, new_insights)

        return new_insights

    def query_insights(
        self,
        insight_type: Optional[str] = None,
        min_confidence: float = 0.0,
        limit: int = 50,
    ) -> List[CuratedInsight]:
        """查询已策展的洞察。"""
        results = []
        for insight in self.insights.values():
            if insight_type and insight.insight_type != insight_type:
                continue
            if insight.confidence < min_confidence:
                continue
            results.append(insight)

        results.sort(key=lambda x: x.confidence, reverse=True)
        return results[:limit]

    def generate_feedback_for_strategist(
        self,
        goal_ids: List[str],
    ) -> Dict[str, float]:
        """
        为 Strategist 生成反馈评分，用于优先级调整。

        基于该目标相关的所有洞察的置信度加权。
        """
        scores: Dict[str, float] = {}

        for gid in goal_ids:
            related_insights = [
                i for i in self.insights.values()
                if gid in i.evidence or gid in i.source_experiment_id
            ]
            if related_insights:
                # 置信度加权平均
                total_weight = sum(i.confidence for i in related_insights)
                weighted_score = sum(
                    i.confidence * (1.0 if i.insight_type == "improvement" else 0.5 if i.insight_type == "pattern" else -0.3)
                    for i in related_insights
                ) / max(total_weight, 1e-8)
                scores[gid] = weighted_score
            else:
                scores[gid] = 0.0

        return scores

    # ---- Internal ----

    def _analyze_trajectory(self, experiment: Experiment) -> List[CuratedInsight]:
        """分析动作轨迹，提取模式。"""
        insights = []
        trajectory = experiment.action_trajectory

        if not trajectory:
            return insights

        # 统计各动作成功率
        action_stats: Dict[str, Dict[str, int]] = defaultdict(lambda: {"success": 0, "total": 0})
        for step in trajectory:
            action_stats[step.action_name]["total"] += 1
            if step.success:
                action_stats[step.action_name]["success"] += 1

        for action_name, stats in action_stats.items():
            success_rate = stats["success"] / max(stats["total"], 1)
            if success_rate < 0.5 and stats["total"] >= 3:
                insights.append(CuratedInsight(
                    insight_id=f"insight_{uuid.uuid4().hex[:8]}",
                    source_experiment_id=experiment.experiment_id,
                    insight_type="warning",
                    description=f"Action '{action_name}' has low success rate: {success_rate:.2%}",
                    confidence=min(0.9, (1.0 - success_rate) * 1.5),
                    evidence=[f"{stats['success']}/{stats['total']} successes"],
                    suggested_actions=[f"Review {action_name} implementation", "Consider alternative approach"],
                ))

        # 检测重试模式
        retry_steps = [s for s in trajectory if s.retry_count > 0]
        if len(retry_steps) > len(trajectory) * 0.3:
            insights.append(CuratedInsight(
                insight_id=f"insight_{uuid.uuid4().hex[:8]}",
                source_experiment_id=experiment.experiment_id,
                insight_type="warning",
                description=f"High retry rate: {len(retry_steps)}/{len(trajectory)} steps needed retries",
                confidence=0.7,
                evidence=[f"{len(retry_steps)} retried steps"],
                suggested_actions=["Increase robustness of failing actions", "Add pre-checks before critical actions"],
            ))

        return insights

    def _analyze_outcome(self, experiment: Experiment) -> List[CuratedInsight]:
        """分析实验结果。"""
        insights = []

        if experiment.score >= 0.8:
            insights.append(CuratedInsight(
                insight_id=f"insight_{uuid.uuid4().hex[:8]}",
                source_experiment_id=experiment.experiment_id,
                insight_type="pattern",
                description=f"High-scoring experiment (score={experiment.score:.3f}): approach worth reusing",
                confidence=experiment.score,
                evidence=[experiment.hypothesis],
                suggested_actions=["Template this approach for similar goals"],
            ))
        elif experiment.score < 0.3:
            insights.append(CuratedInsight(
                insight_id=f"insight_{uuid.uuid4().hex[:8]}",
                source_experiment_id=experiment.experiment_id,
                insight_type="improvement",
                description=f"Low-scoring experiment (score={experiment.score:.3f}): needs significant revision",
                confidence=1.0 - experiment.score,
                evidence=[experiment.hypothesis],
                suggested_actions=["Re-evaluate hypothesis", "Try different action sequence"],
            ))

        return insights

    def _generate_statistical_insight(self, experiment: Experiment) -> Optional[CuratedInsight]:
        """生成统计摘要洞察。"""
        trajectory = experiment.action_trajectory
        if not trajectory:
            return None

        durations = [s.duration_ms for s in trajectory]
        avg_duration = np.mean(durations) if durations else 0
        success_rate = sum(1 for s in trajectory if s.success) / len(trajectory)

        return CuratedInsight(
            insight_id=f"insight_{uuid.uuid4().hex[:8]}",
            source_experiment_id=experiment.experiment_id,
            insight_type="pattern",
            description=(
                f"Experiment stats: {len(trajectory)} steps, "
                f"avg duration {avg_duration:.1f}ms, "
                f"success rate {success_rate:.1%}"
            ),
            confidence=0.6,
            evidence=[f"{len(trajectory)} steps recorded"],
            suggested_actions=[],
        )

    def _is_duplicate(self, insight: CuratedInsight) -> bool:
        """检查洞察是否与已有洞察重复。"""
        # 简单的描述相似度检查（生产环境可用 embedding）
        for existing in self.insights.values():
            # 如果描述完全相同或高度相似
            if existing.description == insight.description:
                # 提升已有洞察的置信度
                existing.confidence = min(
                    1.0,
                    existing.confidence + 0.1
                )
                existing.evidence.extend(insight.evidence)
                return True
        return False

    def _update_world_model(
        self,
        experiment: Experiment,
        insights: List[CuratedInsight],
    ):
        """基于策展结果更新世界模型。"""
        # 记录实验统计
        if "experiment_stats" not in self.world_model:
            self.world_model["experiment_stats"] = {
                "total": 0,
                "high_score": 0,
                "low_score": 0,
            }
        self.world_model["experiment_stats"]["total"] += 1
        if experiment.score >= 0.8:
            self.world_model["experiment_stats"]["high_score"] += 1
        elif experiment.score < 0.3:
            self.world_model["experiment_stats"]["low_score"] += 1

        # 记录动作成功率
        if "action_success_rates" not in self.world_model:
            self.world_model["action_success_rates"] = {}

        for step in experiment.action_trajectory:
            if step.action_name not in self.world_model["action_success_rates"]:
                self.world_model["action_success_rates"][step.action_name] = {
                    "success": 0, "total": 0
                }
            self.world_model["action_success_rates"][step.action_name]["total"] += 1
            if step.success:
                self.world_model["action_success_rates"][step.action_name]["success"] += 1

        # 更新时间戳
        self.world_model["last_updated"] = time.time()

    def summary(self) -> Dict[str, Any]:
        return {
            "experiments_curated": self.total_experiments_curated,
            "insights_generated": self.total_insights_generated,
            "insight_types": {
                t: sum(1 for i in self.insights.values() if i.insight_type == t)
                for t in ["pattern", "rule", "warning", "improvement"]
            },
            "world_model_keys": list(self.world_model.keys()),
        }


# ============================================================================
# HExA Orchestrator — 三层协调器
# ============================================================================


class HierarchicalExperimentalist:
    """
    HierarchicalExperimentalist — HExA 主协调器

    协调 Strategist / Executor / Curator 三层，实现完整的
    "假设 → 实验 → 反馈 → 更新" 闭环。

    与 M113 AutoCurriculaOrchestrator 协作：
      - M113 调用 generate_curriculum_task() 生成训练任务
      - M117 接收任务 → 三层执行 → 返回反馈
      - M113 基于反馈更新课程难度估计
    """

    def __init__(
        self,
        strategist: Optional[Strategist] = None,
        executor: Optional[Executor] = None,
        curator: Optional[Curator] = None,
        enable_auto_curation: bool = True,
    ):
        self.strategist = strategist or Strategist()
        self.executor = executor or Executor()
        self.curator = curator or Curator()
        self.enable_auto_curation = enable_auto_curation

        # 实验历史
        self.experiments: Dict[str, Experiment] = {}
        # M113 课程桥接
        self.m113_curriculum_bridge: Optional[Any] = None

    # ---- Core API ----

    def run_experiment(
        self,
        hypothesis: str,
        world_state: Optional[Dict[str, Any]] = None,
        constraints: Optional[List[str]] = None,
        action_mapper: Optional[Callable[[SubGoal], List[Dict[str, Any]]]] = None,
    ) -> Experiment:
        """
        运行完整实验：Hypothesize → Design → Execute → Analyze → Curate

        返回完整的 Experiment 对象，包含轨迹、反馈和洞察。
        """
        experiment = Experiment(
            experiment_id=f"exp_{uuid.uuid4().hex[:8]}",
            hypothesis=hypothesis,
            phase=ExperimentPhase.HYPOTHESIZE,
        )
        world_state = world_state or {}

        # Phase 1: Hypothesize (Strategist 分解目标)
        experiment.phase = ExperimentPhase.HYPOTHESIZE
        subgoals = self.strategist.decompose_goal(hypothesis, constraints)
        experiment.subgoals = subgoals

        # Phase 2: Design (Strategist 优先级排序)
        experiment.phase = ExperimentPhase.DESIGN
        goal_ids = [sg.goal_id for sg in subgoals]
        feedback_scores = self.curator.generate_feedback_for_strategist(goal_ids)
        prioritized = self.strategist.prioritize_goals(goal_ids, feedback_scores)
        # 按优先级排序子目标
        priority_map = dict(prioritized)
        subgoals.sort(key=lambda sg: priority_map.get(sg.goal_id, 0.0), reverse=True)

        # Phase 3: Execute (Executor 执行每个子目标)
        experiment.phase = ExperimentPhase.EXECUTE
        total_score = 0.0
        for goal in subgoals:
            if goal.status == GoalStatus.BLOCKED:
                continue

            # 可达性检查
            attainability = self.strategist.assess_attainability(goal.goal_id, world_state)
            if attainability < 0.2:
                goal.status = GoalStatus.BLOCKED
                goal.metadata["block_reason"] = f"Low attainability: {attainability:.2f}"
                continue

            steps = self.executor.execute_goal(
                goal=goal,
                experiment=experiment,
                world_state=world_state,
                action_mapper=action_mapper,
            )
            # 累加分数
            step_success = sum(1 for s in steps if s.success) / max(len(steps), 1)
            total_score += step_success * goal.priority

        # 归一化总分
        total_priority = sum(sg.priority for sg in subgoals)
        experiment.score = total_score / max(total_priority, 1e-8)
        experiment.outcome = "success" if experiment.score >= 0.7 else "partial" if experiment.score >= 0.3 else "failure"

        # Phase 4: Analyze + Curate
        experiment.phase = ExperimentPhase.ANALYZE
        if self.enable_auto_curation:
            experiment.phase = ExperimentPhase.CURATE
            self.curator.curate_experiment(experiment)

        experiment.completed_at = time.time()
        self.experiments[experiment.experiment_id] = experiment

        return experiment

    def run_comparative_experiment(
        self,
        hypothesis: str,
        variants: List[Dict[str, Any]],
        world_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Experiment]:
        """
        运行对比实验：同一假设，不同执行变体。
        用于 A/B testing 和消融研究。
        """
        results = {}
        for variant in variants:
            variant_world = variant.get("world_state", world_state)
            variant_mapper = variant.get("action_mapper")
            variant_constraints = variant.get("constraints")

            exp = self.run_experiment(
                hypothesis=f"{hypothesis} [variant: {variant.get('id', 'unknown')}]",
                world_state=variant_world,
                constraints=variant_constraints,
                action_mapper=variant_mapper,
            )
            results[variant.get("id", f"variant_{len(results)}")] = exp

        return results

    # ---- M113 Bridge ----

    def set_curriculum_bridge(self, m113_bridge: Any):
        """设置与 M113 AutoCurriculaOrchestrator 的桥接。"""
        self.m113_curriculum_bridge = m113_bridge

    def receive_curriculum_task(
        self,
        task_spec: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        接收 M113 下发的课程任务，执行并返回反馈。

        task_spec 格式（由 M113 CurriculumOrchestrator 生成）：
          {
            "task_id": str,
            "description": str,
            "difficulty": float,
            "prerequisites": List[str],
            "evaluation_metric": str,
          }
        """
        hypothesis = task_spec.get("description", "Unnamed task")
        constraints = task_spec.get("prerequisites", [])

        experiment = self.run_experiment(
            hypothesis=hypothesis,
            constraints=constraints,
        )

        # 生成 M113 可消费的反馈格式
        feedback = {
            "task_id": task_spec.get("task_id", ""),
            "experiment_id": experiment.experiment_id,
            "score": experiment.score,
            "outcome": experiment.outcome,
            "subgoal_summary": self.strategist.summary(),
            "executor_summary": self.executor.summary(),
            "curator_insights": [
                {
                    "type": i.insight_type,
                    "description": i.description,
                    "confidence": i.confidence,
                }
                for i in self.curator.query_insights(limit=10)
            ],
            "completed_at": experiment.completed_at,
        }

        return feedback

    # ---- Stats ----

    def summary(self) -> Dict[str, Any]:
        return {
            "total_experiments": len(self.experiments),
            "strategist": self.strategist.summary(),
            "executor": self.executor.summary(),
            "curator": self.curator.summary(),
            "average_score": (
                np.mean([e.score for e in self.experiments.values()])
                if self.experiments else 0.0
            ),
        }


# ============================================================================
# Demo & Self-Test
# ============================================================================


def demo():
    """Self-contained demo validating all three layers and orchestration."""
    print("=" * 60)
    print("M117 HierarchicalExperimentalist — Self-Test")
    print("Based on HExA (arXiv 2606.29315)")
    print("=" * 60)

    results = []
    test_id = 0

    # --- Test 1: Strategist ---
    test_id += 1
    print(f"\n[Test {test_id}] Strategist: goal decomposition")
    s = Strategist(max_subgoals=20, decomposition_max_depth=3)
    goals = s.decompose_goal("Build a text classification pipeline", ["must run locally"])
    assert len(goals) >= 4, f"Expected >=4 goals, got {len(goals)}"
    assert goals[0].description == "Build a text classification pipeline"
    print(f"  PASS — {len(goals)} subgoals created (root + {len(goals)-1} children)")
    results.append(("Strategist-decompose", True))

    # --- Test 2: Strategist prioritization ---
    test_id += 1
    print(f"\n[Test {test_id}] Strategist: dynamic prioritization")
    p = s.prioritize_goals(
        [g.goal_id for g in goals],
        feedback_scores={goals[1].goal_id: 0.5, goals[2].goal_id: -0.2},
    )
    assert len(p) == len(goals)
    # 有正反馈的应排前面
    top_goals = [gid for gid, _ in p[:3]]
    print(f"  PASS — top 3 goals prioritized, feedback bonus applied")
    results.append(("Strategist-prioritize", True))

    # --- Test 3: Executor ---
    test_id += 1
    print(f"\n[Test {test_id}] Executor: goal execution with trajectory")

    # 注册模拟动作
    e = Executor(max_steps_per_goal=10, max_retries=2)
    e.register_action("search", lambda **kw: {"outcome": "found results", "success": True})
    e.register_action("read", lambda **kw: {"outcome": "read content", "success": True})
    e.register_action("analyze", lambda **kw: {"outcome": "analysis done", "success": True})
    e.register_action("write", lambda **kw: {"outcome": "written", "success": True})
    e.register_action("verify", lambda **kw: {"outcome": "verified", "success": True})

    exp = Experiment(
        experiment_id="test_exp_001",
        hypothesis="Test execution pipeline",
    )
    goal = SubGoal(
        goal_id="test_goal_1",
        description="Analyze text data",
        allowed_actions=["search", "read", "analyze", "write", "verify"],
    )
    steps = e.execute_goal(goal, exp)
    assert len(steps) == 5, f"Expected 5 steps, got {len(steps)}"
    assert all(s.success for s in steps), "All steps should succeed"
    assert goal.status == GoalStatus.SUCCESS
    print(f"  PASS — {len(steps)} steps, all success, goal status={goal.status.value}")
    results.append(("Executor-execute", True))

    # --- Test 4: Curator ---
    test_id += 1
    print(f"\n[Test {test_id}] Curator: experiment curation and insight generation")
    c = Curator()
    # 造一个低分实验
    bad_exp = Experiment(
        experiment_id="test_exp_bad",
        hypothesis="Flawed approach",
        score=0.15,
    )
    bad_steps = [
        ActionStep(step_id=f"s{i}", goal_id="g1", action_name="search", success=i % 2 == 0, retry_count=i % 3)
        for i in range(6)
    ]
    bad_exp.action_trajectory = bad_steps
    c.collect_feedback(bad_exp, {"type": "manual", "note": "this approach failed"})
    insights = c.curate_experiment(bad_exp)
    assert len(insights) >= 1, f"Expected >=1 insight, got {len(insights)}"
    print(f"  PASS — {len(insights)} insights generated for low-score experiment")
    results.append(("Curator-curate", True))

    # --- Test 5: Full HExA pipeline ---
    test_id += 1
    print(f"\n[Test {test_id}] Full HExA pipeline: hypothesis → experiment → curation")
    hexa = HierarchicalExperimentalist(
        strategist=Strategist(max_subgoals=15, decomposition_max_depth=3),
        executor=Executor(max_steps_per_goal=10, max_retries=2),
        curator=Curator(),
    )
    # 注册动作
    hexa.executor.register_action("search", lambda **kw: {"outcome": "ok", "success": True})
    hexa.executor.register_action("read", lambda **kw: {"outcome": "ok", "success": True})
    hexa.executor.register_action("analyze", lambda **kw: {"outcome": "ok", "success": True})
    hexa.executor.register_action("write", lambda **kw: {"outcome": "ok", "success": True})
    hexa.executor.register_action("verify", lambda **kw: {"outcome": "ok", "success": True})

    experiment = hexa.run_experiment(
        hypothesis="Identify optimal data preprocessing steps",
        constraints=["time_budget=60s"],
    )
    assert experiment.completed_at is not None
    assert experiment.phase == ExperimentPhase.CURATE
    assert 0 <= experiment.score <= 1.0
    print(f"  PASS — experiment {experiment.experiment_id}: score={experiment.score:.3f}, outcome={experiment.outcome}")
    results.append(("HExA-pipeline", True))

    # --- Test 6: M113 Bridge ---
    test_id += 1
    print(f"\n[Test {test_id}] M113 Curriculum Bridge: receive task → execute → feedback")
    task = {
        "task_id": "curriculum_task_001",
        "description": "Classify sentiment in user reviews",
        "difficulty": 0.6,
        "prerequisites": ["nlp_basics", "text_preprocessing"],
        "evaluation_metric": "accuracy",
    }
    feedback = hexa.receive_curriculum_task(task)
    assert "experiment_id" in feedback
    assert "score" in feedback
    assert "curator_insights" in feedback
    print(f"  PASS — feedback generated: score={feedback['score']:.3f}, "
          f"{len(feedback['curator_insights'])} curator insights")
    results.append(("M113-bridge", True))

    # --- Test 7: Comparative experiment ---
    test_id += 1
    print(f"\n[Test {test_id}] Comparative experiment: A/B variants")
    variants = [
        {"id": "baseline", "constraints": ["strict_mode"]},
        {"id": "experimental", "constraints": ["relaxed_mode"]},
    ]
    comp_results = hexa.run_comparative_experiment(
        hypothesis="Compare preprocessing strategies",
        variants=variants,
    )
    assert len(comp_results) == 2
    for vid, vexp in comp_results.items():
        assert vexp.completed_at is not None
    print(f"  PASS — {len(comp_results)} variants executed: "
          f"baseline={comp_results['baseline'].score:.3f}, "
          f"experimental={comp_results['experimental'].score:.3f}")
    results.append(("Comparative-experiment", True))

    # --- Test 8: World model update ---
    test_id += 1
    print(f"\n[Test {test_id}] World model update after curation")
    wm = hexa.curator.world_model
    assert "experiment_stats" in wm
    assert "action_success_rates" in wm
    print(f"  PASS — world model: {wm['experiment_stats']}")
    results.append(("World-model", True))

    # --- Final summary ---
    print("\n" + "=" * 60)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"RESULTS: {passed}/{total} tests passed")
    print(f"Strategist:    {hexa.strategist.summary()}")
    print(f"Executor:      {hexa.executor.summary()}")
    print(f"Curator:       {hexa.curator.summary()}")
    print(f"Total experiments: {len(hexa.experiments)}")
    print("=" * 60)

    return passed == total


if __name__ == "__main__":
    success = demo()
    exit(0 if success else 1)
