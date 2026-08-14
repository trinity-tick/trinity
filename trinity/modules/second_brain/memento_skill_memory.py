"""
MementoSkillMemory — Memento-Skills Read-Write-Reflection Learning Loop
========================================================================
arXiv 2603.18743 · P42-3

实现 Memento-Skills 反射学习记忆: read_write_reflection_loop 匹配→执行→
反思→回写技能库的完整反思学习循环, skill_folder_creation Agent自主创建
新技能文件夹从原子组合复合技能, skill_testing 自动测试验证有效性,
zero_parameter_growth 全程零参数更新。

设计要点:
  - ReadWriteReflectionLoop: Read(匹配)→Write(回写)→Reflection(反思)
  - SkillFolderCreation: 原子→复合技能自主发现
  - SkillTesting: 新技能自动测试验证
  - ZeroParameterGrowth: 所有适应来自技能记忆读写
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

class SkillStatus(Enum):
    """技能状态。"""
    DRAFT = auto()
    TESTED = auto()
    VERIFIED = auto()
    DEPRECATED = auto()


class ReflectionOutcome(Enum):
    """反思结果。"""
    IMPROVED = auto()
    RETIRED = auto()
    MERGED = auto()
    KEPT = auto()


class SkillType(Enum):
    """技能类型。"""
    ATOMIC = auto()     # 原子技能
    COMPOSITE = auto()  # 复合技能
    META = auto()       # 元技能 (操作其他技能)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class SkillRecord:
    """一条技能记忆记录。"""
    skill_id: str
    name: str
    description: str
    skill_type: SkillType = SkillType.ATOMIC
    status: SkillStatus = SkillStatus.DRAFT
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    instructions: List[str] = field(default_factory=list)
    parent_skills: List[str] = field(default_factory=list)  # 复合技能的来源
    success_count: int = 0
    failure_count: int = 0
    version: int = 1
    folder: str = ""  # 技能文件夹路径
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class ReflectionLog:
    """反思日志——记录一次 Read-Write-Reflection 循环。"""
    log_id: str
    skill_id: str
    task_input: Dict[str, Any]
    task_output: Dict[str, Any]
    was_successful: bool
    outcome: ReflectionOutcome
    insights: List[str] = field(default_factory=list)
    adjustments: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class SkillFolder:
    """技能文件夹——原子技能组合的容器。"""
    folder_id: str
    name: str
    description: str
    skills: List[str] = field(default_factory=list)  # skill_ids
    parent_folder: Optional[str] = None
    created_at: float = field(default_factory=time.time)


@dataclass
class SkillTestResult:
    """技能测试结果。"""
    test_id: str
    skill_id: str
    passed: bool
    test_cases: int = 0
    passed_cases: int = 0
    errors: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ReadWriteReflectionLoop
# ---------------------------------------------------------------------------

class ReadWriteReflectionLoop:
    """Read-匹配技能 → 执行 → Reflection-反思 → Write-回写技能库。

    Parameters
    ----------
    max_logs : int
        最大反思日志数。
    """

    def __init__(self, max_logs: int = 500) -> None:
        self.max_logs = max_logs
        self._logs: deque = deque(maxlen=max_logs)
        self._loop_count: int = 0
        self._lock = threading.RLock()

    def execute_loop(
        self,
        skill: SkillRecord,
        task_input: Dict[str, Any],
        skill_executor: Callable[[SkillRecord, Dict[str, Any]], Dict[str, Any]],
    ) -> Tuple[Dict[str, Any], ReflectionLog]:
        """执行一次完整的 Read-Write-Reflection 循环。

        Read: 匹配技能 (已完成, skill 已传入)
        Execute: 执行技能
        Reflection: 反思结果
        Write: 修正回写

        Parameters
        ----------
        skill : SkillRecord
            匹配到的技能。
        task_input : Dict[str, Any]
            任务输入。
        skill_executor : Callable
            技能执行器。

        Returns
        -------
        Tuple[Dict, ReflectionLog]
            (执行输出, 反思日志)。
        """
        with self._lock:
            self._loop_count += 1

            # Execute
            try:
                output = skill_executor(skill, task_input)
                was_successful = output.get("success", True)
            except Exception as e:
                output = {"success": False, "error": str(e)}
                was_successful = False

            # Reflection
            insights, outcome = self._reflect(skill, task_input, output, was_successful)

            # Write — 更新技能记忆
            adjustments = self._write_back(skill, was_successful, insights)

            log = ReflectionLog(
                log_id=f"rwr_{self._loop_count}_{int(time.time()*1e6)}",
                skill_id=skill.skill_id,
                task_input=task_input,
                task_output=output,
                was_successful=was_successful,
                outcome=outcome,
                insights=insights,
                adjustments=adjustments,
            )
            self._logs.append(log)
            return output, log

    def _reflect(
        self,
        skill: SkillRecord,
        task_input: Dict[str, Any],
        output: Dict[str, Any],
        was_successful: bool,
    ) -> Tuple[List[str], ReflectionOutcome]:
        """反思执行结果。"""
        insights: List[str] = []

        if was_successful:
            insights.append(f"Skill '{skill.name}' executed successfully")
            if skill.success_count > 3:
                insights.append("Skill is stable — consider promoting to VERIFIED")
            return insights, ReflectionOutcome.KEPT
        else:
            error = output.get("error", "unknown error")
            insights.append(f"Skill failed: {error}")

            if skill.failure_count > skill.success_count:
                insights.append("Skill has more failures than successes — consider retirement")
                return insights, ReflectionOutcome.RETIRED

            insights.append("Minor failure — skill needs adjustment")
            return insights, ReflectionOutcome.IMPROVED

    def _write_back(
        self, skill: SkillRecord, was_successful: bool, insights: List[str]
    ) -> Dict[str, Any]:
        """回写修改到技能记录。"""
        adjustments: Dict[str, Any] = {}
        if was_successful:
            skill.success_count += 1
        else:
            skill.failure_count += 1

        skill.updated_at = time.time()

        if not was_successful and skill.status == SkillStatus.VERIFIED:
            skill.status = SkillStatus.DRAFT
            adjustments["status"] = "VERIFIED→DRAFT"

        return adjustments

    def statistics(self) -> Dict[str, Any]:
        return {"total_loops": self._loop_count, "logs": len(self._logs)}


# ---------------------------------------------------------------------------
# SkillFolderCreation
# ---------------------------------------------------------------------------

class SkillFolderCreation:
    """Agent自主创建新技能文件夹，从原子技能组合出复合技能。

    Parameters
    ----------
    max_folders : int
        最大文件夹数。
    """

    def __init__(self, max_folders: int = 50) -> None:
        self.max_folders = max_folders
        self._folders: Dict[str, SkillFolder] = {}
        self._folder_count: int = 0
        self._lock = threading.RLock()

    def create_folder(self, name: str, description: str = "") -> SkillFolder:
        """创建新技能文件夹。"""
        with self._lock:
            if len(self._folders) >= self.max_folders:
                oldest = min(self._folders.items(), key=lambda x: x[1].created_at)
                del self._folders[oldest[0]]

            self._folder_count += 1
            folder = SkillFolder(
                folder_id=f"folder_{self._folder_count}_{int(time.time()*1e6)}",
                name=name,
                description=description,
            )
            self._folders[folder.folder_id] = folder
            return folder

    def compose_composite_skill(
        self,
        name: str,
        description: str,
        atomic_skills: List[SkillRecord],
        folder_id: Optional[str] = None,
    ) -> Tuple[SkillRecord, SkillFolder]:
        """从原子技能组合复合技能——自动创建文件夹。

        Parameters
        ----------
        name : str
            复合技能名称。
        description : str
            复合技能描述。
        atomic_skills : List[SkillRecord]
            组成该复合技能的原子技能列表。
        folder_id : Optional[str]
            指定文件夹, 不传则自动创建。

        Returns
        -------
        Tuple[SkillRecord, SkillFolder]
            (复合技能记录, 技能文件夹)。
        """
        with self._lock:
            if folder_id:
                folder = self._folders.get(folder_id)
                if not folder:
                    folder = self.create_folder(name, description)
                folder_id = folder.folder_id
            else:
                folder = self.create_folder(name, description)
                folder_id = folder.folder_id

            # 复合技能的输入/输出是原子技能的并集
            composite_inputs: Dict[str, Any] = {}
            composite_outputs: Dict[str, Any] = {}
            composite_instructions: List[str] = []

            for i, skill in enumerate(atomic_skills):
                composite_inputs.update(skill.inputs)
                composite_outputs.update(skill.outputs)
                composite_instructions.append(f"Step {i+1}: {skill.name} — {skill.description}")
                folder.skills.append(skill.skill_id)

            composite = SkillRecord(
                skill_id=f"comp_{self._folder_count}_{int(time.time()*1e6)}",
                name=name,
                description=description,
                skill_type=SkillType.COMPOSITE,
                inputs=composite_inputs,
                outputs=composite_outputs,
                instructions=composite_instructions,
                parent_skills=[s.skill_id for s in atomic_skills],
                folder=folder_id,
            )

            return composite, folder

    def get_folder(self, folder_id: str) -> Optional[SkillFolder]:
        return self._folders.get(folder_id)

    def statistics(self) -> Dict[str, Any]:
        return {"total_folders": len(self._folders)}


# ---------------------------------------------------------------------------
# SkillTesting
# ---------------------------------------------------------------------------

class SkillTesting:
    """新技能创建后自动测试验证有效性。

    Parameters
    ----------
    default_test_cases : int
        默认测试用例数。
    """

    def __init__(self, default_test_cases: int = 3) -> None:
        self.default_test_cases = default_test_cases
        self._results: Dict[str, List[SkillTestResult]] = defaultdict(list)
        self._lock = threading.RLock()

    def test_skill(
        self,
        skill: SkillRecord,
        test_cases: Optional[List[Dict[str, Any]]] = None,
        skill_executor: Optional[Callable[[SkillRecord, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> SkillTestResult:
        """对新技能进行自动测试。

        Parameters
        ----------
        skill : SkillRecord
            待测试技能。
        test_cases : Optional[List[Dict]]
            测试用例; 不传则用默认空测试。
        skill_executor : Optional[Callable]
            技能执行器。

        Returns
        -------
        SkillTestResult
        """
        with self._lock:
            cases = test_cases or [{"test_input": "default"} for _ in range(self.default_test_cases)]

            passed = 0
            errors: List[str] = []

            for i, case in enumerate(cases):
                try:
                    if skill_executor:
                        result = skill_executor(skill, case)
                        if result.get("success", True):
                            passed += 1
                        else:
                            errors.append(f"Case {i}: {result.get('error', 'unknown')}")
                    else:
                        # 无执行器时仅做结构校验
                        if skill.instructions:
                            passed += 1
                        else:
                            errors.append(f"Case {i}: no instructions defined")
                except Exception as e:
                    errors.append(f"Case {i}: {e}")

            result = SkillTestResult(
                test_id=f"test_{int(time.time()*1e6)}",
                skill_id=skill.skill_id,
                passed=passed == len(cases),
                test_cases=len(cases),
                passed_cases=passed,
                errors=errors,
            )

            self._results[skill.skill_id].append(result)

            # 更新技能状态
            if result.passed:
                skill.status = SkillStatus.TESTED
            else:
                skill.status = SkillStatus.DRAFT

            return result

    def get_results(self, skill_id: str) -> List[SkillTestResult]:
        return self._results.get(skill_id, [])

    def statistics(self) -> Dict[str, Any]:
        total = sum(len(v) for v in self._results.values())
        return {"total_tests": total, "skills_tested": len(self._results)}


# ---------------------------------------------------------------------------
# MementoSkillMemory
# ---------------------------------------------------------------------------

class MementoSkillMemory:
    """Memento-Skills 反射学习记忆系统 (Zero-Parameter Growth)。

    Parameters
    ----------
    max_folders : int
        最大技能文件夹数。
    max_logs : int
        最大反思日志数。
    """

    def __init__(self, max_folders: int = 50, max_logs: int = 500) -> None:
        self._skills: Dict[str, SkillRecord] = {}
        self.read_write_reflection_loop = ReadWriteReflectionLoop(max_logs=max_logs)
        self.skill_folder_creation = SkillFolderCreation(max_folders=max_folders)
        self.skill_testing = SkillTesting()
        self._lock = threading.RLock()
        self._skill_count: int = 0

        logger.info("MementoSkillMemory initialized [folders=%d logs=%d]", max_folders, max_logs)

    # ------------------------------------------------------------------
    # Skill CRUD
    # ------------------------------------------------------------------

    def create_skill(
        self,
        name: str,
        description: str,
        inputs: Optional[Dict[str, Any]] = None,
        outputs: Optional[Dict[str, Any]] = None,
        instructions: Optional[List[str]] = None,
        skill_type: SkillType = SkillType.ATOMIC,
    ) -> SkillRecord:
        """创建新技能 (零参数增长——所有信息存入记忆)。"""
        with self._lock:
            self._skill_count += 1
            skill = SkillRecord(
                skill_id=f"skill_{self._skill_count}_{int(time.time()*1e6)}",
                name=name,
                description=description,
                skill_type=skill_type,
                inputs=inputs or {},
                outputs=outputs or {},
                instructions=instructions or [],
            )
            self._skills[skill.skill_id] = skill
            return skill

    def run_skill(
        self,
        skill_id: str,
        task_input: Dict[str, Any],
        skill_executor: Optional[Callable[[SkillRecord, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Tuple[Dict[str, Any], ReflectionLog]:
        """执行技能并触发 Read-Write-Reflection 循环。

        Returns
        -------
        Tuple[Dict, ReflectionLog]
            (执行输出, 反思日志)。
        """
        skill = self._skills.get(skill_id)
        if not skill:
            return {"success": False, "error": "Skill not found"}, ReflectionLog(
                log_id="err", skill_id=skill_id, task_input=task_input,
                task_output={}, was_successful=False, outcome=ReflectionOutcome.RETIRED,
            )

        def default_executor(s: SkillRecord, inp: Dict[str, Any]) -> Dict[str, Any]:
            return {"success": True, "skill": s.name, "input": inp, "instructions": s.instructions}

        executor = skill_executor or default_executor
        return self.read_write_reflection_loop.execute_loop(skill, task_input, executor)

    def compose_and_test(
        self,
        name: str,
        description: str,
        atomic_skill_ids: List[str],
    ) -> Tuple[Optional[SkillRecord], Optional[SkillTestResult]]:
        """组合原子技能为复合技能并自动测试。"""
        atomic_skills = [self._skills[sid] for sid in atomic_skill_ids if sid in self._skills]
        if len(atomic_skills) != len(atomic_skill_ids):
            return None, None

        composite, folder = self.skill_folder_creation.compose_composite_skill(
            name, description, atomic_skills,
        )
        self._skills[composite.skill_id] = composite

        # 自动测试
        test_result = self.skill_testing.test_skill(composite)
        return composite, test_result

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_skills": len(self._skills),
                "loops": self.read_write_reflection_loop.statistics()["total_loops"],
                "folders": self.skill_folder_creation.statistics()["total_folders"],
                "tests": self.skill_testing.statistics()["total_tests"],
            }
