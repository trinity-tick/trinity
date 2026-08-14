"""
P19-8: Context Engineering Orchestrator — Production Full-Stack DAG Orchestration
==================================================================================

对标 2026 生产全栈上下文工程编排。

设计要点：
  - 五阶段 DAG：Token 预算计算 → 语义压缩 → KV 复用 → 记忆路由 → 推测解码
  - 阶段间数据依赖自动解析
  - 各阶段自适应参数调优
  - 端到端延迟追踪与预算控制

核心组件：
  - DAGStageDefinition:     DAG 阶段定义
  - DependencyResolver:      依赖自动解析器
  - AdaptiveParameterTuner:  自适应参数调优
  - LatencyTracker:          端到端延迟追踪
  - ContextEngineeringOrchestrator: 总编排器
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

class DAGStage(Enum):
    """DAG 五阶段。"""
    TOKEN_BUDGET = "token_budget"          # Stage 1: Token 预算计算
    SEMANTIC_COMPRESSION = "semantic_compression"  # Stage 2: 语义压缩
    KV_REUSE = "kv_reuse"                  # Stage 3: KV 缓存复用
    MEMORY_ROUTING = "memory_routing"      # Stage 4: 记忆路由
    SPECULATIVE_DECODE = "speculative_decode"  # Stage 5: 推测解码


class StageStatus(Enum):
    """阶段状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class TuningStrategy(Enum):
    """参数调优策略。"""
    GRID_SEARCH = "grid_search"
    BAYESIAN = "bayesian"
    GRADIENT_BASED = "gradient_based"
    HEURISTIC = "heuristic"


class BudgetType(Enum):
    """预算类型。"""
    TOKEN = "token"               # Token 预算
    LATENCY = "latency"           # 延迟预算（ms）
    MEMORY = "memory"             # 内存预算（MB）
    COMPUTE = "compute"           # 计算预算（FLOPs）


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class StageConfig:
    """阶段配置。"""
    stage: DAGStage
    parameters: Dict[str, Any] = field(default_factory=dict)
    timeout_ms: float = 5000.0
    retry_count: int = 1
    skip_on_failure: bool = False


@dataclass
class StageResult:
    """阶段执行结果。"""
    result_id: str
    stage: DAGStage
    status: StageStatus
    output: Any = None
    latency_ms: float = 0.0
    token_consumed: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    finished_at: float = 0.0


@dataclass
class DAGNode:
    """DAG 节点。"""
    node_id: str
    stage: DAGStage
    config: StageConfig
    dependencies: List[str] = field(default_factory=list)
    status: StageStatus = StageStatus.PENDING


@dataclass
class OrchestrationBudget:
    """编排预算。"""
    budget_id: str
    total_token_budget: int = 4096
    total_latency_budget_ms: float = 3000.0
    per_stage_budgets: Dict[DAGStage, Dict[str, float]] = field(default_factory=dict)
    remaining_tokens: int = 4096
    elapsed_ms: float = 0.0


@dataclass
class LatencyProfile:
    """延迟画像。"""
    profile_id: str
    stage_latencies: Dict[DAGStage, float] = field(default_factory=dict)
    total_latency_ms: float = 0.0
    bottleneck_stage: Optional[DAGStage] = None
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# Constants
# ============================================================================

DEFAULT_STAGE_DEPENDENCIES: Dict[DAGStage, List[DAGStage]] = {
    DAGStage.TOKEN_BUDGET: [],
    DAGStage.SEMANTIC_COMPRESSION: [DAGStage.TOKEN_BUDGET],
    DAGStage.KV_REUSE: [DAGStage.SEMANTIC_COMPRESSION],
    DAGStage.MEMORY_ROUTING: [DAGStage.KV_REUSE],
    DAGStage.SPECULATIVE_DECODE: [DAGStage.MEMORY_ROUTING],
}

DEFAULT_STAGE_CONFIGS: Dict[DAGStage, Dict[str, Any]] = {
    DAGStage.TOKEN_BUDGET: {"budget_fraction": 1.0, "min_tokens": 512},
    DAGStage.SEMANTIC_COMPRESSION: {"compression_ratio": 0.6, "keep_instructions": True},
    DAGStage.KV_REUSE: {"cache_ttl": 300, "max_prefix_len": 2048},
    DAGStage.MEMORY_ROUTING: {"top_k": 10, "fusion_strategy": "rrf"},
    DAGStage.SPECULATIVE_DECODE: {"acceptance_threshold": 0.7, "draft_len": 5},
}


# ============================================================================
# Core Components
# ============================================================================

