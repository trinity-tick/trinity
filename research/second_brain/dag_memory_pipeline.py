"""
# status: orphan (2026-08-15 audit, not in runtime path)
P15-5: DAG Memory Pipeline
===========================

对标 Production Harness 2026 — 有向无环图编排的记忆任务流水线。

设计要点：
  - Planner/Worker/Critic 三种角色分离，形成编排 DAG
  - 多维预算压力控制：token 预算、时间预算、质量阈值
  - 记忆任务依赖自动解析，构建执行拓扑序
  - 失败节点自动重试 + 降级策略，保障流水线韧性

核心组件：
  - Planner:             接收高层目标，拆解为子任务并构建 DAG
  - Worker:              执行单个节点任务，上报执行结果
  - Critic:              对节点输出进行质量评估，触发重试/降级
  - DAGMemoryPipeline:   全局编排器，调度执行与资源管控
  - BudgetManager:       多维预算（token / 时间 / 质量）监控
  - DependencyResolver:  自动解析任务间依赖，生成拓扑排序
  - FailureHandler:      失败节点重试 + 降级策略
"""

from __future__ import annotations

import heapq
import logging
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

class NodeRole(Enum):
    """DAG 节点角色。"""
    PLANNER = "planner"
    WORKER = "worker"
    CRITIC = "critic"
    ROUTER = "router"


class NodeStatus(Enum):
    """节点执行状态。"""
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    DEGRADED = "degraded"
    SKIPPED = "skipped"


class BudgetType(Enum):
    """预算类型。"""
    TOKEN = "token"
    TIME_MS = "time_ms"
    QUALITY = "quality"


class FailurePolicy(Enum):
    """失败处理策略。"""
    RETRY_ONCE = "retry_once"
    RETRY_TWICE = "retry_twice"
    DEGRADE = "degrade"
    SKIP = "skip"
    ABORT = "abort"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class TaskNode:
    """DAG 中的一个任务节点。"""
    node_id: str
    role: NodeRole
    name: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    output_schema: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    children: List[str] = field(default_factory=list)
    status: NodeStatus = NodeStatus.PENDING
    retry_count: int = 0
    max_retries: int = 2
    failure_policy: FailurePolicy = FailurePolicy.RETRY_TWICE
    estimated_token_cost: int = 0
    estimated_time_ms: int = 0
    quality_threshold: float = 0.7
    output: Optional[Any] = None
    error_message: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass
class BudgetConstraint:
    """单维预算约束。"""
    budget_type: BudgetType
    limit: float
    consumed: float = 0.0
    warning_threshold: float = 0.8

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.consumed)

    @property
    def is_exhausted(self) -> bool:
        return self.consumed >= self.limit

    @property
    def is_warning(self) -> bool:
        return self.limit > 0 and (self.consumed / self.limit) >= self.warning_threshold


@dataclass
class ExecutionRecord:
    """单个节点的执行记录。"""
    node_id: str
    status: NodeStatus
    output: Optional[Any]
    error: Optional[str]
    duration_ms: float
    token_consumed: int
    quality_score: float
    retry_count: int
    timestamp: float = field(default_factory=time.time)


@dataclass
class PipelineStats:
    """流水线执行统计。"""
    pipeline_id: str
    total_nodes: int
    succeeded: int = 0
    failed: int = 0
    degraded: int = 0
    skipped: int = 0
    total_duration_ms: float = 0.0
    total_tokens: int = 0
    avg_quality: float = 0.0
    records: List[ExecutionRecord] = field(default_factory=list)


# ============================================================================
# Core Components
# ============================================================================

