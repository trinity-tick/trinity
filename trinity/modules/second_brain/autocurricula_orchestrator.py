"""
M113 AutoCurriculaOrchestrator — 自动课程编排

基于 Open-ended Multi-agent Autocurricula via Visual Inspection (arXiv 2607.08193, 7月9日)

核心能力：
- 基于 agent 当前能力自动生成训练任务
- 难度渐进课程：从简单到复杂自动排序
- 多 agent 协同课程：共享经验池 + 交叉验证
- 与 M108 RecursiveSearchOrchestrator 互补集成
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# 枚举与常量
# ---------------------------------------------------------------------------

class DifficultyLevel(Enum):
    TRIVIAL = 1
    EASY = 2
    MEDIUM = 3
    HARD = 4
    EXPERT = 5


class TaskStatus(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    FAILED = auto()
    SKIPPED = auto()


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------

@dataclass
class CurriculumTask:
    """课程任务"""
    task_id: str
    description: str
    difficulty: DifficultyLevel = DifficultyLevel.EASY
    estimated_difficulty: float = 0.5      # 0.0 ~ 1.0
    prerequisites: List[str] = field(default_factory=list)
    skills_required: List[str] = field(default_factory=list)
    skills_trained: List[str] = field(default_factory=list)
    success_rate: float = 0.0
    attempts: int = 0
    completions: int = 0
    avg_time_seconds: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "description": self.description,
            "difficulty": self.difficulty.name,
            "estimated_difficulty": self.estimated_difficulty,
            "prerequisites": self.prerequisites,
            "skills_required": self.skills_required,
            "skills_trained": self.skills_trained,
            "success_rate": round(self.success_rate, 4),
            "attempts": self.attempts,
            "completions": self.completions,
            "avg_time_seconds": round(self.avg_time_seconds, 2),
        }


@dataclass
class AgentCapability:
    """Agent 能力画像"""
    agent_id: str
    skill_scores: Dict[str, float] = field(default_factory=dict)
    completed_tasks: Set[str] = field(default_factory=set)
    failed_tasks: Set[str] = field(default_factory=set)
    total_attempts: int = 0
    global_score: float = 0.0

    def update_skill(self, skill: str, score: float):
        current = self.skill_scores.get(skill, 0.5)
        # EMA 平滑更新
        alpha = 0.3
        self.skill_scores[skill] = alpha * score + (1 - alpha) * current

    def record_result(self, task_id: str, success: bool):
        self.total_attempts += 1
        if success:
            self.completed_tasks.add(task_id)
            self.failed_tasks.discard(task_id)
        else:
            self.failed_tasks.add(task_id)
        self._recompute_global()

    def _recompute_global(self):
        if not self.skill_scores:
            self.global_score = 0.5
        else:
            self.global_score = float(np.mean(list(self.skill_scores.values())))


# ---------------------------------------------------------------------------
# TaskDifficultyEstimator
# ---------------------------------------------------------------------------

class TaskDifficultyEstimator:
    """
    基于历史成功率的任务难度估算器。

    使用 Beta 先验 MAP 估计历史成功率，
    并结合先验难度标签输出平滑难度值。
    """

    def __init__(self, prior_alpha: float = 1.0, prior_beta: float = 1.0):
        self.prior_alpha = prior_alpha
        self.prior_beta = prior_beta

    def estimate(
        self,
        task: CurriculumTask,
        global_avg_success: float = 0.5,
    ) -> float:
        """
        Beta-Binomial posterior mean 作为平滑成功率。
        difficulty = 1 - smoothed_success_rate
        """
        alpha = self.prior_alpha + task.completions
        beta = self.prior_beta + (task.attempts - task.completions)
        smoothed_success = alpha / (alpha + beta) if (alpha + beta) > 0 else global_avg_success
        difficulty = 1.0 - smoothed_success
        return max(0.0, min(1.0, difficulty))

    def difficulty_to_level(self, difficulty_score: float) -> DifficultyLevel:
        if difficulty_score < 0.2:
            return DifficultyLevel.TRIVIAL
        elif difficulty_score < 0.4:
            return DifficultyLevel.EASY
        elif difficulty_score < 0.6:
            return DifficultyLevel.MEDIUM
        elif difficulty_score < 0.8:
            return DifficultyLevel.HARD
        else:
            return DifficultyLevel.EXPERT


# ---------------------------------------------------------------------------
# CurriculumOrchestrator
# ---------------------------------------------------------------------------

class CurriculumOrchestrator:
    """
    自动课程编排器。

    流程：
    1. 分析 agent 能力 → 识别薄弱技能
    2. 生成候选任务池
    3. 按 difficulty 排序 (progressive curriculum)
    4. 分配任务给 agent，监控进展
    """

    def __init__(
        self,
        task_pool: Optional[List[CurriculumTask]] = None,
        estimator: Optional[TaskDifficultyEstimator] = None,
    ):
        self.task_pool: List[CurriculumTask] = task_pool or []
        self.estimator = estimator or TaskDifficultyEstimator()
        self.agent_capabilities: Dict[str, AgentCapability] = {}
        self.task_index: Dict[str, CurriculumTask] = {}

        for t in self.task_pool:
            self.task_index[t.task_id] = t

    # ------------------------------------------------------------------
    # Agent 管理
    # ------------------------------------------------------------------
    def register_agent(
        self,
        agent_id: str,
        initial_skills: Optional[Dict[str, float]] = None,
    ) -> AgentCapability:
        cap = AgentCapability(
            agent_id=agent_id,
            skill_scores=initial_skills or {},
        )
        self.agent_capabilities[agent_id] = cap
        return cap

    # ------------------------------------------------------------------
    # 任务池管理
    # ------------------------------------------------------------------
    def add_task(self, task: CurriculumTask):
        self.task_pool.append(task)
        self.task_index[task.task_id] = task

    def add_tasks(self, tasks: List[CurriculumTask]):
        for t in tasks:
            self.add_task(t)

    # ------------------------------------------------------------------
    # 生成课程
    # ------------------------------------------------------------------
    def generate_curriculum(
        self,
        agent_id: str,
        max_tasks: int = 10,
        focus_skills: Optional[List[str]] = None,
        max_difficulty: Optional[DifficultyLevel] = None,
    ) -> List[CurriculumTask]:
        """
        为指定 agent 生成渐进式课程。

        策略：
        - 先按 agent 当前能力筛掉太难的
        - 薄弱技能对应的任务优先
        - 按 difficulty 升序排列 (progressive)
        - 前置依赖满足的任务优先
        """
        cap = self.agent_capabilities.get(agent_id)
        if cap is None:
            return []

        # 识别薄弱技能 (得分 < 0.4)
        weak_skills = {
            s for s, score in cap.skill_scores.items() if score < 0.4
        }
        if focus_skills:
            weak_skills = weak_skills & set(focus_skills) or set(focus_skills)

        candidates: List[Tuple[float, CurriculumTask]] = []
        for task in self.task_pool:
            # 已完成的不再推荐
            if task.task_id in cap.completed_tasks:
                continue

            # 难度限制
            est_diff = self.estimator.estimate(task)
            if max_difficulty and self.estimator.difficulty_to_level(est_diff).value > max_difficulty.value:
                continue

            # 前置依赖检查
            prereqs_met = all(p in cap.completed_tasks for p in task.prerequisites)
            if not prereqs_met:
                continue

            # 评分：薄弱技能匹配越多 → 优先级越高
            skill_match = len(set(task.skills_trained) & weak_skills)
            # 难度适中奖励 (不推荐太简单)
            difficulty_reward = min(est_diff, 0.7)
            score = skill_match * 0.6 + difficulty_reward * 0.4
            candidates.append((score, task))

        # 按 score 降序 → 选 top max_tasks → 按 difficulty 升序重排
        candidates.sort(key=lambda x: -x[0])
        selected = [t for _, t in candidates[:max_tasks]]
        selected.sort(key=lambda t: self.estimator.estimate(t))
        return selected

    # ------------------------------------------------------------------
    # 记录结果并更新能力
    # ------------------------------------------------------------------
    def record_result(
        self,
        agent_id: str,
        task_id: str,
        success: bool,
        time_seconds: float = 0.0,
    ):
        cap = self.agent_capabilities.get(agent_id)
        if cap is None:
            return

        cap.record_result(task_id, success)

        task = self.task_index.get(task_id)
        if task is None:
            return

        # 更新任务统计
        task.attempts += 1
        if success:
            task.completions += 1
            task.avg_time_seconds = (
                (task.avg_time_seconds * (task.completions - 1) + time_seconds)
                / task.completions
            )
        task.success_rate = task.completions / task.attempts if task.attempts > 0 else 0.0

        # 更新 agent 技能分
        for skill in task.skills_trained:
            cap.update_skill(skill, 1.0 if success else 0.0)

    # ------------------------------------------------------------------
    # 导出状态
    # ------------------------------------------------------------------
    def export_curriculum_state(self, filepath: Optional[Path] = None) -> Dict[str, Any]:
        state = {
            "timestamp": datetime.now().isoformat(),
            "agents": {
                aid: {
                    "global_score": round(cap.global_score, 4),
                    "skill_scores": cap.skill_scores,
                    "completed": len(cap.completed_tasks),
                    "attempts": cap.total_attempts,
                }
                for aid, cap in self.agent_capabilities.items()
            },
            "task_pool": [t.to_dict() for t in self.task_pool],
        }
        if filepath:
            filepath.parent.mkdir(parents=True, exist_ok=True)
            filepath.write_text(
                json.dumps(state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        return state


# ---------------------------------------------------------------------------
# MultiAgentCurriculum
# ---------------------------------------------------------------------------

class MultiAgentCurriculum:
    """
    多 Agent 协同课程。

    核心机制：
    - 共享经验池 (SharedExperienceBuffer)
    - 交叉验证：agent A 的任务可由 agent B 复现验证
    - 技能互补：较弱 agent 从较强 agent 的解法中学习
    """

    def __init__(self):
        self.orchestrator = CurriculumOrchestrator()
        self.shared_buffer: SharedExperienceBuffer = SharedExperienceBuffer()
        self.cross_validation_log: List[Dict[str, Any]] = []

    def register_agents(self, agents: Dict[str, Dict[str, float]]):
        """批量注册 agent"""
        for aid, skills in agents.items():
            self.orchestrator.register_agent(aid, skills)

    def assign_curricula(
        self,
        max_tasks_per_agent: int = 5,
    ) -> Dict[str, List[CurriculumTask]]:
        """为所有 agent 分配课程"""
        assignments: Dict[str, List[CurriculumTask]] = {}
        for agent_id in self.orchestrator.agent_capabilities:
            curriculum = self.orchestrator.generate_curriculum(
                agent_id, max_tasks=max_tasks_per_agent
            )
            assignments[agent_id] = curriculum
        return assignments

    def cross_validate(
        self,
        validator_agent_id: str,
        target_agent_id: str,
        task_id: str,
        target_solution: Any,
        validation_result: bool,
    ):
        """
        交叉验证：validator agent 复现 target agent 的任务，
        验证解的正确性 / 可复现性。
        """
        entry = {
            "timestamp": datetime.now().isoformat(),
            "validator": validator_agent_id,
            "target": target_agent_id,
            "task_id": task_id,
            "solution_match": validation_result,
        }
        self.cross_validation_log.append(entry)

        # 如果验证通过，将经验写入共享池
        if validation_result:
            self.shared_buffer.add_experience(
                task_id=task_id,
                agent_id=target_agent_id,
                solution=target_solution,
                validated_by=validator_agent_id,
            )

    def get_skill_gap_matrix(self) -> Dict[str, Dict[str, float]]:
        """计算 agent 间技能差距矩阵"""
        agents = self.orchestrator.agent_capabilities
        all_skills: Set[str] = set()
        for cap in agents.values():
            all_skills.update(cap.skill_scores.keys())

        matrix: Dict[str, Dict[str, float]] = {}
        for aid_a, cap_a in agents.items():
            matrix[aid_a] = {}
            for aid_b, cap_b in agents.items():
                if aid_a == aid_b:
                    matrix[aid_a][aid_b] = 0.0
                    continue
                diffs = []
                for s in all_skills:
                    sa = cap_a.skill_scores.get(s, 0.5)
                    sb = cap_b.skill_scores.get(s, 0.5)
                    diffs.append(abs(sa - sb))
                matrix[aid_a][aid_b] = float(np.mean(diffs)) if diffs else 0.0
        return matrix


# ---------------------------------------------------------------------------
# SharedExperienceBuffer
# ---------------------------------------------------------------------------

class SharedExperienceBuffer:
    """多 agent 共享经验池"""

    def __init__(self, max_size: int = 10000):
        self.max_size = max_size
        self.experiences: List[Dict[str, Any]] = []
        self.task_experience_index: Dict[str, List[int]] = defaultdict(list)

    def add_experience(
        self,
        task_id: str,
        agent_id: str,
        solution: Any,
        validated_by: Optional[str] = None,
        score: float = 0.0,
    ):
        exp = {
            "task_id": task_id,
            "agent_id": agent_id,
            "solution": solution,
            "validated_by": validated_by,
            "score": score,
            "timestamp": datetime.now().isoformat(),
        }
        idx = len(self.experiences)
        self.experiences.append(exp)
        self.task_experience_index[task_id].append(idx)

        # LRU eviction
        if len(self.experiences) > self.max_size:
            self.experiences.pop(0)
            # 重建索引
            self._rebuild_index()

    def query(self, task_id: str, top_k: int = 5) -> List[Dict[str, Any]]:
        indices = self.task_experience_index.get(task_id, [])
        valid = [i for i in indices if i < len(self.experiences)]
        exps = [self.experiences[i] for i in valid]
        # 按 score 排序
        exps.sort(key=lambda e: e["score"], reverse=True)
        return exps[:top_k]

    def _rebuild_index(self):
        self.task_experience_index.clear()
        for i, exp in enumerate(self.experiences):
            self.task_experience_index[exp["task_id"]].append(i)

    def __len__(self):
        return len(self.experiences)


# ---------------------------------------------------------------------------
# M108 互补集成
# ---------------------------------------------------------------------------

class SearchCurriculumBridge:
    """
    M108 RecursiveSearchOrchestrator ←→ M113 AutoCurriculaOrchestrator 桥接。

    - M108 负责搜索深度（递归分解、多路径探索）
    - M113 负责任务课程广度（难度渐进、多 agent 协同）
    - 搜索深度 × 课程广度 = 全面覆盖
    """

    def __init__(
        self,
        search_orchestrator: Any = None,   # M108 实例引用
        curriculum: Optional[MultiAgentCurriculum] = None,
    ):
        self.search = search_orchestrator
        self.curriculum = curriculum or MultiAgentCurriculum()

    def discover_and_schedule(self, domain: str, max_search_depth: int = 3):
        """
        使用 M108 发现领域子任务 → 转交给 M113 编排为课程。
        """
        # 这里假定 search_orchestrator 有 discover_tasks 接口
        discovered = []
        if self.search and hasattr(self.search, "discover_tasks"):
            discovered = self.search.discover_tasks(domain, depth=max_search_depth)

        for raw_task in discovered:
            task = CurriculumTask(
                task_id=raw_task.get("id", f"auto_{len(self.curriculum.orchestrator.task_pool)}"),
                description=raw_task.get("description", ""),
                skills_required=raw_task.get("skills", []),
                skills_trained=raw_task.get("skills", []),
            )
            self.curriculum.orchestrator.add_task(task)

        return len(discovered)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== M113 AutoCurriculaOrchestrator 自检 ===\n")

    # 构建任务池
    tasks = [
        CurriculumTask("T1", "Hello World 输出", DifficultyLevel.TRIVIAL,
                       skills_trained=["basic_io"]),
        CurriculumTask("T2", "字符串反转", DifficultyLevel.EASY,
                       skills_trained=["string_manipulation"]),
        CurriculumTask("T3", "二分查找", DifficultyLevel.MEDIUM,
                       skills_trained=["algorithm", "binary_search"]),
        CurriculumTask("T4", "快速排序实现", DifficultyLevel.MEDIUM,
                       skills_trained=["algorithm", "sorting"],
                       prerequisites=["T3"]),
        CurriculumTask("T5", "图遍历 BFS/DFS", DifficultyLevel.HARD,
                       skills_trained=["graph", "traversal"],
                       prerequisites=["T4"]),
        CurriculumTask("T6", "A* 寻路", DifficultyLevel.EXPERT,
                       skills_trained=["graph", "pathfinding", "heuristic"],
                       prerequisites=["T5"]),
    ]

    mac = MultiAgentCurriculum()
    mac.register_agents({
        "agent_alpha": {"basic_io": 0.9, "string_manipulation": 0.7, "algorithm": 0.3},
        "agent_beta":  {"basic_io": 0.5, "algorithm": 0.8, "graph": 0.7},
    })

    mac.orchestrator.add_tasks(tasks)

    # 生成课程
    for aid in ["agent_alpha", "agent_beta"]:
        curriculum = mac.orchestrator.generate_curriculum(aid, max_tasks=4)
        cap = mac.orchestrator.agent_capabilities[aid]
        print(f"[{aid}] global_score={cap.global_score:.3f}")
        for t in curriculum:
            est = mac.orchestrator.estimator.estimate(t)
            lvl = mac.orchestrator.estimator.difficulty_to_level(est)
            print(f"  {t.task_id}: {t.description}  [{lvl.name}]  est={est:.3f}")

    # 记录一次结果
    mac.orchestrator.record_result("agent_alpha", "T1", success=True, time_seconds=0.5)
    print(f"\nAfter T1: agent_alpha.global_score={mac.orchestrator.agent_capabilities['agent_alpha'].global_score:.3f}")

    # 交叉验证
    mac.cross_validate("agent_beta", "agent_alpha", "T1", "print('hello')", True)
    print(f"Shared buffer size: {len(mac.shared_buffer)}")
    print(f"Cross-validation entries: {len(mac.cross_validation_log)}")

    # 技能差距矩阵
    gap = mac.get_skill_gap_matrix()
    print(f"\nSkill gap matrix: {json.dumps(gap, indent=2)}")

    print("\n=== 自检通过 ===")