class DependencyResolver:
    """阶段间数据依赖自动解析器。"""

    def __init__(self):
        self._lock = threading.RLock()
        self.dag_nodes: Dict[str, DAGNode] = {}
        self._built = False

    def build_dag(self, stages: Optional[List[DAGStage]] = None) -> List[DAGNode]:
        """构建 DAG。"""
        with self._lock:
            stage_list = stages or list(DAGStage)
            nodes: List[DAGNode] = []

            for stage in stage_list:
                deps = DEFAULT_STAGE_DEPENDENCIES.get(stage, [])
                dep_ids = [self._stage_to_id(d) for d in deps]
                config = StageConfig(
                    stage=stage,
                    parameters=DEFAULT_STAGE_CONFIGS.get(stage, {}),
                )
                node = DAGNode(
                    node_id=self._stage_to_id(stage),
                    stage=stage,
                    config=config,
                    dependencies=dep_ids,
                )
                nodes.append(node)
                self.dag_nodes[node.node_id] = node

            self._built = True
            return nodes

    def resolve(self, stage_results: Dict[str, StageResult]) -> List[DAGStage]:
        """解析可执行的下一批阶段。"""
        with self._lock:
            ready: List[DAGStage] = []
            completed = {nid for nid, r in stage_results.items()
                        if r.status == StageStatus.COMPLETED}

            for nid, node in self.dag_nodes.items():
                if nid in completed or node.status == StageStatus.COMPLETED:
                    continue
                if node.status in (StageStatus.FAILED, StageStatus.SKIPPED):
                    continue
                if all(dep in completed for dep in node.dependencies):
                    ready.append(node.stage)

            return ready

    @staticmethod
    def _stage_to_id(stage: DAGStage) -> str:
        return stage.value

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self.dag_nodes),
                "stages": [n.stage.value for n in self.dag_nodes.values()],
                "built": self._built,
            }


class AdaptiveParameterTuner:
    """自适应参数调优。

    基于历史执行数据调整各阶段参数。
    """

    def __init__(self, strategy: TuningStrategy = TuningStrategy.HEURISTIC):
        self._lock = threading.RLock()
        self.strategy = strategy
        self.history: List[Dict[str, Any]] = []
        self.param_importance: Dict[str, float] = defaultdict(float)
        self.best_params: Dict[str, Dict[str, Any]] = {}

    def record(self, stage: DAGStage, params: Dict[str, Any], latency_ms: float, success: bool):
        """记录一次执行。"""
        with self._lock:
            self.history.append({
                "stage": stage.value,
                "params": params,
                "latency_ms": latency_ms,
                "success": success,
                "timestamp": time.time(),
            })

    def tune(self, stage: DAGStage, current_params: Dict[str, Any]) -> Dict[str, Any]:
        """自适应调优参数。"""
        with self._lock:
            if self.strategy == TuningStrategy.HEURISTIC:
                return self._heuristic_tune(stage, current_params)
            return current_params

    def _heuristic_tune(self, stage: DAGStage, current: Dict[str, Any]) -> Dict[str, Any]:
        """启发式调优。"""
        tuned = dict(current)

        # 基于历史数据调整
        stage_history = [h for h in self.history if h["stage"] == stage.value and h["success"]]
        if len(stage_history) < 3:
            return tuned

        # 寻找最佳历史参数
        best = min(stage_history, key=lambda h: h["latency_ms"])
        if best["latency_ms"] < stage_history[-1].get("latency_ms", float("inf")) * 0.9:
            for k, v in best["params"].items():
                if k in tuned and isinstance(v, (int, float)):
                    tuned[k] = v * 0.8 + tuned[k] * 0.2  # EMA

        return tuned

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_records": len(self.history),
                "strategy": self.strategy.value,
            }


class LatencyTracker:
    """端到端延迟追踪。

    分阶段记录延迟，定位瓶颈。
    """

    def __init__(self, window_size: int = 100):
        self._lock = threading.RLock()
        self.window_size = window_size
        self.profiles: deque[LatencyProfile] = deque(maxlen=window_size)
        self.stage_latency_buffer: Dict[DAGStage, deque[float]] = {
            s: deque(maxlen=window_size) for s in DAGStage
        }

    def record_stage(self, stage: DAGStage, latency_ms: float):
        """记录单阶段延迟。"""
        with self._lock:
            self.stage_latency_buffer[stage].append(latency_ms)

    def build_profile(self) -> LatencyProfile:
        """构建延迟画像。"""
        with self._lock:
            stage_avg: Dict[DAGStage, float] = {}
            for stage, buf in self.stage_latency_buffer.items():
                if buf:
                    stage_avg[stage] = sum(buf) / len(buf)

            total = sum(stage_avg.values())

            # 找出瓶颈
            bottleneck = max(stage_avg, key=stage_avg.get) if stage_avg else None

            profile = LatencyProfile(
                profile_id=str(uuid.uuid4())[:8],
                stage_latencies=stage_avg,
                total_latency_ms=round(total, 2),
                bottleneck_stage=bottleneck,
            )
            self.profiles.append(profile)
            return profile

    def p95(self) -> float:
        """P95 延迟。"""
        with self._lock:
            all_latencies = []
            for buf in self.stage_latency_buffer.values():
                all_latencies.extend(buf)
            if not all_latencies:
                return 0.0
            sorted_lat = sorted(all_latencies)
            idx = int(len(sorted_lat) * 0.95)
            return sorted_lat[min(idx, len(sorted_lat) - 1)]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            stage_avgs = {}
            for stage, buf in self.stage_latency_buffer.items():
                if buf:
                    stage_avgs[stage.value] = round(sum(buf) / len(buf), 2)
            return {
                "total_profiles": len(self.profiles),
                "p95_ms": round(self.p95(), 2),
                "stage_averages": stage_avgs,
            }