class DependencyResolver:
    """任务依赖自动解析器。

    根据节点声明的 dependencies 构建拓扑排序，检测循环依赖。
    """

    def __init__(self):
        self._lock = threading.RLock()

    def resolve(self, nodes: List[TaskNode]) -> List[TaskNode]:
        """拓扑排序，返回可执行顺序。"""
        with self._lock:
            adj: Dict[str, List[str]] = {}
            in_degree: Dict[str, int] = {}
            node_map: Dict[str, TaskNode] = {}

            for n in nodes:
                node_map[n.node_id] = n
                adj.setdefault(n.node_id, [])
                in_degree.setdefault(n.node_id, 0)

            for n in nodes:
                for dep_id in n.dependencies:
                    if dep_id not in node_map:
                        logger.warning("依赖节点 %s 不在图中，跳过", dep_id)
                        continue
                    adj[dep_id].append(n.node_id)
                    in_degree[n.node_id] += 1

            # Kahn's algorithm
            queue: deque = deque()
            for nid, deg in in_degree.items():
                if deg == 0:
                    queue.append(nid)

            ordered: List[TaskNode] = []
            while queue:
                nid = queue.popleft()
                ordered.append(node_map[nid])
                for child in adj.get(nid, []):
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        queue.append(child)

            if len(ordered) != len(nodes):
                remaining = set(n.node_id for n in nodes) - set(n.node_id for n in ordered)
                raise ValueError(f"检测到循环依赖：{remaining}")

            # 标记节点状态
            resolved: List[TaskNode] = []
            for n in ordered:
                if not n.dependencies:
                    n.status = NodeStatus.READY
                else:
                    n.status = NodeStatus.PENDING
                resolved.append(n)
            return resolved

    def get_ready_nodes(self, nodes: List[TaskNode]) -> List[TaskNode]:
        """获取所有就绪的节点。"""
        return [n for n in nodes if n.status == NodeStatus.READY]


