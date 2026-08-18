"""
# status: orphan (2026-08-15 audit, not in runtime path)
CB68: MetaLearningMemoryOptimizer — 元学习记忆优化器
=====================================================

无梯度元学习框架，自动调优 Trinity 各组件超参数。

核心设计:
  - ParameterSpace: 定义可调参数空间及约束（衰减率、检索权重、重要性阈值等）
  - MetaLearner: Population-Based Training (PBT) 探索-利用交替
  - TaskAdapter: 将不同记忆任务（对话/文档/代码）映射到共享参数空间
  - FitnessEvaluator: 通过下游任务表现评估参数配置
  - ParetoTracker: 帕累托前沿追踪（多目标 trade-off）
  - ParameterCheckpoint: 支持 checkpoint 回滚

Reference:
  - Population-Based Training (PBT) for hyperparameter optimization
  - Gradient-free meta-learning for lifelong agent memory
"""

from __future__ import annotations

import copy
import logging
import math
import random
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class TaskType(Enum):
    """记忆任务类型。"""
    DIALOGUE = "dialogue"      # 对话记忆
    DOCUMENT = "document"      # 文档记忆
    CODE = "code"               # 代码记忆
    MULTIMODAL = "multimodal"  # 多模态
    HYBRID = "hybrid"           # 混合


class ParamType(Enum):
    """参数类型。"""
    CONTINUOUS = "continuous"   # 连续值 [min, max]
    DISCRETE = "discrete"       # 离散值 {options}
    INTEGER = "integer"         # 整数 [min, max]
    CATEGORICAL = "categorical" # 类别


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class ParameterDef:
    """参数定义——可调参数的元信息。

    Attributes:
        name: 参数名。
        param_type: 参数类型。
        min_val / max_val: 连续/整数范围。
        options: 离散/类别选项。
        default: 默认值。
        constraint: 可选约束函数名。
    """
    name: str
    param_type: ParamType
    min_val: float = 0.0
    max_val: float = 1.0
    options: List[Any] = field(default_factory=list)
    default: Any = 0.5
    constraint: str = ""

    def sample(self) -> Any:
        if self.param_type == ParamType.CONTINUOUS:
            return random.uniform(self.min_val, self.max_val)
        elif self.param_type == ParamType.INTEGER:
            return random.randint(int(self.min_val), int(self.max_val))
        elif self.param_type in (ParamType.DISCRETE, ParamType.CATEGORICAL):
            if self.options:
                return random.choice(self.options)
            return self.default
        return self.default


@dataclass
class ParameterSpace:
    """可调参数空间。

    Attributes:
        task_type: 适用的任务类型。
        parameters: 参数定义列表。
        param_dict: 运行时参数名→当前值映射。
    """
    task_type: TaskType = TaskType.HYBRID
    parameters: List[ParameterDef] = field(default_factory=list)
    param_dict: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.param_dict:
            for p in self.parameters:
                self.param_dict[p.name] = p.default

    def get(self, name: str) -> Any:
        return self.param_dict.get(name)

    def set(self, name: str, value: Any):
        self.param_dict[name] = value

    def clone(self) -> ParameterSpace:
        return copy.deepcopy(self)


@dataclass
class ParameterCheckpoint:
    """参数检查点——支持回滚。"""
    step: int
    param_snapshot: Dict[str, Any]
    fitness: float = 0.0
    timestamp: float = field(default_factory=_time.time)


# ============================================================================
# FitnessEvaluator
# ============================================================================

class MLMOFitnessEvaluator:
    """适应度评估器（命名前缀 MLMO 避让已有 FitnessEvaluator）。

    通过下游任务表现（准确率/召回/延迟/压缩比）评估参数配置。
    """

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or {
            "accuracy": 0.35, "recall": 0.30, "latency_inv": 0.20, "compression": 0.15,
        }

    def evaluate(
        self,
        accuracy: float = 0.0,
        recall: float = 0.0,
        latency_ms: float = 100.0,
        compression_ratio: float = 1.0,
    ) -> float:
        """综合适应度评分 [0..1]。"""
        latency_inv = max(0.0, 1.0 - latency_ms / 1000.0)
        return (
            self.weights["accuracy"] * accuracy
            + self.weights["recall"] * recall
            + self.weights["latency_inv"] * latency_inv
            + self.weights["compression"] * min(1.0, compression_ratio)
        )


# ============================================================================
# TaskAdapter
# ============================================================================