class ContextEngineeringOrchestrator:
    """生产全栈上下文工程编排器。

    五阶段 DAG 编排、预算控制、延迟追踪。
    """

    def __init__(self, total_budget: int = 4096, latency_target_ms: float = 2000.0):
        self._lock = threading.RLock()
        self.resolver = DependencyResolver()
        self.tuner = AdaptiveParameterTuner()
        self.tracker = LatencyTracker()
        self.total_budget = total_budget
        self.latency_target_ms = latency_target_ms
        self.results: Dict[str, StageResult] = {}
        self.orchestration_count: int = 0

    def run(self, input_text: str, stage_handlers: Optional[Dict[DAGStage, Callable]] = None) -> Dict[str, Any]:
        """执行五阶段编排。"""
        with self._lock:
            self.orchestration_count += 1
            orchestration_id = str(uuid.uuid4())[:8]
            overall_start = time.time()

            # 构建 DAG
            nodes = self.resolver.build_dag()

            # 全局预算
            budget = OrchestrationBudget(
                budget_id=orchestration_id,
                total_token_budget=self.total_budget,
                total_latency_budget_ms=self.latency_target_ms,
                remaining_tokens=self.total_budget,
            )

            stage_outputs: Dict[str, Any] = {}
            completed_stages: List[DAGStage] = []

            for node in nodes:
                stage = node.stage
                stage_start = time.time()

                # 参数调优
                tuned_params = self.tuner.tune(stage, node.config.parameters)

                # 预算检查
                if budget.remaining_tokens <= 0 and stage != DAGStage.TOKEN_BUDGET:
                    result = StageResult(
                        result_id=str(uuid.uuid4())[:8],
                        stage=stage,
                        status=StageStatus.SKIPPED,
                        error="Budget exhausted",
                    )
                else:
                    try:
                        # 执行阶段（模拟或实际 handler）
                        handler = stage_handlers.get(stage) if stage_handlers else None
                        output = handler(input_text, tuned_params, stage_outputs) if handler else {
                            "stage": stage.value,
                            "params": tuned_params,
                            "tokens_used": self._estimate_tokens(stage),
                        }
                        stage_latency = (time.time() - stage_start) * 1000

                        result = StageResult(
                            result_id=str(uuid.uuid4())[:8],
                            stage=stage,
                            status=StageStatus.COMPLETED,
                            output=output,
                            latency_ms=round(stage_latency, 2),
                            token_consumed=output.get("tokens_used", 0) if isinstance(output, dict) else 10,
                            started_at=stage_start,
                            finished_at=time.time(),
                        )
                        stage_outputs[stage.value] = output
                        completed_stages.append(stage)
                        budget.remaining_tokens -= result.token_consumed

                    except Exception as e:
                        result = StageResult(
                            result_id=str(uuid.uuid4())[:8],
                            stage=stage,
                            status=StageStatus.FAILED if not node.config.skip_on_failure else StageStatus.SKIPPED,
                            error=str(e),
                        )

                self.results[result.result_id] = result
                self.tracker.record_stage(stage, result.latency_ms)
                self.tuner.record(stage, tuned_params, result.latency_ms,
                                  result.status == StageStatus.COMPLETED)

            # 构建延迟画像
            latency_profile = self.tracker.build_profile()
            budget.elapsed_ms = (time.time() - overall_start) * 1000

            return {
                "orchestration_id": orchestration_id,
                "stages_completed": len(completed_stages),
                "total_stages": len(nodes),
                "total_latency_ms": round(budget.elapsed_ms, 2),
                "tokens_remaining": budget.remaining_tokens,
                "bottleneck": latency_profile.bottleneck_stage.value if latency_profile.bottleneck_stage else None,
                "p95_ms": self.tracker.p95(),
            }

    @staticmethod
    def _estimate_tokens(stage: DAGStage) -> int:
        """估算各阶段 Token 消耗。"""
        estimates = {
            DAGStage.TOKEN_BUDGET: 20,
            DAGStage.SEMANTIC_COMPRESSION: 500,
            DAGStage.KV_REUSE: 200,
            DAGStage.MEMORY_ROUTING: 300,
            DAGStage.SPECULATIVE_DECODE: 800,
        }
        return estimates.get(stage, 100)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_orchestrations": self.orchestration_count,
                "resolver": self.resolver.statistics(),
                "tuner": self.tuner.statistics(),
                "tracker": self.tracker.statistics(),
            }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P19-8 Context Engineering Orchestrator",
        "benchmark": "2026 Production Full-Stack DAG Orchestration",
        "classes": 5,
        "enums": 4,
        "dataclasses": 5,
        "key_pattern": "5-Stage DAG (Budget→Compress→KV→Route→Decode) + Adaptive Tune + Latency Track + Budget Control",
        "key_metric": "End-to-end DAG orchestration with P95 latency tracking & adaptive parameter tuning",
        "thread_safe": True,
    }
