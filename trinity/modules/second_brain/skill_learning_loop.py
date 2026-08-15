"""
# status: orphan (2026-08-15 audit, not in runtime path)
P13-2: Token-Space Skill Continuous Learning Loop
===================================================

对标 Letta Learning SDK 技能学习闭环。

核心能力：
  - TrajectoryCollector:  从代理执行轨迹中自动捕获成功/失败序列
  - SkillExtractor:       从轨迹中提取可复用技能步骤，存储为 .md 格式
  - SkillGeneralizer:     泛化技能使其可跨任务/跨模型使用
  - CrossModelTransfer:   强模型生成技能 → 弱模型消费技能的传递管道
  - SkillBenchmark:       技能效果评估（对比启用/禁用技能的端到端表现差异）

接口兼容：
  - skill_synthesis.py:    技能合成触发与存储
  - proactive_anticipator.py: 预判与技能预加载协同
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
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

class TrajectoryStatus(Enum):
    """轨迹状态。"""
    SUCCESS = "success"         # 成功完成任务
    FAILURE = "failure"         # 任务失败
    PARTIAL = "partial"         # 部分完成
    ABORTED = "aborted"         # 用户中止
    TIMEOUT = "timeout"         # 超时


class SkillFormat(Enum):
    """技能存储格式。"""
    MARKDOWN = "markdown"       # .md 模型无关格式（默认）
    JSON = "json"               # 结构化 JSON
    PROMPT_TEMPLATE = "prompt_template"  # 提示模板


class SkillComplexity(Enum):
    """技能复杂度。"""
    ATOMIC = "atomic"           # 单步操作
    COMPOSITE = "composite"     # 多步组合
    CONDITIONAL = "conditional" # 条件分支
    ITERATIVE = "iterative"     # 循环迭代


class TransferMode(Enum):
    """跨模型传输模式。"""
    STRONG_TO_WEAK = "strong_to_weak"      # 强 → 弱（蒸馏）
    WEAK_TO_STRONG = "weak_to_strong"      # 弱 → 强（提升）
    PEER_TRANSFER = "peer_transfer"        # 对等传输
    ENSEMBLE = "ensemble"                   # 多模型集成


class BenchmarkMode(Enum):
    """基准测试模式。"""
    AB_TEST = "ab_test"                    # A/B 对比（启用/禁用）
    REPEATED_MEASURES = "repeated_measures"  # 重复测量
    CROSS_VALIDATION = "cross_validation"   # 交叉验证
    HOLDOUT = "holdout"                     # 留出法


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ActionStep:
    """轨迹中的单个动作步骤。"""
    step_id: str
    action_type: str           # 工具调用 / 推理 / 用户交互等
    input_data: str
    output_data: str
    duration_ms: float = 0.0
    success: bool = True
    error_message: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Trajectory:
    """完整的代理执行轨迹。"""
    trajectory_id: str
    task_description: str
    task_domain: str
    steps: List[ActionStep]
    status: TrajectoryStatus = TrajectoryStatus.SUCCESS
    total_duration_ms: float = 0.0
    model_name: str = ""
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    tags: List[str] = field(default_factory=list)
    success_patterns: List[str] = field(default_factory=list)
    failure_patterns: List[str] = field(default_factory=list)


@dataclass
class Skill:
    """提取出的可复用技能。"""
    skill_id: str
    name: str
    description: str
    steps: List[str]                # 技能步骤（Markdown 描述）
    source_trajectories: List[str]  # 来源轨迹 ID
    format: SkillFormat = SkillFormat.MARKDOWN
    complexity: SkillComplexity = SkillComplexity.ATOMIC
    success_rate: float = 0.0
    usage_count: int = 0
    domain: str = ""
    generalization_level: float = 0.0  # 泛化程度 0-1
    created_at: float = field(default_factory=time.time)
    last_used_at: float = 0.0
    version: int = 1
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_markdown(self) -> str:
        """导出为 .md 格式。"""
        lines = [
            f"# {self.name}",
            f"",
            f"**Description**: {self.description}",
            f"**Domain**: {self.domain}",
            f"**Complexity**: {self.complexity.value}",
            f"**Success Rate**: {self.success_rate:.2%}",
            f"**Version**: v{self.version}",
            f"**Tags**: {', '.join(self.tags)}",
            f"",
            f"## Steps",
            f"",
        ]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"{i}. {step}")
        lines.append(f"")
        lines.append(f"## Metadata")
        lines.append(f"```json")
        lines.append(json.dumps({
            "skill_id": self.skill_id,
            "generalization_level": self.generalization_level,
            "source_trajectories": self.source_trajectories,
            "usage_count": self.usage_count,
        }, indent=2))
        lines.append(f"```")
        return "\n".join(lines)


@dataclass
class BenchmarkResult:
    """技能基准测试结果。"""
    test_id: str
    skill_id: str
    skill_name: str
    mode: BenchmarkMode
    enabled_score: float       # 启用技能时的端到端分数
    disabled_score: float      # 禁用技能时的端到端分数
    delta: float               # 分数差异
    delta_pct: float           # 相对百分比
    significance: float         # 统计显著性（p-value 近似）
    sample_size: int = 0
    created_at: float = field(default_factory=time.time)


# ============================================================================
# TrajectoryCollector
# ============================================================================

class TrajectoryCollector:
    """从代理执行轨迹中自动捕获成功/失败序列。

    监听代理执行过程，记录每个步骤的输入/输出/耗时/状态，
    构建完整的 Trajectory 对象供下游 SkillExtractor 使用。
    """

    def __init__(self, max_trajectories: int = 1000):
        self.max_trajectories = max_trajectories
        self._lock = threading.RLock()
        self._active_trajectories: Dict[str, Tuple[Trajectory, List[ActionStep]]] = {}
        self._completed_trajectories: deque = deque(maxlen=max_trajectories)
        self._total_collected = 0
        self._total_success = 0
        self._total_failure = 0

    def start_trajectory(
        self,
        task_description: str,
        task_domain: str = "",
        model_name: str = "",
        session_id: str = "",
    ) -> str:
        """开始记录新轨迹，返回 trajectory_id。"""
        trajectory_id = f"traj_{uuid.uuid4().hex[:12]}"
        traj = Trajectory(
            trajectory_id=trajectory_id,
            task_description=task_description,
            task_domain=task_domain,
            steps=[],
            model_name=model_name,
            session_id=session_id,
        )
        with self._lock:
            self._active_trajectories[trajectory_id] = (traj, [])
        return trajectory_id

    def record_step(
        self,
        trajectory_id: str,
        action_type: str,
        input_data: str,
        output_data: str,
        duration_ms: float = 0.0,
        success: bool = True,
        error_message: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """记录轨迹中的一步。"""
        step = ActionStep(
            step_id=f"step_{uuid.uuid4().hex[:8]}",
            action_type=action_type,
            input_data=input_data,
            output_data=output_data,
            duration_ms=duration_ms,
            success=success,
            error_message=error_message,
            metadata=metadata or {},
        )
        with self._lock:
            entry = self._active_trajectories.get(trajectory_id)
            if entry is None:
                return None
            entry[1].append(step)
            return step.step_id

    def finish_trajectory(
        self,
        trajectory_id: str,
        status: TrajectoryStatus = TrajectoryStatus.SUCCESS,
    ) -> Optional[Trajectory]:
        """结束轨迹记录，返回完成的 Trajectory。"""
        with self._lock:
            entry = self._active_trajectories.pop(trajectory_id, None)
            if entry is None:
                return None
            traj, steps = entry
            traj.steps = steps
            traj.status = status
            traj.total_duration_ms = sum(s.duration_ms for s in steps)

            # 提取成功/失败模式
            traj.success_patterns = [
                f"{s.action_type}:{s.input_data[:50]}" for s in steps if s.success
            ][:5]
            traj.failure_patterns = [
                f"{s.action_type}:{s.error_message[:50]}" for s in steps if not s.success
            ][:5]

            self._completed_trajectories.append(traj)
            self._total_collected += 1
            if status == TrajectoryStatus.SUCCESS:
                self._total_success += 1
            else:
                self._total_failure += 1
            return traj

    def get_recent_trajectories(self, n: int = 20, status: Optional[TrajectoryStatus] = None) -> List[Trajectory]:
        """获取最近 N 条轨迹。"""
        with self._lock:
            trajs = list(self._completed_trajectories)
            if status:
                trajs = [t for t in trajs if t.status == status]
            return trajs[-n:]

    def get_successful_trajectories(self, domain: str = "", limit: int = 50) -> List[Trajectory]:
        """获取成功轨迹，可按领域过滤。"""
        with self._lock:
            trajs = [t for t in self._completed_trajectories if t.status == TrajectoryStatus.SUCCESS]
            if domain:
                trajs = [t for t in trajs if t.task_domain == domain]
            return trajs[-limit:]

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "active_trajectories": len(self._active_trajectories),
                "completed_trajectories": len(self._completed_trajectories),
                "total_collected": self._total_collected,
                "total_success": self._total_success,
                "total_failure": self._total_failure,
                "success_rate": (
                    self._total_success / max(self._total_collected, 1)
                ),
            }


# ============================================================================
# SkillExtractor
# ============================================================================

class SkillExtractor:
    """从轨迹中提取可复用的技能步骤，存储为模型无关的 .md 格式。

    提取策略：
      - 模式挖掘：识别反复出现的步骤序列
      - 关键路径提取：去除噪音步骤，保留核心操作链
      - 成功/失败对比：从成功轨迹中提取正向技能
    """

    def __init__(self, min_step_sequence: int = 2, max_skill_steps: int = 20):
        self.min_step_sequence = min_step_sequence
        self.max_skill_steps = max_skill_steps
        self._lock = threading.RLock()
        self._extracted_skills: Dict[str, Skill] = {}
        self._step_patterns: Dict[str, int] = defaultdict(int)  # pattern → frequency

    def extract_from_trajectory(self, trajectory: Trajectory) -> List[Skill]:
        """从单条轨迹中提取技能。"""
        skills: List[Skill] = []
        if len(trajectory.steps) < self.min_step_sequence:
            return skills

        with self._lock:
            # 按连续成功步骤分段
            success_segments = self._segment_by_success(trajectory.steps)

            for segment in success_segments:
                if len(segment) < self.min_step_sequence:
                    continue

                # 生成 step 描述
                step_descriptions = [
                    self._describe_step(s) for s in segment[:self.max_skill_steps]
                ]

                # 生成模式指纹
                pattern = "→".join(s.action_type for s in segment[:self.max_skill_steps])
                self._step_patterns[pattern] += 1

                skill = Skill(
                    skill_id=f"skill_{uuid.uuid4().hex[:12]}",
                    name=self._infer_skill_name(segment),
                    description=f"从任务「{trajectory.task_description[:100]}」中提取的 {len(segment)} 步技能",
                    steps=step_descriptions,
                    source_trajectories=[trajectory.trajectory_id],
                    format=SkillFormat.MARKDOWN,
                    complexity=self._infer_complexity(segment),
                    domain=trajectory.task_domain,
                    tags=trajectory.tags,
                )
                self._extracted_skills[skill.skill_id] = skill
                skills.append(skill)

        return skills

    def _segment_by_success(self, steps: List[ActionStep]) -> List[List[ActionStep]]:
        """按连续成功步骤分段。"""
        segments = []
        current = []
        for step in steps:
            if step.success:
                current.append(step)
            else:
                if len(current) >= self.min_step_sequence:
                    segments.append(current)
                current = []
        if len(current) >= self.min_step_sequence:
            segments.append(current)
        return segments

    def _describe_step(self, step: ActionStep) -> str:
        """生成步骤的自然语言描述。"""
        if step.success:
            return f"执行 `{step.action_type}` → 结果: {step.output_data[:80]}"
        return f"执行 `{step.action_type}` 失败: {step.error_message[:80]}"

    def _infer_skill_name(self, steps: List[ActionStep]) -> str:
        """推断技能名称。"""
        action_types = list(set(s.action_type for s in steps))
        if len(action_types) == 1:
            return f"{action_types[0].replace('_', ' ').title()} ({len(steps)} steps)"
        return f"Composite: {' → '.join(action_types[:3])}"

    def _infer_complexity(self, steps: List[ActionStep]) -> SkillComplexity:
        """推断技能复杂度。"""
        unique_actions = len(set(s.action_type for s in steps))
        if unique_actions == 1:
            return SkillComplexity.ATOMIC
        if unique_actions <= 3:
            return SkillComplexity.COMPOSITE
        return SkillComplexity.CONDITIONAL

    def get_skill(self, skill_id: str) -> Optional[Skill]:
        """获取指定技能。"""
        with self._lock:
            return self._extracted_skills.get(skill_id)

    def search_by_domain(self, domain: str) -> List[Skill]:
        """按领域搜索技能。"""
        with self._lock:
            return [s for s in self._extracted_skills.values() if s.domain == domain]

    def get_top_patterns(self, n: int = 10) -> List[Tuple[str, int]]:
        """获取最高频的步骤模式。"""
        with self._lock:
            return sorted(self._step_patterns.items(), key=lambda x: x[1], reverse=True)[:n]

    def export_skill_markdown(self, skill_id: str) -> Optional[str]:
        """导出技能为 Markdown 字符串。"""
        skill = self.get_skill(skill_id)
        if skill:
            return skill.to_markdown()
        return None

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "extracted_skills": len(self._extracted_skills),
                "unique_patterns": len(self._step_patterns),
                "by_complexity": {
                    c.value: sum(1 for s in self._extracted_skills.values() if s.complexity == c)
                    for c in SkillComplexity
                },
            }


# ============================================================================
# SkillGeneralizer
# ============================================================================

class SkillGeneralizer:
    """泛化技能使其可跨任务/跨模型使用。

    核心策略：
      - 实体抽象化：将具体实体名替换为 {entity} 占位符
      - 参数化：识别可变参数，转换为模板变量
      - 领域适配：将领域特定术语映射为通用等价词
      - 抽象层次提升：从具体步骤推导更高层次的操作意图
    """

    def __init__(self, generalization_threshold: float = 0.6):
        self.generalization_threshold = generalization_threshold
        self._lock = threading.RLock()
        self._entity_pattern = re.compile(
            r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b'
        )
        self._generalized_skills: Dict[str, Skill] = {}

    def generalize(self, skill: Skill) -> Skill:
        """对技能进行泛化处理，返回泛化后的副本。"""
        with self._lock:
            generalized_steps = []
            for step in skill.steps:
                generalized_steps.append(self._abstract_entities(step))

            generalized = Skill(
                skill_id=f"gen_{skill.skill_id}",
                name=f"[GEN] {skill.name}",
                description=f"泛化版: {skill.description}",
                steps=generalized_steps,
                source_trajectories=list(skill.source_trajectories),
                format=skill.format,
                complexity=skill.complexity,
                domain=skill.domain,
                generalization_level=1.0,
                tags=skill.tags + ["generalized"],
                metadata={"original_skill_id": skill.skill_id},
            )

            self._generalized_skills[generalized.skill_id] = generalized
            return generalized

    def _abstract_entities(self, text: str) -> str:
        """将文本中的实体名替换为占位符。"""
        replacements = {
            "file_path": r'(?:[A-Z]:\\(?:[^\\/:*?"<>|]+\\)*[^\\/:*?"<>|]+)',
            "url": r'https?://\S+',
            "email": r'\S+@\S+\.\S+',
            "date": r'\d{4}-\d{2}-\d{2}',
        }
        result = text
        result = re.sub(replacements["file_path"], r"`{file_path}`", result)
        result = re.sub(replacements["url"], r"`{url}`", result)
        result = re.sub(replacements["email"], r"`{email}`", result)
        return result

    def is_generalizable(self, skill: Skill) -> bool:
        """判断技能是否可泛化。"""
        if skill.generalization_level >= self.generalization_threshold:
            return False
        has_concrete = bool(
            self._entity_pattern.search(" ".join(skill.steps))
            or any("://" in s for s in skill.steps)
        )
        return has_concrete

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "generalized_skills": len(self._generalized_skills),
                "threshold": self.generalization_threshold,
            }


# ============================================================================
# CrossModelTransfer
# ============================================================================

class CrossModelTransfer:
    """强模型生成技能 → 弱模型消费技能的传递管道。

    Letta 范式：强模型（如 GPT-4o）生成高质量技能描述，
    弱模型（如小型部署模型）在执行时参考这些技能。
    管道确保技能描述是模型无关的 .md 格式，可被任何模型消费。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._transfer_history: List[Dict[str, Any]] = []
        self._skill_registry: Dict[str, Dict[str, Skill]] = defaultdict(dict)
        # model_name → {skill_id → Skill}

    def register_strong_skill(self, model_name: str, skill: Skill) -> None:
        """注册由强模型生成的技能。"""
        with self._lock:
            self._skill_registry[model_name][skill.skill_id] = skill
            logger.info(f"Strong skill registered: {skill.name} from {model_name}")

    def transfer_to_weak(self, skill_id: str, target_model: str) -> Optional[Skill]:
        """将技能传输给弱模型——返回模型无关的 Markdown 格式技能。"""
        with self._lock:
            # 从所有强模型中查找技能
            skill = None
            source_model = ""
            for model, skills in self._skill_registry.items():
                if skill_id in skills:
                    skill = skills[skill_id]
                    source_model = model
                    break

            if skill is None:
                return None

            # 为弱模型创建适配版本（保证 .md 格式）
            adapted = Skill(
                skill_id=f"weak_{skill.skill_id}_{target_model}",
                name=skill.name,
                description=skill.description,
                steps=skill.steps,
                source_trajectories=list(skill.source_trajectories),
                format=SkillFormat.MARKDOWN,
                complexity=skill.complexity,
                domain=skill.domain,
                tags=skill.tags + [f"transferred_to:{target_model}"],
                metadata={
                    "source_model": source_model,
                    "target_model": target_model,
                    "original_skill_id": skill_id,
                },
            )

            self._skill_registry[target_model][adapted.skill_id] = adapted
            self._transfer_history.append({
                "skill_id": skill_id,
                "source": source_model,
                "target": target_model,
                "timestamp": time.time(),
            })
            return adapted

    def batch_transfer(
        self,
        skill_ids: List[str],
        target_model: str,
    ) -> List[Skill]:
        """批量传输技能。"""
        results = []
        for sid in skill_ids:
            skill = self.transfer_to_weak(sid, target_model)
            if skill:
                results.append(skill)
        return results

    def get_skills_for_model(self, model_name: str) -> List[Skill]:
        """获取某模型可用的所有技能。"""
        with self._lock:
            return list(self._skill_registry.get(model_name, {}).values())

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_transfers": len(self._transfer_history),
                "registered_models": list(self._skill_registry.keys()),
                "skills_per_model": {
                    m: len(s) for m, s in self._skill_registry.items()
                },
            }


