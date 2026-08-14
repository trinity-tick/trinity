"""
P13-5: Multi-Session Arena
===========================

对标 MemoryArena (ICML 2026) — 多会话记忆基准测试框架。

设计要点：
  - 用于评估跨会话记忆系统的信息保持与选择性遗忘能力
  - 模拟真实的多日/多周会话间隔，引入自然遗忘曲线
  - 区分"有益遗忘"（清理无关信息）与"有害遗忘"（丢失关键依赖）
  - benchmark() 输出标准 Recall / Precision / ForgettingScore 三元指标

核心组件：
  - MultiSessionTask:              跨会话任务定义，含隐藏依赖与部分信息
  - SessionSimulator:              按天/周间隔模拟多会话交互
  - CrossSessionDependencyTracker:  追踪会话间任务依赖链
  - SelectiveForgettingScorer:     区分有益/有害遗忘，计算综合遗忘分
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, Generic, List, Optional, Sequence, Set, Tuple, TypeVar, Union

import numpy as np

logger = logging.getLogger(__name__)

# ============================================================================
# Enums
# ============================================================================

class TaskDifficulty(Enum):
    """任务难度等级。"""
    TRIVIAL = "trivial"         # 单步完成，无隐藏依赖
    EASY = "easy"               # 2-3 步，1 个隐藏依赖
    MODERATE = "moderate"       # 4-6 步，2-3 个隐藏依赖
    HARD = "hard"               # 7-10 步，4+ 个隐藏依赖，跨会话
    EXPERT = "expert"           # 10+ 步，多级隐藏依赖，需信息整合


class DependencyType(Enum):
    """会话间依赖类型。"""
    EXPLICIT = "explicit"        # 显式引用前一会话的输出
    IMPLICIT = "implicit"        # 隐含的上下文依赖（需推理）
    TEMPORAL = "temporal"        # 时间序列依赖（先后顺序）
    SEMANTIC = "semantic"        # 语义关联（主题延续）
    INSTRUMENTAL = "instrumental" # 工具性依赖（前一会话产出的文件/工具）


class ForgettingCategory(Enum):
    """遗忘类别。"""
    BENEFICIAL = "beneficial"    # 有益遗忘：清理无关/冗余信息
    HARMFUL = "harmful"          # 有害遗忘：丢失关键依赖
    NEUTRAL = "neutral"          # 中性遗忘：对任务无影响
    AMBIGUOUS = "ambiguous"      # 模糊遗忘：后果不确定


class SessionGranularity(Enum):
    """会话粒度。"""
    HOURLY = "hourly"
    DAILY = "daily"
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class HiddenDependency:
    """隐藏依赖——不显式标注、需系统自行发现的跨会话依赖。"""
    dependency_id: str
    source_session: int
    target_session: int
    dependency_type: DependencyType
    description: str
    criticality: float = 0.5            # 对任务完成的关键程度 (0-1)
    discoverable: bool = True            # 是否可通过上下文推理发现
    hint_keywords: List[str] = field(default_factory=list)


@dataclass
class TaskStep:
    """任务步骤。"""
    step_index: int
    description: str
    required_knowledge: List[str] = field(default_factory=list)
    hidden_dependencies: List[str] = field(default_factory=list)
    expected_output: str = ""
    is_critical: bool = False


@dataclass
class MultiSessionTask:
    """跨会话任务定义。"""
    task_id: str
    name: str
    description: str
    difficulty: TaskDifficulty = TaskDifficulty.MODERATE
    total_sessions: int = 3
    steps: List[TaskStep] = field(default_factory=list)
    hidden_dependencies: List[HiddenDependency] = field(default_factory=list)
    partial_information_ratio: float = 0.3   # 每次会话可见信息的比例
    tags: List[str] = field(default_factory=list)


@dataclass
class SessionRecord:
    """单次会话记录。"""
    session_id: str
    session_index: int
    task_id: str
    granularity: SessionGranularity = SessionGranularity.DAILY
    elapsed_days: float = 0.0               # 距离首次会话的天数
    input_context: Dict[str, Any] = field(default_factory=dict)
    output_context: Dict[str, Any] = field(default_factory=dict)
    retrieved_memories: List[str] = field(default_factory=list)
    forgotten_dependencies: List[str] = field(default_factory=list)
    completion_ratio: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class DependencyChain:
    """依赖链——跨会话的依赖关系路径。"""
    chain_id: str
    dependency_ids: List[str]
    source_session: int
    target_session: int
    length: int = 0
    critical_path: bool = False         # 是否为关键路径
    resolved: bool = False
    resolution_latency_sessions: int = 0


@dataclass
class ForgettingEvent:
    """遗忘事件。"""
    event_id: str
    dependency_id: str
    category: ForgettingCategory
    session_index: int
    detected_at: float = field(default_factory=time.time)
    recovery_possible: bool = True
    impact_score: float = 0.0            # 对任务的影响程度
    description: str = ""


@dataclass
class ArenaStats:
    """Arena 统计信息。"""
    total_tasks: int = 0
    total_sessions: int = 0
    total_dependencies: int = 0
    resolved_dependencies: int = 0
    beneficial_forgetting: int = 0
    harmful_forgetting: int = 0
    neutral_forgetting: int = 0
    ambiguous_forgetting: int = 0
    recall: float = 0.0
    precision: float = 0.0
    forgetting_score: float = 0.0
    avg_session_completion: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# CrossSessionDependencyTracker
# ============================================================================

class CrossSessionDependencyTracker:
    """跨会话依赖追踪器。

    追踪任务间隐藏和显式依赖链，维护依赖图谱，
    在每次会话结束后更新依赖解析状态。
    """

    def __init__(self, name: str = "cross_session_dependency_tracker") -> None:
        self._name = name
        self._lock = threading.RLock()
        self._dependencies: Dict[str, HiddenDependency] = {}
        self._chains: Dict[str, DependencyChain] = {}
        self._resolved: Set[str] = set()
        self._unresolved: Set[str] = set()
        # 邻接表：session_index -> Set[session_index]
        self._session_graph: Dict[int, Set[int]] = defaultdict(set)

    def register_dependency(self, dep: HiddenDependency) -> None:
        """注册一个隐藏依赖。"""
        with self._lock:
            self._dependencies[dep.dependency_id] = dep
            self._unresolved.add(dep.dependency_id)
            self._session_graph[dep.source_session].add(dep.target_session)

    def register_dependencies(self, deps: List[HiddenDependency]) -> None:
        """批量注册隐藏依赖。"""
        for dep in deps:
            self.register_dependency(dep)

    def mark_resolved(self, dependency_id: str, session_index: int) -> bool:
        """标记依赖已解析。"""
        with self._lock:
            if dependency_id not in self._dependencies:
                return False
            self._resolved.add(dependency_id)
            self._unresolved.discard(dependency_id)
            return True

    def get_unresolved(self, session_index: int) -> List[HiddenDependency]:
        """获取指定会话中仍未解析的依赖。"""
        with self._lock:
            return [
                d for did, d in self._dependencies.items()
                if did in self._unresolved and d.source_session <= session_index
            ]

    def build_chain(
        self,
        dep_ids: List[str],
        source_session: int,
        target_session: int,
        critical: bool = False,
    ) -> DependencyChain:
        """构建依赖链。"""
        chain_id = str(uuid.uuid4())[:12]
        chain = DependencyChain(
            chain_id=chain_id,
            dependency_ids=list(dep_ids),
            source_session=source_session,
            target_session=target_session,
            length=len(dep_ids),
            critical_path=critical,
        )
        with self._lock:
            self._chains[chain_id] = chain
        return chain

    def get_stats(self) -> Dict[str, Any]:
        """获取追踪器统计信息。"""
        with self._lock:
            return {
                "name": self._name,
                "total_dependencies": len(self._dependencies),
                "resolved": len(self._resolved),
                "unresolved": len(self._unresolved),
                "total_chains": len(self._chains),
                "session_count": len(self._session_graph),
            }


# ============================================================================
# SessionSimulator
# ============================================================================

class SessionSimulator:
    """多会话模拟器。

    按指定的时间间隔（天/周）模拟多轮会话交互，
    控制每轮会话的可见信息量（partial information ratio），
    支持自定义遗忘曲线插值参数。
    """

    def __init__(
        self,
        granularity: SessionGranularity = SessionGranularity.DAILY,
        partial_ratio: float = 0.3,
        forgetting_decay_rate: float = 0.05,
        name: str = "session_simulator",
    ) -> None:
        self._granularity = granularity
        self._partial_ratio = partial_ratio
        self._forgetting_decay_rate = forgetting_decay_rate
        self._name = name
        self._lock = threading.RLock()
        self._sessions: Dict[str, SessionRecord] = {}
        self._session_index: int = 0

    def _granularity_days(self) -> float:
        """返回粒度对应的天数。"""
        mapping = {
            SessionGranularity.HOURLY: 1.0 / 24,
            SessionGranularity.DAILY: 1.0,
            SessionGranularity.WEEKLY: 7.0,
            SessionGranularity.BIWEEKLY: 14.0,
            SessionGranularity.MONTHLY: 30.0,
        }
        return mapping.get(self._granularity, 1.0)

    def simulate_session(
        self,
        task: MultiSessionTask,
        memory_retrieval_fn: Optional[Callable[[str], List[str]]] = None,
    ) -> SessionRecord:
        """模拟一次会话交互。"""
        with self._lock:
            self._session_index += 1
            session_id = f"{task.task_id}_S{self._session_index:03d}"
            days = self._session_index * self._granularity_days()

            # 模拟部分信息可见
            visible_steps = max(1, int(len(task.steps) * self._partial_ratio))
            retrieved: List[str] = []
            if memory_retrieval_fn:
                try:
                    retrieved = memory_retrieval_fn(task.task_id)
                except Exception:
                    pass

            # 模拟遗忘效应
            forgotten = self._simulate_forgetting(task, days)

            record = SessionRecord(
                session_id=session_id,
                session_index=self._session_index,
                task_id=task.task_id,
                granularity=self._granularity,
                elapsed_days=days,
                input_context={"visible_steps": visible_steps},
                retrieved_memories=retrieved,
                forgotten_dependencies=forgotten,
                completion_ratio=min(1.0, self._session_index / task.total_sessions),
            )
            self._sessions[session_id] = record
            logger.debug(
                "Session %s index=%d days=%.1f visible=%d retrieved=%d forgotten=%d",
                session_id, self._session_index, days,
                visible_steps, len(retrieved), len(forgotten),
            )
            return record

    def _simulate_forgetting(
        self, task: MultiSessionTask, days: float
    ) -> List[str]:
        """根据艾宾浩斯式衰减模拟遗忘的依赖 ID 列表。"""
        forgotten: List[str] = []
        for dep in task.hidden_dependencies:
            # 遗忘概率 = 1 - exp(-decay_rate * days / criticality)
            survival = math.exp(
                -self._forgetting_decay_rate * days / max(dep.criticality, 0.01)
            )
            if np.random.random() > survival:
                forgotten.append(dep.dependency_id)
        return forgotten

    def get_session(self, session_id: str) -> Optional[SessionRecord]:
        """获取指定会话记录。"""
        with self._lock:
            return self._sessions.get(session_id)

    def get_all_sessions(self) -> List[SessionRecord]:
        """获取所有会话记录。"""
        with self._lock:
            return list(self._sessions.values())

    def get_stats(self) -> Dict[str, Any]:
        """获取模拟器统计信息。"""
        with self._lock:
            return {
                "name": self._name,
                "granularity": self._granularity.value,
                "total_sessions": len(self._sessions),
                "partial_ratio": self._partial_ratio,
                "forgetting_decay_rate": self._forgetting_decay_rate,
                "current_index": self._session_index,
            }


# ============================================================================
# SelectiveForgettingScorer
# ============================================================================

class SelectiveForgettingScorer:
    """选择性遗忘评分器。

    区分三类遗忘：
      - 有益遗忘（Beneficial）：清理了与任务无关的过期信息
      - 有害遗忘（Harmful）：丢失了关键依赖，导致任务无法完成
      - 中性遗忘（Neutral）：对任务完成无影响

    基于依赖关键程度（criticality）、恢复难度（recoverability）、
    会话跨度（session span）三维度计算综合 ForgettingScore。
    """

    def __init__(self, name: str = "forgetting_scorer") -> None:
        self._name = name
        self._lock = threading.RLock()
        self._events: List[ForgettingEvent] = []

    def classify_forgetting(
        self,
        dependency: HiddenDependency,
        recovered: bool,
        impact_on_task: float,
    ) -> ForgettingCategory:
        """分类遗忘事件。"""
        if not recovered and dependency.criticality > 0.7:
            return ForgettingCategory.HARMFUL
        if recovered and dependency.criticality < 0.3:
            return ForgettingCategory.BENEFICIAL
        if recovered:
            return ForgettingCategory.NEUTRAL
        return ForgettingCategory.AMBIGUOUS

    def record_event(
        self,
        dependency: HiddenDependency,
        session_index: int,
        recovered: bool,
        impact: float,
    ) -> ForgettingEvent:
        """记录一次遗忘事件并分类。"""
        category = self.classify_forgetting(dependency, recovered, impact)
        event = ForgettingEvent(
            event_id=str(uuid.uuid4())[:12],
            dependency_id=dependency.dependency_id,
            category=category,
            session_index=session_index,
            recovery_possible=not recovered and dependency.discoverable,
            impact_score=impact,
            description=(
                f"session {session_index}: {category.value} forgetting of "
                f"'{dependency.description}' (criticality={dependency.criticality})"
            ),
        )
        with self._lock:
            self._events.append(event)
        return event

    def compute_scores(self) -> Dict[str, float]:
        """计算 Recall / Precision / ForgettingScore。"""
        with self._lock:
            total = len(self._events)
            if total == 0:
                return {"recall": 1.0, "precision": 1.0, "forgetting_score": 0.0}

            harmful = sum(1 for e in self._events if e.category == ForgettingCategory.HARMFUL)
            beneficial = sum(1 for e in self._events if e.category == ForgettingCategory.BENEFICIAL)
            neutral = sum(1 for e in self._events if e.category == ForgettingCategory.NEUTRAL)
            ambiguous = sum(1 for e in self._events if e.category == ForgettingCategory.AMBIGUOUS)

            # Recall: 非有害遗忘占全部依赖的比例
            recall = 1.0 - (harmful / total)
            # Precision: 有益遗忘占所有遗忘的比例（排除中性）
            all_forgetting = harmful + beneficial + ambiguous
            precision = beneficial / max(all_forgetting, 1)
            # ForgettingScore: 综合分 = Recall * (1 - harmful_weight) + Precision * beneficial_weight
            forgetting_score = recall * 0.6 + precision * 0.4

            return {
                "recall": round(recall, 4),
                "precision": round(precision, 4),
                "forgetting_score": round(forgetting_score, 4),
                "harmful": harmful,
                "beneficial": beneficial,
                "neutral": neutral,
                "ambiguous": ambiguous,
                "total": total,
            }

    def get_stats(self) -> Dict[str, Any]:
        """获取评分器统计信息。"""
        scores = self.compute_scores()
        scores["name"] = self._name
        scores["total_events"] = len(self._events)
        return scores


# ============================================================================
# Multi-Session Arena (Top-Level)
# ============================================================================

class MultiSessionArena:
    """多会话竞技场——主入口。

    整合任务定义、会话模拟、依赖追踪、遗忘评分，
    提供统一的 benchmark() 接口输出 Recall / Precision / ForgettingScore。

    对标 MemoryArena (ICML 2026)。
    """

    def __init__(
        self,
        granularity: SessionGranularity = SessionGranularity.DAILY,
        name: str = "multi_session_arena",
    ) -> None:
        self._name = name
        self._lock = threading.RLock()
        self._simulator = SessionSimulator(granularity=granularity)
        self._tracker = CrossSessionDependencyTracker()
        self._scorer = SelectiveForgettingScorer()
        self._tasks: Dict[str, MultiSessionTask] = {}
        self._results: Dict[str, ArenaStats] = {}

    def register_task(self, task: MultiSessionTask) -> None:
        """注册跨会话任务。"""
        with self._lock:
            self._tasks[task.task_id] = task
            for dep in task.hidden_dependencies:
                self._tracker.register_dependency(dep)

    def run_task(
        self,
        task_id: str,
        memory_retrieval_fn: Optional[Callable[[str], List[str]]] = None,
    ) -> List[SessionRecord]:
        """运行一个跨会话任务并返回所有会话记录。"""
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task '{task_id}' not registered")

        sessions: List[SessionRecord] = []
        for _ in range(task.total_sessions):
            record = self._simulator.simulate_session(task, memory_retrieval_fn)
            sessions.append(record)

            # 模拟依赖解析与遗忘分类
            for dep in task.hidden_dependencies:
                recovered = dep.dependency_id in record.retrieved_memories
                impact = dep.criticality * (1.0 - record.completion_ratio)
                self._scorer.record_event(dep, record.session_index, recovered, impact)
                if recovered:
                    self._tracker.mark_resolved(dep.dependency_id, record.session_index)

        return sessions

    def benchmark(
        self,
        memory_retrieval_fn: Optional[Callable[[str], List[str]]] = None,
    ) -> ArenaStats:
        """运行基准测试，输出 Recall / Precision / ForgettingScore。

        对所有已注册任务依次运行多会话模拟，汇总统计信息。
        """
        with self._lock:
            total_tasks = len(self._tasks)
            total_sessions = 0
            total_deps = 0
            total_completion = 0.0

            for task_id in list(self._tasks.keys()):
                sessions = self.run_task(task_id, memory_retrieval_fn)
                total_sessions += len(sessions)
                total_deps += len(self._tasks[task_id].hidden_dependencies)
                total_completion += (
                    sessions[-1].completion_ratio if sessions else 0.0
                )

            scores = self._scorer.compute_scores()
            tracker_stats = self._tracker.get_stats()

            stats = ArenaStats(
                total_tasks=total_tasks,
                total_sessions=total_sessions,
                total_dependencies=total_deps,
                resolved_dependencies=tracker_stats["resolved"],
                beneficial_forgetting=scores.get("beneficial", 0),
                harmful_forgetting=scores.get("harmful", 0),
                neutral_forgetting=scores.get("neutral", 0),
                ambiguous_forgetting=scores.get("ambiguous", 0),
                recall=scores["recall"],
                precision=scores["precision"],
                forgetting_score=scores["forgetting_score"],
                avg_session_completion=round(
                    total_completion / max(total_tasks, 1), 4
                ),
            )
            self._results["latest"] = stats
            logger.info(
                "Benchmark complete: recall=%.4f precision=%.4f fs=%.4f",
                stats.recall, stats.precision, stats.forgetting_score,
            )
            return stats

    def get_stats(self) -> Dict[str, Any]:
        """获取 Arena 总体统计信息。"""
        with self._lock:
            stats = self._results.get("latest")
            return {
                "name": self._name,
                "tasks_registered": len(self._tasks),
                "latest_benchmark": dataclasses.asdict(stats) if stats else None,
                "simulator_stats": self._simulator.get_stats(),
                "tracker_stats": self._tracker.get_stats(),
                "scorer_stats": self._scorer.get_stats(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """返回模块级统计信息。"""
    return {
        "module": "P13-5 Multi-Session Arena",
        "benchmark": "MemoryArena (ICML 2026)",
        "classes": 4,
        "enums": 4,
        "dataclasses": 6,
        "key_metric": "Recall / Precision / ForgettingScore",
        "thread_safe": True,
    }