class TaskAdapter:
    """将不同记忆任务映射到共享参数空间。"""

    _TASK_DEFAULTS: Dict[TaskType, Dict[str, float]] = {
        TaskType.DIALOGUE: {
            "decay_rate": 0.15, "retrieval_weight": 0.7,
            "importance_threshold": 0.3, "compression_level": 0.5,
        },
        TaskType.DOCUMENT: {
            "decay_rate": 0.05, "retrieval_weight": 0.85,
            "importance_threshold": 0.5, "compression_level": 0.8,
        },
        TaskType.CODE: {
            "decay_rate": 0.03, "retrieval_weight": 0.9,
            "importance_threshold": 0.6, "compression_level": 0.6,
        },
    }

    def adapt(self, task_type: TaskType, space: ParameterSpace) -> ParameterSpace:
        defaults = self._TASK_DEFAULTS.get(task_type, {})
        for name, val in defaults.items():
            if name in space.param_dict:
                space.param_dict[name] = val
        return space

    def create_space(self, task_type: TaskType) -> ParameterSpace:
        params = [
            ParameterDef("decay_rate", ParamType.CONTINUOUS, 0.0, 1.0, default=0.1),
            ParameterDef("retrieval_weight", ParamType.CONTINUOUS, 0.0, 1.0, default=0.8),
            ParameterDef("importance_threshold", ParamType.CONTINUOUS, 0.0, 1.0, default=0.4),
            ParameterDef("compression_level", ParamType.CONTINUOUS, 0.0, 1.0, default=0.5),
            ParameterDef("batch_size", ParamType.INTEGER, 8, 256, default=64),
            ParameterDef("max_context_length", ParamType.INTEGER, 1024, 32768, default=8192),
        ]
        space = ParameterSpace(task_type=task_type, parameters=params)
        return self.adapt(task_type, space)


# ============================================================================
# ParetoTracker
# ============================================================================

@dataclass
class ParetoPoint:
    """帕累托前沿点。"""
    config: Dict[str, Any]
    accuracy: float
    recall: float
    latency_ms: float

    def dominates(self, other: ParetoPoint) -> bool:
        """self Pareto-dominates other（精确度更高且延迟不更差）。"""
        better_quality = self.accuracy >= other.accuracy and self.recall >= other.recall
        not_worse_latency = self.latency_ms <= other.latency_ms
        strictly_better = (
            self.accuracy > other.accuracy
            or self.recall > other.recall
            or self.latency_ms < other.latency_ms
        )
        return better_quality and not_worse_latency and strictly_better


class ParetoTracker:
    """帕累托前沿追踪器。"""

    def __init__(self, max_frontier: int = 20):
        self._frontier: List[ParetoPoint] = []
        self.max_frontier = max_frontier
        self._lock = threading.RLock()

    def add(self, point: ParetoPoint):
        with self._lock:
            # Remove points dominated by the new point
            self._frontier = [p for p in self._frontier if not point.dominates(p)]
            # Add if not dominated by existing
            if not any(p.dominates(point) for p in self._frontier):
                self._frontier.append(point)
                self._frontier = self._frontier[-self.max_frontier:]

    def get_frontier(self) -> List[ParetoPoint]:
        with self._lock:
            return list(self._frontier)

    def size(self) -> int:
        with self._lock:
            return len(self._frontier)


# ============================================================================
# MetaLearner (PBT)
# ============================================================================