# ============================================================================
# SkillBenchmark
# ============================================================================

class SkillBenchmark:
    """技能效果评估——对比启用/禁用技能的端到端表现差异。

    支持四种评估模式：
      - A/B Test: 随机分配启用/禁用，对比结果
      - Repeated Measures: 同任务启用/禁用各测 N 次
      - Cross-Validation: K 折交叉验证
      - Holdout: 留出部分任务做验证
    """

    def __init__(self, mode: BenchmarkMode = BenchmarkMode.AB_TEST):
        self.mode = mode
        self._lock = threading.RLock()
        self._results: List[BenchmarkResult] = []
        self._pending_tests: Dict[str, Dict[str, Any]] = {}

    def start_test(
        self,
        skill_id: str,
        skill_name: str,
        sample_size: int = 30,
    ) -> str:
        """启动基准测试。"""
        test_id = f"bench_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._pending_tests[test_id] = {
                "skill_id": skill_id,
                "skill_name": skill_name,
                "sample_size": sample_size,
                "started_at": time.time(),
                "enabled_runs": 0,
                "disabled_runs": 0,
                "enabled_scores": [],
                "disabled_scores": [],
            }
        return test_id

    def record_enabled_run(self, test_id: str, score: float) -> None:
        """记录启用技能的一次运行。"""
        with self._lock:
            test = self._pending_tests.get(test_id)
            if test:
                test["enabled_scores"].append(score)
                test["enabled_runs"] += 1
                self._check_completion(test_id)

    def record_disabled_run(self, test_id: str, score: float) -> None:
        """记录禁用技能的一次运行。"""
        with self._lock:
            test = self._pending_tests.get(test_id)
            if test:
                test["disabled_scores"].append(score)
                test["disabled_runs"] += 1
                self._check_completion(test_id)

    def _check_completion(self, test_id: str) -> None:
        """检查测试是否完成，若完成则计算统计量。"""
        test = self._pending_tests.get(test_id)
        if test is None:
            return
        required = test["sample_size"]
        if len(test["enabled_scores"]) >= required and len(test["disabled_scores"]) >= required:
            enabled = np.array(test["enabled_scores"][:required])
            disabled = np.array(test["disabled_scores"][:required])

            delta = np.mean(enabled) - np.mean(disabled)
            delta_pct = (delta / max(np.mean(disabled), 1e-8)) * 100

            # 简化 Welch's t-test
            se = np.sqrt(
                np.var(enabled, ddof=1) / len(enabled)
                + np.var(disabled, ddof=1) / len(disabled)
            )
            t_stat = delta / max(se, 1e-8)
            # 近似 p-value（单侧）
            significance = float(2.0 * (1.0 - min(0.999, _norm_cdf_approx(abs(t_stat)))))

            result = BenchmarkResult(
                test_id=test_id,
                skill_id=test["skill_id"],
                skill_name=test["skill_name"],
                mode=self.mode,
                enabled_score=float(np.mean(enabled)),
                disabled_score=float(np.mean(disabled)),
                delta=float(delta),
                delta_pct=float(delta_pct),
                significance=significance,
                sample_size=required,
            )
            self._results.append(result)
            del self._pending_tests[test_id]

    def get_best_skills(self, n: int = 10, min_significance: float = 0.05) -> List[BenchmarkResult]:
        """获取提升最大的技能排行。"""
        with self._lock:
            significant = [r for r in self._results if r.significance <= min_significance]
            return sorted(significant, key=lambda r: r.delta, reverse=True)[:n]

    def get_skill_effect(self, skill_id: str) -> Optional[BenchmarkResult]:
        """获取指定技能的评估结果（最新一次）。"""
        with self._lock:
            matches = [r for r in self._results if r.skill_id == skill_id]
            if not matches:
                return None
            return max(matches, key=lambda r: r.created_at)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            positive = sum(1 for r in self._results if r.delta > 0)
            return {
                "total_tests": len(self._results),
                "positive_skills": positive,
                "negative_skills": len(self._results) - positive,
                "avg_delta_pct": float(np.mean([r.delta_pct for r in self._results])) if self._results else 0.0,
                "pending_tests": len(self._pending_tests),
                "mode": self.mode.value,
            }


