"""
# status: orphan (2026-08-15 audit, not in runtime path)
CurriculumGuidedMemory — DSMentor Difficulty-Sorted Curriculum Learning
=======================================================================
ACL 2026 SURGeLLM Workshop · P40-1

实现 DSMentor 课程引导记忆: organize_by_difficulty() 按隐含难度系数排序,
retain_experience() 将经验写入长期记忆供后续检索, mentor_guidance() 基于历史
经验引导推理, curriculum_step() 从易到难逐步推进。

设计要点:
  - DifficultyRanker: 估计任务难度 (基于多维特征)
  - ExperienceBuffer: 增长式在线经验累积, 完成即写入
  - MentorGuider: 检索历史经验引导当前推理
  - CurriculumPlan: 课程推进路径 (易→难)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class DSMDifficultyLevel(Enum):
    """DSMentor 难度等级 (重命名: DifficultyLevel→DSMDifficultyLevel)。"""
    BEGINNER = auto()
    EASY = auto()
    MEDIUM = auto()
    HARD = auto()
    EXPERT = auto()


class ExperienceCategory(Enum):
    """经验类别。"""
    SUCCESS = auto()
    FAILURE = auto()
    PARTIAL = auto()
    EXPLORATION = auto()


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class ExperienceRecord:
    """一条经验记录——完成某个任务后的反思。"""
    record_id: str
    task_description: str
    category: ExperienceCategory
    solution: str
    difficulty: DSMDifficultyLevel
    score: float = 0.0
    key_insights: List[str] = field(default_factory=list)
    retrieval_tags: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CurriculumStep:
    """课程推进的一步——单个任务的调度。"""
    step_id: str
    index: int
    task_description: str
    difficulty: DSMDifficultyLevel
    estimated_score: float = 0.0
    completed: bool = False
    experience_id: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class CurriculumPlan:
    """课程计划——从易到难的完整推进路径。"""
    plan_id: str
    steps: List[CurriculumStep] = field(default_factory=list)
    current_index: int = 0
    total_steps: int = 0
    created_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# DifficultyRanker
# ---------------------------------------------------------------------------

class DifficultyRanker:
    """难度排序器——基于多维特征估计任务隐含难度。

    Parameters
    ----------
    features : List[str]
        用于排序的特征维度 (如 ["complexity", "context_deps", "step_count"])。
    """

    def __init__(self, features: Optional[List[str]] = None) -> None:
        self.features = features or ["complexity", "context_deps", "step_count", "tool_usage"]
        self._feature_weights: Dict[str, float] = {
            "complexity": 0.4,
            "context_deps": 0.25,
            "step_count": 0.2,
            "tool_usage": 0.15,
        }
        self._lock = threading.RLock()

    def estimate_difficulty(self, task_features: Dict[str, float]) -> Tuple[DSMDifficultyLevel, float]:
        """根据特征向量估计难度等级。

        Returns
        -------
        Tuple[DSMDifficultyLevel, float]
            (难度等级, 归一化分数 0.0~1.0)。
        """
        score = 0.0
        total_weight = 0.0
        for feat, value in task_features.items():
            w = self._feature_weights.get(feat, 0.1)
            score += w * float(value)
            total_weight += w
        if total_weight > 0:
            score /= total_weight
        score = float(np.clip(score, 0.0, 1.0))

        if score < 0.2:
            return DSMDifficultyLevel.BEGINNER, score
        elif score < 0.4:
            return DSMDifficultyLevel.EASY, score
        elif score < 0.6:
            return DSMDifficultyLevel.MEDIUM, score
        elif score < 0.8:
            return DSMDifficultyLevel.HARD, score
        return DSMDifficultyLevel.EXPERT, score

    def organize_by_difficulty(
        self,
        tasks: List[Dict[str, Any]],
    ) -> List[CurriculumStep]:
        """按隐含难度系数排序任务。

        Parameters
        ----------
        tasks : List[Dict[str, Any]]
            每个任务含 "description" (str) 与可选 "features" (Dict[str, float])。

        Returns
        -------
        List[CurriculumStep]
            按难度递增排序的任务步骤。
        """
        with self._lock:
            scored: List[Tuple[CurriculumStep, float]] = []

            for i, task in enumerate(tasks):
                feats = task.get("features", {})
                level, score = self.estimate_difficulty(feats)

                step = CurriculumStep(
                    step_id=f"step_{i}_{int(time.time()*1e6)}",
                    index=i,
                    task_description=task.get("description", f"task_{i}"),
                    difficulty=level,
                    estimated_score=score,
                )
                scored.append((step, score))

            scored.sort(key=lambda x: x[1])
            return [s for s, _ in scored]


# ---------------------------------------------------------------------------
# CurriculumGuidedMemory
# ---------------------------------------------------------------------------

class CurriculumGuidedMemory:
    """DSMentor 课程引导记忆系统。

    核心流程: rank tasks → execute easy-to-hard → retain experience → mentor future。

    Parameters
    ----------
    buffer_capacity : int
        经验缓冲区最大容量。
    retrieval_k : int
        mentor_guidance 时检索的 top-k 经验数。
    """

    def __init__(self, buffer_capacity: int = 256, retrieval_k: int = 5) -> None:
        self.buffer_capacity = buffer_capacity
        self.retrieval_k = retrieval_k
        self._ranker = DifficultyRanker()
        self._experiences: deque = deque(maxlen=buffer_capacity)
        self._lock = threading.RLock()
        self._experience_count: int = 0
        self._current_plan: Optional[CurriculumPlan] = None

        logger.info("CurriculumGuidedMemory initialized [buf=%d k=%d]", buffer_capacity, retrieval_k)

    # ------------------------------------------------------------------
    # Curriculum Planning
    # ------------------------------------------------------------------

    def organize_by_difficulty(self, tasks: List[Dict[str, Any]]) -> List[CurriculumStep]:
        """按难度排序任务, 生成课程步骤。"""
        return self._ranker.organize_by_difficulty(tasks)

    def curriculum_step(self, tasks: List[Dict[str, Any]]) -> Optional[CurriculumStep]:
        """课程推进——从易到难返回下一步任务。

        Parameters
        ----------
        tasks : List[Dict[str, Any]]
            所有待执行任务。

        Returns
        -------
        Optional[CurriculumStep]
            下一步任务; None 表示全部完成。
        """
        with self._lock:
            if self._current_plan is None or self._current_plan.current_index >= self._current_plan.total_steps:
                # 生成新计划
                steps = self.organize_by_difficulty(tasks)
                self._current_plan = CurriculumPlan(
                    plan_id=f"plan_{int(time.time()*1e6)}",
                    steps=steps,
                    total_steps=len(steps),
                )

            plan = self._current_plan
            if plan.current_index >= plan.total_steps:
                return None

            step = plan.steps[plan.current_index]
            plan.current_index += 1
            return step

    # ------------------------------------------------------------------
    # Experience Retention
    # ------------------------------------------------------------------

    def retain_experience(
        self,
        task_description: str,
        solution: str,
        category: ExperienceCategory,
        difficulty: DSMDifficultyLevel,
        score: float = 0.0,
        insights: Optional[List[str]] = None,
        step_id: Optional[str] = None,
    ) -> ExperienceRecord:
        """完成一个任务后保留经验到长期记忆。

        Parameters
        ----------
        task_description : str
            任务描述。
        solution : str
            解决方案。
        category : ExperienceCategory
            成功/失败/部分/探索。
        difficulty : DSMDifficultyLevel
            难度等级。
        score : float
            完成质量评分 (0~1)。
        insights : Optional[List[str]]
            关键洞察。
        step_id : Optional[str]
            关联的课程步骤 ID。

        Returns
        -------
        ExperienceRecord
            写入的经验记录。
        """
        with self._lock:
            self._experience_count += 1
            record = ExperienceRecord(
                record_id=f"exp_{self._experience_count}_{int(time.time()*1e6)}",
                task_description=task_description,
                category=category,
                solution=solution,
                difficulty=difficulty,
                score=score,
                key_insights=insights or [],
                retrieval_tags=[difficulty.name.lower(), category.name.lower()],
            )
            self._experiences.append(record)

            # 更新课程步骤
            if self._current_plan and step_id:
                for step in self._current_plan.steps:
                    if step.step_id == step_id:
                        step.completed = True
                        step.experience_id = record.record_id
                        break

            logger.info("Experience retained: %s [%s] score=%.2f", record.record_id, category.name, score)
            return record

    # ------------------------------------------------------------------
    # Mentor Guidance
    # ------------------------------------------------------------------

    def mentor_guidance(self, current_task: str, k: Optional[int] = None) -> Dict[str, Any]:
        """基于历史经验引导当前推理。

        Parameters
        ----------
        current_task : str
            当前任务描述。
        k : Optional[int]
            检索的经验数。

        Returns
        -------
        Dict[str, Any]
            {"advice": str, "references": List[ExperienceRecord], "confidence": float}。
        """
        with self._lock:
            k = k or self.retrieval_k
            if not self._experiences:
                return {"advice": "", "references": [], "confidence": 0.0}

            experiences = list(self._experiences)
            # 按相关性 (任务关键词匹配) 排序
            task_words = set(current_task.lower().split())
            scored = []
            for exp in experiences:
                exp_words = set(exp.task_description.lower().split())
                overlap = len(task_words & exp_words) / max(len(task_words | exp_words), 1)
                # 加成: 高分经验更受重视
                score = overlap * 0.7 + exp.score * 0.3
                scored.append((exp, score))

            scored.sort(key=lambda x: x[1], reverse=True)
            top = scored[:k]

            if not top:
                return {"advice": "", "references": [], "confidence": 0.0}

            # 聚合建议
            success_refs = [e for e, s in top if e.category == ExperienceCategory.SUCCESS]
            failure_refs = [e for e, s in top if e.category == ExperienceCategory.FAILURE]

            advice_parts = []
            if success_refs:
                advice_parts.append(f"Similar successful approaches: {'; '.join(e.solution[:80] for e in success_refs[:2])}")
            if failure_refs:
                advice_parts.append(f"Avoid these failed patterns: {'; '.join(e.solution[:80] for e in failure_refs[:2])}")

            confidence = float(np.mean([s for _, s in top]))

            return {
                "advice": " | ".join(advice_parts),
                "references": [e for e, _ in top],
                "confidence": confidence,
            }

    def search_experiences(self, keyword: str) -> List[ExperienceRecord]:
        """按关键词搜索经验。"""
        kw = keyword.lower()
        return [e for e in self._experiences
                if kw in e.task_description.lower() or kw in " ".join(e.retrieval_tags).lower()]

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            cats = {c.name: 0 for c in ExperienceCategory}
            diffs = {d.name: 0 for d in DSMDifficultyLevel}
            avg_score = 0.0
            for e in self._experiences:
                cats[e.category.name] = cats.get(e.category.name, 0) + 1
                diffs[e.difficulty.name] = diffs.get(e.difficulty.name, 0) + 1
                avg_score += e.score
            n = len(self._experiences)
            return {
                "total_experiences": n,
                "category_distribution": cats,
                "difficulty_distribution": diffs,
                "mean_score": avg_score / n if n else 0.0,
                "plan_progress": (
                    f"{self._current_plan.current_index}/{self._current_plan.total_steps}"
                    if self._current_plan else "no plan"
                ),
            }