class MetaLearner:
    """Population-Based Training 元学习器。

    探索（explore）= mutate + perturb；利用（exploit）= replace low-fitness members。
    """

    def __init__(
        self,
        population_size: int = 16,
        exploit_interval: int = 5,
        perturbation_scale: float = 0.1,
    ):
        self.population_size = population_size
        self.exploit_interval = exploit_interval
        self.perturbation_scale = perturbation_scale
        self._population: List[Tuple[ParameterSpace, float]] = []
        self._step: int = 0
        self._lock = threading.RLock()

    def initialize(self, base_space: ParameterSpace):
        with self._lock:
            self._population = []
            self._step = 0
            for _ in range(self.population_size):
                variant = base_space.clone()
                for p in variant.parameters:
                    variant.param_dict[p.name] = p.sample()
                self._population.append((variant, 0.0))

    def step(self, fitnesses: List[float]) -> Optional[ParameterSpace]:
        """PBT 单步：利用 + 探索。

        Args:
            fitnesses: 与 population[i] 一一对应的适应度列表。

        Returns:
            当前最优参数空间。
        """
        with self._lock:
            if len(fitnesses) != len(self._population):
                return None

            for i, fit in enumerate(fitnesses):
                self._population[i] = (self._population[i][0], fit)

            self._step += 1

            if self._step % self.exploit_interval == 0:
                self._exploit()

            self._population.sort(key=lambda x: x[1], reverse=True)
            return self._population[0][0].clone()

    def _exploit(self):
        """底部 20% 被顶部 20% 替换 + 变异。"""
        n = len(self._population)
        top_n = max(1, n // 5)

        sorted_pop = sorted(self._population, key=lambda x: x[1], reverse=True)
        elite = sorted_pop[:top_n]

        for i in range(n - top_n, n):
            donor = random.choice(elite)[0].clone()
            for p in donor.parameters:
                if p.param_type in (ParamType.CONTINUOUS, ParamType.INTEGER):
                    noise = random.uniform(-self.perturbation_scale, self.perturbation_scale)
                    val = donor.param_dict[p.name] + noise * (p.max_val - p.min_val)
                    donor.param_dict[p.name] = max(p.min_val, min(p.max_val, val))
                elif p.param_type in (ParamType.DISCRETE, ParamType.CATEGORICAL):
                    if random.random() < self.perturbation_scale and p.options:
                        donor.param_dict[p.name] = random.choice(p.options)
            self._population[i] = (donor, self._population[i][1])

    def best(self) -> Optional[ParameterSpace]:
        with self._lock:
            if not self._population:
                return None
            return max(self._population, key=lambda x: x[1])[0].clone()


class PopulationTrainer:
    """群体训练器——管理多任务多配置。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._trainers: Dict[str, MetaLearner] = {}

    def get_or_create(self, task_id: str, base_space: ParameterSpace) -> MetaLearner:
        with self._lock:
            if task_id not in self._trainers:
                ml = MetaLearner()
                ml.initialize(base_space)
                self._trainers[task_id] = ml
            return self._trainers[task_id]


# ============================================================================
# Main Class
# ============================================================================

class MetaLearningMemoryOptimizer:
    """元学习记忆优化器 (CB68)。

    自动调优 Trinity 超参数的入口。

    Usage:
        mlmo = MetaLearningMemoryOptimizer()
        mlmo.register_task("chat_memory", TaskType.DIALOGUE)
        mlmo.report_fitness("chat_memory", fitnesses=[0.78, 0.65, ...])
        best = mlmo.get_best_params("chat_memory")
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.adapter = TaskAdapter()
        self.evaluator = MLMOFitnessEvaluator()
        self.trainer = PopulationTrainer()
        self.pareto = ParetoTracker()
        self._spaces: Dict[str, ParameterSpace] = {}
        self._checkpoints: Dict[str, List[ParameterCheckpoint]] = {}
        self._start_time: float = _time.time()

    def register_task(self, task_id: str, task_type: TaskType):
        with self._lock:
            space = self.adapter.create_space(task_type)
            self._spaces[task_id] = space
            self.trainer.get_or_create(task_id, space)

    def report_fitness(
        self,
        task_id: str,
        fitnesses: List[float],
        accuracy: float = 0.0,
        recall: float = 0.0,
        latency_ms: float = 100.0,
    ):
        with self._lock:
            if task_id not in self._spaces:
                return
            ml = self.trainer.get_or_create(task_id, self._spaces[task_id])
            best = ml.step(fitnesses)
            if best:
                self._spaces[task_id] = best
                self.pareto.add(ParetoPoint(
                    config=dict(best.param_dict),
                    accuracy=accuracy, recall=recall, latency_ms=latency_ms,
                ))

    def get_best_params(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            ml = self.trainer._trainers.get(task_id)
            if ml:
                best = ml.best()
                return best.param_dict if best else None
            return None

    def checkpoint(self, task_id: str) -> Optional[ParameterCheckpoint]:
        with self._lock:
            space = self._spaces.get(task_id)
            if not space:
                return None
            cp = ParameterCheckpoint(
                step=len(self._checkpoints.get(task_id, [])),
                param_snapshot=dict(space.param_dict),
            )
            self._checkpoints.setdefault(task_id, []).append(cp)
            return cp

    def rollback(self, task_id: str, step: int) -> bool:
        with self._lock:
            cps = self._checkpoints.get(task_id, [])
            if step < 0 or step >= len(cps):
                return False
            space = self._spaces.get(task_id)
            if space:
                space.param_dict = dict(cps[step].param_snapshot)
                return True
            return False

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "MetaLearningMemoryOptimizer (CB68)",
                "registered_tasks": list(self._spaces.keys()),
                "pareto_frontier_size": self.pareto.size(),
                "total_checkpoints": sum(len(c) for c in self._checkpoints.values()),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