def _norm_cdf_approx(x: float) -> float:
    """标准正态分布 CDF 近似（Abramowitz and Stegun 7.1.26）。"""
    if x < 0:
        return 1.0 - _norm_cdf_approx(-x)
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.3275911
    t = 1.0 / (1.0 + p * x)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2.0)
    return y if y > 0 else 0.0


# ============================================================================
# Orchestrator
# ============================================================================

class SkillLearningLoop:
    """技能持续学习闭环编排器。

    整合 TrajectoryCollector / SkillExtractor / SkillGeneralizer /
    CrossModelTransfer / SkillBenchmark 为统一学习管道。
    """

    def __init__(
        self,
        skill_storage_dir: str = "",
        benchmark_mode: BenchmarkMode = BenchmarkMode.AB_TEST,
    ):
        self.collector = TrajectoryCollector()
        self.extractor = SkillExtractor()
        self.generalizer = SkillGeneralizer()
        self.transfer = CrossModelTransfer()
        self.benchmark = SkillBenchmark(mode=benchmark_mode)
        self.skill_storage_dir = skill_storage_dir

        self._lock = threading.RLock()
        self._all_skills: Dict[str, Skill] = {}

    def learn_from_trajectory(
        self,
        trajectory: Trajectory,
        generalize: bool = True,
    ) -> List[Skill]:
        """从轨迹学习技能：提取 → 泛化 → 注册。"""
        extracted = self.extractor.extract_from_trajectory(trajectory)
        result_skills = []

        with self._lock:
            for skill in extracted:
                self._all_skills[skill.skill_id] = skill

                if generalize and self.generalizer.is_generalizable(skill):
                    generalized = self.generalizer.generalize(skill)
                    self._all_skills[generalized.skill_id] = generalized
                    result_skills.append(generalized)
                else:
                    result_skills.append(skill)

        return result_skills

    def save_skill_to_disk(self, skill_id: str, output_dir: str) -> Optional[str]:
        """将技能保存为 .md 文件到磁盘。"""
        skill = self._all_skills.get(skill_id)
        if skill is None:
            return None
        markdown = skill.to_markdown()
        safe_name = re.sub(r'[^\w\-.]', '_', skill.name)[:60]
        output_path = os.path.join(output_dir, f"{safe_name}_v{skill.version}.md")
        try:
            os.makedirs(output_dir, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(markdown)
            return output_path
        except (OSError, IOError) as e:
            logger.error(f"Failed to save skill {skill_id}: {e}")
            return None

    def statistics(self) -> Dict[str, Any]:
        """返回闭环整体统计。"""
        with self._lock:
            return {
                "total_skills": len(self._all_skills),
                "collector": self.collector.statistics(),
                "extractor": self.extractor.statistics(),
                "generalizer": self.generalizer.statistics(),
                "transfer": self.transfer.statistics(),
                "benchmark": self.benchmark.statistics(),
            }