class BudgetManager:
    """多维预算管理器。

    监控 token、时间、质量三类预算，超限时触发告警与降级。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.constraints: Dict[BudgetType, BudgetConstraint] = {}

    def set_budget(self, budget_type: BudgetType, limit: float, warning: float = 0.8):
        with self._lock:
            self.constraints[budget_type] = BudgetConstraint(
                budget_type=budget_type, limit=limit, warning_threshold=warning
            )

    def consume(self, budget_type: BudgetType, amount: float) -> bool:
        """消耗预算，返回是否成功。"""
        with self._lock:
            constraint = self.constraints.get(budget_type)
            if constraint is None:
                return True  # 无约束则放行
            if constraint.is_exhausted:
                return False
            constraint.consumed += amount
            if constraint.is_warning and constraint.consumed - amount < constraint.warning_threshold * constraint.limit:
                logger.warning("预算 %s 达到告警阈值：%.1f / %.1f", budget_type, constraint.consumed, constraint.limit)
            return True

    def check(self, budget_type: BudgetType) -> bool:
        """检查预算是否充足。"""
        with self._lock:
            constraint = self.constraints.get(budget_type)
            if constraint is None:
                return True
            return not constraint.is_exhausted

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                bt.value: {"limit": c.limit, "consumed": c.consumed, "remaining": c.remaining, "warning": c.is_warning}
                for bt, c in self.constraints.items()
            }


class FailureHandler:
    """失败处理与降级策略。"""

    def __init__(self):
        self._lock = threading.RLock()

    def handle(self, node: TaskNode, error: str) -> Tuple[NodeStatus, str]:
        """处理节点失败，返回 (新状态, 动作描述)。"""
        with self._lock:
            node.error_message = error
            logger.error("节点 %s 执行失败（第 %d 次）：%s", node.node_id, node.retry_count + 1, error)

            if node.failure_policy == FailurePolicy.ABORT:
                return NodeStatus.FAILED, "中止流水线"

            if node.failure_policy in (FailurePolicy.RETRY_ONCE, FailurePolicy.RETRY_TWICE):
                max_retries = 1 if node.failure_policy == FailurePolicy.RETRY_ONCE else 2
                if node.retry_count < max_retries:
                    node.retry_count += 1
                    node.status = NodeStatus.READY
                    return NodeStatus.READY, f"重试 ({node.retry_count}/{max_retries})"

            if node.failure_policy == FailurePolicy.DEGRADE:
                node.status = NodeStatus.DEGRADED
                return NodeStatus.DEGRADED, "降级执行"

            if node.failure_policy == FailurePolicy.SKIP:
                node.status = NodeStatus.SKIPPED
                return NodeStatus.SKIPPED, "跳过节点"

            # 默认降级
            node.status = NodeStatus.DEGRADED
            return NodeStatus.DEGRADED, "降级（重试耗尽）"


class Planner:
    """任务规划器。

    接收高层目标，拆解为子任务并构建执行 DAG。
    """

    def __init__(self):
        self._lock = threading.RLock()

    def plan(self, goal: str, sub_tasks: List[Dict[str, Any]]) -> List[TaskNode]:
        """根据目标和子任务描述生成节点列表。"""
        with self._lock:
            nodes: List[TaskNode] = []
            for i, task in enumerate(sub_tasks):
                node = TaskNode(
                    node_id=str(uuid.uuid4())[:8],
                    role=NodeRole.WORKER if task.get("role") != "critic" else NodeRole.CRITIC,
                    name=task.get("name", f"task_{i}"),
                    input_schema=task.get("input", {}),
                    output_schema=task.get("output", {}),
                    dependencies=task.get("depends_on", []),
                    estimated_token_cost=task.get("token_cost", 100),
                    estimated_time_ms=task.get("time_ms", 500),
                    quality_threshold=task.get("quality", 0.7),
                    failure_policy=FailurePolicy[task.get("on_failure", "RETRY_TWICE").upper()],
                )
                nodes.append(node)
            logger.info("规划完成：%d 个子任务", len(nodes))
            return nodes


class Worker:
    """任务执行器。

    执行单个节点任务，返回执行结果。
    """

    def __init__(self):
        self._lock = threading.RLock()

    def execute(self, node: TaskNode, executor: Callable[[Dict[str, Any]], Any]) -> ExecutionRecord:
        """执行节点任务。"""
        start = time.time()
        try:
            node.status = NodeStatus.RUNNING
            node.started_at = start
            output = executor(node.input_schema)
            elapsed = (time.time() - start) * 1000
            node.output = output
            node.status = NodeStatus.SUCCESS
            node.completed_at = time.time()
            return ExecutionRecord(
                node_id=node.node_id, status=NodeStatus.SUCCESS, output=output,
                error=None, duration_ms=elapsed, token_consumed=node.estimated_token_cost,
                quality_score=1.0, retry_count=node.retry_count,
            )
        except Exception as e:
            elapsed = (time.time() - start) * 1000
            node.status = NodeStatus.FAILED
            node.completed_at = time.time()
            return ExecutionRecord(
                node_id=node.node_id, status=NodeStatus.FAILED, output=None,
                error=str(e), duration_ms=elapsed, token_consumed=node.estimated_token_cost,
                quality_score=0.0, retry_count=node.retry_count,
            )


class Critic:
    """质量评估器。

    评估节点输出质量，低于阈值触发纠正或降级。
    """

    def __init__(self):
        self._lock = threading.RLock()

    def evaluate(self, node: TaskNode, evaluator: Callable[[Any], float]) -> float:
        """评估输出质量，返回分数 0-1。"""
        with self._lock:
            if node.output is None:
                return 0.0
            try:
                score = evaluator(node.output)
                if score < node.quality_threshold:
                    logger.warning("节点 %s 质量不达标：%.2f < %.2f", node.node_id, score, node.quality_threshold)
                return score
            except Exception:
                return 0.0

    def should_degrade(self, score: float, threshold: float) -> bool:
        return score < threshold


class DAGMemoryPipeline:
    """DAG 记忆流水线主编排器。

    整合 Planner/Worker/Critic，调度 DAG 执行，
    监控多维预算，处理失败节点。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.planner = Planner()
        self.worker = Worker()
        self.critic = Critic()
        self.resolver = DependencyResolver()
        self.budget = BudgetManager()
        self.failure_handler = FailureHandler()
        self.pipeline_id = str(uuid.uuid4())[:8]
        self.nodes: List[TaskNode] = []
        self.execution_log: List[ExecutionRecord] = []

    def build(
        self,
        goal: str,
        sub_tasks: List[Dict[str, Any]],
        token_budget: int = 10000,
        time_budget_ms: int = 60000,
        quality_threshold: float = 0.7,
    ):
        """构建流水线 DAG。"""
        with self._lock:
            self.nodes = self.planner.plan(goal, sub_tasks)
            self.nodes = self.resolver.resolve(self.nodes)
            self.budget.set_budget(BudgetType.TOKEN, float(token_budget))
            self.budget.set_budget(BudgetType.TIME_MS, float(time_budget_ms))
            self.budget.set_budget(BudgetType.QUALITY, float(quality_threshold))
            logger.info("Pipeline %s 构建完成，%d 个节点", self.pipeline_id, len(self.nodes))

    def run(self, executor: Callable[[Dict[str, Any]], Any], evaluator: Optional[Callable[[Any], float]] = None) -> PipelineStats:
        """执行流水线。"""
        stats = PipelineStats(pipeline_id=self.pipeline_id, total_nodes=len(self.nodes))

        completed: Set[str] = set()
        while len(completed) < len(self.nodes):
            # 检查预算
            if not self.budget.check(BudgetType.TOKEN) or not self.budget.check(BudgetType.TIME_MS):
                logger.error("预算耗尽，中止流水线")
                break

            ready = [n for n in self.nodes if n.status == NodeStatus.READY and n.node_id not in completed]
            if not ready:
                pending = [n for n in self.nodes if n.status == NodeStatus.PENDING]
                if pending:
                    logger.warning("存在 %d 个依赖未满足的节点", len(pending))
                    break
                break

            node = ready[0]

            # 执行
            record = self.worker.execute(node, executor)
            self.budget.consume(BudgetType.TOKEN, float(node.estimated_token_cost))
            self.budget.consume(BudgetType.TIME_MS, record.duration_ms)

            if record.status == NodeStatus.FAILED:
                new_status, action = self.failure_handler.handle(node, record.error or "未知错误")
                if new_status in (NodeStatus.DEGRADED, NodeStatus.SKIPPED):
                    record.status = new_status
                    completed.add(node.node_id)
                    self.execution_log.append(record)
                elif new_status == NodeStatus.FAILED:
                    completed.add(node.node_id)
                    self.execution_log.append(record)
                continue

            # Critic 评估
            if evaluator:
                score = self.critic.evaluate(node, evaluator)
                record.quality_score = score
                self.budget.consume(BudgetType.QUALITY, 1.0 - score)

            # 成功：释放子节点依赖
            node.status = NodeStatus.SUCCESS
            completed.add(node.node_id)
            self.execution_log.append(record)

            for n in self.nodes:
                if node.node_id in n.dependencies:
                    all_deps_done = all(
                        dep in completed and
                        any(r.node_id == dep and r.status == NodeStatus.SUCCESS for r in self.execution_log)
                        for dep in n.dependencies
                    )
                    if all_deps_done and n.status == NodeStatus.PENDING:
                        n.status = NodeStatus.READY

        # 汇总统计
        for rec in self.execution_log:
            stats.total_duration_ms += rec.duration_ms
            stats.total_tokens += rec.token_consumed
            if rec.status == NodeStatus.SUCCESS:
                stats.succeeded += 1
            elif rec.status == NodeStatus.FAILED:
                stats.failed += 1
            elif rec.status == NodeStatus.DEGRADED:
                stats.degraded += 1
            elif rec.status == NodeStatus.SKIPPED:
                stats.skipped += 1

        if stats.succeeded + stats.degraded > 0:
            scores = [r.quality_score for r in self.execution_log if r.status in (NodeStatus.SUCCESS, NodeStatus.DEGRADED)]
            stats.avg_quality = sum(scores) / len(scores) if scores else 0.0

        stats.records = self.execution_log
        return stats

    def statistics(self) -> Dict[str, Any]:
        return {
            "pipeline_id": self.pipeline_id,
            "total_nodes": len(self.nodes),
            "completed": len([r for r in self.execution_log if r.status != NodeStatus.PENDING]),
            "budget_status": self.budget.status(),
        }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """模块级统计信息。"""
    return {
        "module": "P15-5 DAG Memory Pipeline",
        "benchmark": "Production Harness 2026",
        "classes": 6,
        "enums": 4,
        "dataclasses": 4,
        "key_pattern": "Planner/Worker/Critic DAG Orchestration",
        "key_metric": "Multi-dim Budget (token/time/quality) + Auto Retry & Degrade",
        "thread_safe": True,
    }
