"""
SelfEvolvingSkillSystem — MUSE-Autoskill Autonomous Skill Evolution
====================================================================
字节ByteBrain, May 2026 · P42-4

实现 MUSE-Autoskill 自演化技能系统: skill_creation_engine 从任务执行中
自主发现并创建新技能, skill_memory_store 结构化技能记忆存储,
skill_evaluation_loop 自动评估技能质量并迭代优化,
skill_management 版本管理/依赖解析/冲突检测/废弃清理。

设计要点:
  - SkillCreationEngine: 从执行模式中自动发现新技能
  - SkillMemoryStore: 输入/输出/前提/效果结构化存储
  - SkillEvaluationLoop: 自动评估+迭代优化
  - SkillManagement: 版本/依赖/冲突/废弃 全生命周期
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class SkillLifecycle(Enum):
    """技能生命周期状态。"""
    DISCOVERED = auto()
    DRAFT = auto()
    EVALUATING = auto()
    STABLE = auto()
    DEPRECATED = auto()
    RETIRED = auto()


class ConflictType(Enum):
    """冲突类型。"""
    INPUT_CONFLICT = auto()
    OUTPUT_CONFLICT = auto()
    DEPENDENCY_CONFLICT = auto()
    NAMESPACE_CONFLICT = auto()


class MUSE_EvaluationResult:
    """技能评估结果 (重命名: EvaluationResult→MUSE_EvaluationResult 避免冲突)。"""
    def __init__(
        self,
        skill_id: str,
        overall_score: float = 0.0,
        quality_scores: Optional[Dict[str, float]] = None,
        improvement_suggestions: Optional[List[str]] = None,
        passed: bool = False,
    ) -> None:
        self.skill_id = skill_id
        self.overall_score = overall_score
        self.quality_scores = quality_scores or {
            "reliability": 0.0,
            "efficiency": 0.0,
            "generality": 0.0,
            "safety": 0.0,
        }
        self.improvement_suggestions = improvement_suggestions or []
        self.passed = passed
        self.timestamp = time.time()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SkillSpec:
    """技能规格——结构化的输入/输出/前提/效果。"""
    spec_id: str
    skill_id: str
    name: str
    description: str
    inputs: Dict[str, str] = field(default_factory=dict)   # 参数名→类型
    outputs: Dict[str, str] = field(default_factory=dict)   # 输出名→类型
    preconditions: List[str] = field(default_factory=list)  # 前提条件
    effects: List[str] = field(default_factory=list)        # 执行效果
    examples: List[Dict[str, Any]] = field(default_factory=list)
    version: str = "1.0.0"


@dataclass
class SkillVersion:
    """技能版本记录。"""
    version_id: str
    skill_id: str
    version: str
    spec: SkillSpec
    lifecycle: SkillLifecycle = SkillLifecycle.DRAFT
    created_at: float = field(default_factory=time.time)
    changelog: str = ""


@dataclass
class SkillDependency:
    """技能依赖关系。"""
    dep_id: str
    skill_id: str
    depends_on: str        # 依赖的技能ID
    dependency_type: str = "requires"  # requires / enhances / conflicts
    optional: bool = False


@dataclass
class ConflictReport:
    """冲突检测报告。"""
    report_id: str
    skill_a: str
    skill_b: str
    conflict_type: ConflictType
    description: str
    severity: str = "warning"  # warning / error / critical
    resolved: bool = False
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# SkillCreationEngine
# ---------------------------------------------------------------------------

class SkillCreationEngine:
    """从任务执行中自主发现并创建新技能。

    Parameters
    ----------
    discovery_threshold : int
        同一模式重复多少次即触发技能发现。
    """

    def __init__(self, discovery_threshold: int = 3) -> None:
        self.discovery_threshold = discovery_threshold
        self._execution_patterns: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

    def observe_execution(
        self,
        task_name: str,
        inputs: Dict[str, Any],
        outputs: Dict[str, Any],
        success: bool,
    ) -> Optional[SkillSpec]:
        """观察一次任务执行——当模式重复度超过阈值时自动发现新技能。

        Returns
        -------
        Optional[SkillSpec]
            若达到发现阈值则返回建议的技能规格。
        """
        with self._lock:
            pattern_key = _hash_pattern(task_name, inputs)
            if pattern_key not in self._execution_patterns:
                self._execution_patterns[pattern_key] = {
                    "task_name": task_name,
                    "count": 0,
                    "success_count": 0,
                    "input_signature": list(inputs.keys()),
                    "output_signature": list(outputs.keys()),
                    "input_types": {k: type(v).__name__ for k, v in inputs.items()},
                    "output_types": {k: type(v).__name__ for k, v in outputs.items()},
                }

            pattern = self._execution_patterns[pattern_key]
            pattern["count"] += 1
            if success:
                pattern["success_count"] += 1

            if pattern["count"] >= self.discovery_threshold:
                return self._create_skill_spec(pattern)

            return None

    def _create_skill_spec(self, pattern: Dict[str, Any]) -> SkillSpec:
        """从执行模式生成技能规格。"""
        spec = SkillSpec(
            spec_id=f"spec_{int(time.time()*1e6)}",
            skill_id="",  # 创建后分配
            name=pattern["task_name"],
            description=f"Auto-discovered skill for '{pattern['task_name']}'",
            inputs=pattern["input_types"],
            outputs=pattern["output_types"],
            preconditions=[],
            effects=[f"Execute {pattern['task_name']}"],
        )
        logger.info("Auto-discovered skill: %s (observed %d times)", pattern["task_name"], pattern["count"])
        return spec

    def statistics(self) -> Dict[str, Any]:
        return {"tracked_patterns": len(self._execution_patterns)}


# ---------------------------------------------------------------------------
# SkillMemoryStore
# ---------------------------------------------------------------------------

class SkillMemoryStore:
    """结构化技能记忆存储——含输入/输出/前提/效果规格。

    Parameters
    ----------
    capacity : int
        最大技能数。
    """

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self._skills: Dict[str, SkillSpec] = {}
        self._lock = threading.RLock()
        self._skill_count: int = 0

    def store_skill(self, spec: SkillSpec) -> SkillSpec:
        """存储技能规格。"""
        with self._lock:
            if not spec.skill_id:
                self._skill_count += 1
                spec.skill_id = f"sk_{self._skill_count}_{int(time.time()*1e6)}"
                spec.spec_id = spec.skill_id + "_spec"

            if len(self._skills) >= self.capacity:
                oldest = min(self._skills.items(), key=lambda x: 0)
                del self._skills[oldest[0]]

            self._skills[spec.skill_id] = spec
            return spec

    def get_skill(self, skill_id: str) -> Optional[SkillSpec]:
        return self._skills.get(skill_id)

    def search_by_capability(self, required_inputs: Set[str], desired_outputs: Set[str]) -> List[SkillSpec]:
        """按能力检索技能——输入/输出匹配。"""
        results: List[Tuple[SkillSpec, float]] = []
        for skill in self._skills.values():
            score = 0.0
            skill_inputs = set(skill.inputs.keys())
            skill_outputs = set(skill.outputs.keys())

            if required_inputs:
                input_match = len(required_inputs & skill_inputs) / max(len(required_inputs), 1)
                score += input_match * 0.5

            if desired_outputs:
                output_match = len(desired_outputs & skill_outputs) / max(len(desired_outputs), 1)
                score += output_match * 0.5

            if score > 0:
                results.append((skill, score))

        results.sort(key=lambda x: x[1], reverse=True)
        return [r[0] for r in results[:10]]

    def statistics(self) -> Dict[str, Any]:
        return {"total_skills": len(self._skills)}


# ---------------------------------------------------------------------------
# SkillEvaluationLoop
# ---------------------------------------------------------------------------

class SkillEvaluationLoop:
    """自动评估技能质量并迭代优化。

    Parameters
    ----------
    improvement_threshold : float
        低于此分数触发优化建议。
    """

    def __init__(self, improvement_threshold: float = 0.6) -> None:
        self.improvement_threshold = improvement_threshold
        self._eval_history: Dict[str, List[MUSE_EvaluationResult]] = defaultdict(list)
        self._lock = threading.RLock()

    def evaluate(self, skill: SkillSpec, execution_stats: Optional[Dict[str, Any]] = None) -> MUSE_EvaluationResult:
        """评估技能质量。

        Parameters
        ----------
        skill : SkillSpec
            待评估技能。
        execution_stats : Optional[Dict]
            执行统计 (success_rate, avg_latency等)。
        """
        with self._lock:
            stats = execution_stats or {}

            reliability = stats.get("success_rate", 0.8)
            efficiency = 1.0 - min(stats.get("avg_latency", 1.0) / 10.0, 1.0)
            generality = min(len(skill.examples) / 5.0, 1.0) if skill.examples else 0.3
            safety = 0.7  # 默认基础安全分

            overall = (reliability * 0.3 + efficiency * 0.2 + generality * 0.2 + safety * 0.3)

            suggestions: List[str] = []
            if reliability < self.improvement_threshold:
                suggestions.append("Improve reliability: add error handling, validate inputs")
            if efficiency < self.improvement_threshold:
                suggestions.append("Improve efficiency: optimize execution path")
            if generality < self.improvement_threshold:
                suggestions.append("Improve generality: add more diverse examples")
            if len(skill.preconditions) == 0:
                suggestions.append("Define explicit preconditions for robustness")

            result = MUSE_EvaluationResult(
                skill_id=skill.skill_id,
                overall_score=overall,
                quality_scores={
                    "reliability": reliability,
                    "efficiency": efficiency,
                    "generality": generality,
                    "safety": safety,
                },
                improvement_suggestions=suggestions,
                passed=overall >= self.improvement_threshold,
            )

            self._eval_history[skill.skill_id].append(result)
            return result

    def get_improvement_history(self, skill_id: str) -> List[MUSE_EvaluationResult]:
        return self._eval_history.get(skill_id, [])

    def statistics(self) -> Dict[str, Any]:
        total = sum(len(v) for v in self._eval_history.values())
        return {"total_evaluations": total, "skills_evaluated": len(self._eval_history)}


# ---------------------------------------------------------------------------
# SkillManagement
# ---------------------------------------------------------------------------

class SkillManagement:
    """技能版本管理/依赖解析/冲突检测/废弃清理。

    Parameters
    ----------
    max_versions_per_skill : int
        每个技能最大版本数。
    """

    def __init__(self, max_versions_per_skill: int = 10) -> None:
        self.max_versions_per_skill = max_versions_per_skill
        self._versions: Dict[str, Dict[str, SkillVersion]] = defaultdict(dict)
        self._dependencies: Dict[str, List[SkillDependency]] = defaultdict(list)
        self._conflicts: List[ConflictReport] = []
        self._lock = threading.RLock()

    def create_version(self, skill_spec: SkillSpec, changelog: str = "") -> SkillVersion:
        """为技能创建新版本。"""
        with self._lock:
            skill_versions = self._versions[skill_spec.skill_id]
            if len(skill_versions) >= self.max_versions_per_skill:
                oldest = min(skill_versions.items(), key=lambda x: x[1].created_at)
                del skill_versions[oldest[0]]

            version = SkillVersion(
                version_id=f"ver_{int(time.time()*1e6)}",
                skill_id=skill_spec.skill_id,
                version=skill_spec.version,
                spec=skill_spec,
                changelog=changelog,
            )
            skill_versions[skill_spec.version] = version
            return version

    def add_dependency(
        self, skill_id: str, depends_on: str, dep_type: str = "requires", optional: bool = False
    ) -> SkillDependency:
        """添加技能依赖。"""
        with self._lock:
            dep = SkillDependency(
                dep_id=f"dep_{int(time.time()*1e6)}",
                skill_id=skill_id,
                depends_on=depends_on,
                dependency_type=dep_type,
                optional=optional,
            )
            self._dependencies[skill_id].append(dep)
            return dep

    def detect_conflicts(
        self, skill_store: SkillMemoryStore
    ) -> List[ConflictReport]:
        """检测技能间的冲突——输入/输出/依赖冲突。"""
        with self._lock:
            new_conflicts: List[ConflictReport] = []
            specs = list(skill_store._skills.values())

            for i, spec_a in enumerate(specs):
                for spec_b in specs[i + 1:]:
                    # 命名冲突
                    if spec_a.name == spec_b.name:
                        new_conflicts.append(ConflictReport(
                            report_id=f"conf_{int(time.time()*1e6)}",
                            skill_a=spec_a.skill_id,
                            skill_b=spec_b.skill_id,
                            conflict_type=ConflictType.NAMESPACE_CONFLICT,
                            description=f"Same name: {spec_a.name}",
                            severity="error",
                        ))
                        continue

                    # 输入冲突
                    common_inputs = set(spec_a.inputs.keys()) & set(spec_b.inputs.keys())
                    for inp in common_inputs:
                        if spec_a.inputs[inp] != spec_b.inputs[inp]:
                            new_conflicts.append(ConflictReport(
                                report_id=f"conf_{int(time.time()*1e6)}",
                                skill_a=spec_a.skill_id,
                                skill_b=spec_b.skill_id,
                                conflict_type=ConflictType.INPUT_CONFLICT,
                                description=f"Input '{inp}' type mismatch: {spec_a.inputs[inp]} vs {spec_b.inputs[inp]}",
                                severity="warning",
                            ))

            self._conflicts.extend(new_conflicts)
            return new_conflicts

    def deprecate_skill(
        self, skill_id: str, store: SkillMemoryStore
    ) -> bool:
        """废弃技能——标记为生命周期结束。"""
        with self._lock:
            skill = store.get_skill(skill_id)
            if skill:
                # 检查依赖它的技能
                dependents = [
                    sid for sid, deps in self._dependencies.items()
                    if any(d.depends_on == skill_id and not d.optional for d in deps)
                ]
                if dependents:
                    logger.warning("Cannot deprecate %s: still depended by %s", skill_id, dependents)
                    return False

                # 标记所有版本为 RETIRED
                for ver_dict in self._versions.get(skill_id, {}).values():
                    ver_dict.lifecycle = SkillLifecycle.RETIRED

                return True
            return False

    def resolve_dependency_chain(
        self, skill_id: str, resolved: Optional[Set[str]] = None
    ) -> List[str]:
        """解析技能完整依赖链——拓扑排序。"""
        if resolved is None:
            resolved = set()
        if skill_id in resolved:
            return []

        resolved.add(skill_id)
        chain = [skill_id]

        for dep in self._dependencies.get(skill_id, []):
            if dep.dependency_type == "requires":
                sub_chain = self.resolve_dependency_chain(dep.depends_on, resolved)
                chain = sub_chain + chain

        return chain

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_versions": sum(len(v) for v in self._versions.values()),
            "total_dependencies": sum(len(d) for d in self._dependencies.values()),
            "total_conflicts": len(self._conflicts),
        }


# ---------------------------------------------------------------------------
# SelfEvolvingSkillSystem
# ---------------------------------------------------------------------------

class SelfEvolvingSkillSystem:
    """MUSE-Autoskill 自演化技能系统。

    Parameters
    ----------
    store_capacity : int
        技能存储容量。
    discovery_threshold : int
        模式重复发现阈值。
    improvement_threshold : float
        改进触发阈值。
    """

    def __init__(
        self,
        store_capacity: int = 200,
        discovery_threshold: int = 3,
        improvement_threshold: float = 0.6,
    ) -> None:
        self.skill_creation_engine = SkillCreationEngine(
            discovery_threshold=discovery_threshold,
        )
        self.skill_memory_store = SkillMemoryStore(capacity=store_capacity)
        self.skill_evaluation_loop = SkillEvaluationLoop(
            improvement_threshold=improvement_threshold,
        )
        self.skill_management = SkillManagement()
        self._lock = threading.RLock()

        logger.info(
            "SelfEvolvingSkillSystem initialized [store=%d disc=%d imp=%.2f]",
            store_capacity, discovery_threshold, improvement_threshold,
        )

    def observe_and_evolve(
        self,
        task_name: str,
        inputs: Dict[str, Any],
        outputs: Dict[str, Any],
        success: bool,
    ) -> Dict[str, Any]:
        """观察执行并触发技能发现+评估+优化全流程。

        Returns
        -------
        Dict[str, Any]
            包含所有进展的汇总。
        """
        # 1. 技能发现
        spec = self.skill_creation_engine.observe_execution(task_name, inputs, outputs, success)
        if spec:
            self.skill_memory_store.store_skill(spec)
            self.skill_management.create_version(spec)

            # 2. 自动评估
            eval_result = self.skill_evaluation_loop.evaluate(spec, {
                "success_rate": 1.0 if success else 0.5,
            })

            # 3. 冲突检测
            conflicts = self.skill_management.detect_conflicts(self.skill_memory_store)

            return {
                "discovered": True,
                "skill_id": spec.skill_id,
                "score": eval_result.overall_score,
                "passed": eval_result.passed,
                "conflicts": len(conflicts),
                "suggestions": eval_result.improvement_suggestions,
            }

        return {"discovered": False}

    def search_skills(self, required_inputs: Set[str], desired_outputs: Set[str]) -> List[SkillSpec]:
        """按能力检索技能。"""
        return self.skill_memory_store.search_by_capability(required_inputs, desired_outputs)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "skills": self.skill_memory_store.statistics()["total_skills"],
                "patterns": self.skill_creation_engine.statistics()["tracked_patterns"],
                "evaluations": self.skill_evaluation_loop.statistics()["total_evaluations"],
                "versions": self.skill_management.statistics()["total_versions"],
                "dependencies": self.skill_management.statistics()["total_dependencies"],
                "conflicts": self.skill_management.statistics()["total_conflicts"],
            }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_pattern(task_name: str, inputs: Dict[str, Any]) -> str:
    import hashlib, json
    sig = {"task": task_name, "input_keys": sorted(inputs.keys())}
    s = json.dumps(sig, sort_keys=True, default=str)
    return hashlib.md5(s.encode()).hexdigest()
