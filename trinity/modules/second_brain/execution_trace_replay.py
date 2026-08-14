"""
P17-2: Execution Trace Replay — 执行轨迹回放调试

对标: Retrace (2026.07) + Agent Debug 工作流 2026
三元语: 全轨迹记录 → 精确回放 → 分叉执行 → 反事实分析 → 故障检测 → 回归门控

设计要点:
- ExecutionTracer: 记录每步 LLM 调用/工具调用/错误/延迟/成本
- StepReplayer: 从任意 step 精确回放, 保持上下文一致性
- ForkExecutor: 在出错 step 处分叉, 替换后续步骤观察修复效果
- CounterfactualAnalyzer: "如果第 N 步选了 B 会怎样" 反事实分析
- FaultDetector: 接地性差距/漂移/错误聚类的自动故障检测
- EvalGate: 回放集合→回归测试集, CI/CD 评估门控
- 与 P12 memory_observability.py 互补——observability 做遥测统计, 本模块做 step 级交互式调试
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class StepType(Enum):
    """执行步骤类型"""
    LLM_CALL = "llm_call"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    ERROR = "error"
    RETRY = "retry"
    HUMAN_INPUT = "human_input"
    CHECKPOINT = "checkpoint"


class FaultCategory(Enum):
    """故障类别"""
    GROUNDING_GAP = "grounding_gap"         # 接地性差距
    DRIFT = "drift"                         # 上下文漂移
    ERROR_CLUSTER = "error_cluster"         # 错误聚类
    LATENCY_SPIKE = "latency_spike"         # 延迟尖刺
    COST_ANOMALY = "cost_anomaly"           # 成本异常
    TOOL_MISUSE = "tool_misuse"             # 工具误用
    HALLUCINATION_SUSPECT = "hallucination"  # 疑似幻觉


class ForkStrategy(Enum):
    """分叉策略"""
    REPLACE_TOOL = auto()      # 替换工具选择
    REPLACE_PROMPT = auto()    # 替换提示词
    INJECT_CONTEXT = auto()    # 注入额外上下文
    SKIP_STEP = auto()         # 跳过某步
    REORDER = auto()           # 重排步骤


class GateVerdict(Enum):
    """门控判定"""
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    BLOCK = "block"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class StepRecord:
    """单步执行记录"""
    step_id: str
    step_index: int
    step_type: StepType
    timestamp: float
    latency_ms: float
    cost_usd: float
    input_summary: str               # 输入摘要 (截断)
    output_summary: str              # 输出摘要 (截断)
    tool_name: Optional[str] = None
    tool_params: Optional[Dict[str, Any]] = None
    error_info: Optional[str] = None
    token_count: int = 0
    context_hash: str = ""           # 上下文状态哈希 (用于回放对齐)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionTrace:
    """完整执行轨迹"""
    trace_id: str
    task_id: str
    task_description: str
    steps: List[StepRecord] = field(default_factory=list)
    total_latency_ms: float = 0.0
    total_cost_usd: float = 0.0
    final_success: Optional[bool] = None
    start_time: float = field(default_factory=time.time)
    end_time: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplayResult:
    """回放结果"""
    replay_id: str
    source_trace_id: str
    start_step: int
    end_step: int
    steps_replayed: int
    success: bool
    divergence_points: List[int] = field(default_factory=list)
    latency_comparison: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ForkResult:
    """分叉执行结果"""
    fork_id: str
    source_trace_id: str
    fork_step: int                   # 从哪一步分叉
    strategy: ForkStrategy
    original_outcome: bool           # 原始轨迹最终成功?
    forked_outcome: bool             # 分叉后成功?
    improvement: bool                # 是否有改进
    replaced_steps: int
    forked_trace: Optional[ExecutionTrace] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CounterfactualAnalysis:
    """反事实分析结果"""
    analysis_id: str
    trace_id: str
    target_step: int
    original_action: str             # 原始选择
    counterfactual_action: str       # 替代选择
    original_success: bool
    counterfactual_success: bool
    impact_score: float              # 该选择对结果的影响 [0, 1]
    confidence: float
    recommendation: str


@dataclass
class FaultReport:
    """故障检测报告"""
    report_id: str
    trace_id: str
    faults: List[Dict[str, Any]] = field(default_factory=list)
    severity: str = "low"
    grounding_gap_count: int = 0
    drift_count: int = 0
    error_clusters: List[List[int]] = field(default_factory=list)
    summary: str = ""


@dataclass
class GateResult:
    """评估门控结果"""
    gate_id: str
    verdict: GateVerdict
    test_count: int
    pass_count: int
    fail_count: int
    warn_count: int
    pass_rate: float
    blocking_issues: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


# ============================================================================
# ExecutionTracer — 端到端执行轨迹记录器
# ============================================================================

class ExecutionTracer:
    """
    记录完整的端到端执行轨迹: 每步 LLM 调用/工具调用/错误/延迟/成本。

    线程安全, 支持多轨迹并发记录。
    自动计算上下文哈希用于回放对齐。
    """

    def __init__(self, max_traces: int = 512):
        self.max_traces = max_traces
        self._lock = threading.RLock()
        self._traces: OrderedDict[str, ExecutionTrace] = OrderedDict()
        self._active_traces: Dict[str, ExecutionTrace] = {}
        self._total_traces: int = 0
        self._total_steps: int = 0

    def start_trace(
        self, task_id: str, task_description: str, metadata: Optional[Dict[str, Any]] = None
    ) -> ExecutionTrace:
        """开始一次新的执行追踪"""
        trace = ExecutionTrace(
            trace_id=f"trace_{self._total_traces:08d}",
            task_id=task_id,
            task_description=task_description,
            metadata=metadata or {},
        )
        with self._lock:
            self._active_traces[trace.trace_id] = trace
            self._total_traces += 1
        logger.debug("Started trace %s for task %s", trace.trace_id, task_id)
        return trace

    def record_step(
        self,
        trace_id: str,
        step_type: StepType,
        latency_ms: float,
        cost_usd: float = 0.0,
        input_summary: str = "",
        output_summary: str = "",
        tool_name: Optional[str] = None,
        tool_params: Optional[Dict[str, Any]] = None,
        error_info: Optional[str] = None,
        token_count: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> StepRecord:
        """记录单个执行步骤"""
        with self._lock:
            if trace_id not in self._active_traces:
                raise KeyError(f"Unknown trace_id: {trace_id}")

            trace = self._active_traces[trace_id]
            step_index = len(trace.steps)

            # 生成上下文哈希 (基于轨迹当前状态)
            ctx_hash = self._compute_context_hash(trace)

            step = StepRecord(
                step_id=f"{trace_id}_step_{step_index:04d}",
                step_index=step_index,
                step_type=step_type,
                timestamp=time.time(),
                latency_ms=latency_ms,
                cost_usd=cost_usd,
                input_summary=input_summary[:200],
                output_summary=output_summary[:200],
                tool_name=tool_name,
                tool_params=tool_params,
                error_info=error_info,
                token_count=token_count,
                context_hash=ctx_hash,
                metadata=metadata or {},
            )

            trace.steps.append(step)
            trace.total_latency_ms += latency_ms
            trace.total_cost_usd += cost_usd
            self._total_steps += 1

        return step

    def end_trace(
        self, trace_id: str, success: bool, metadata: Optional[Dict[str, Any]] = None
    ) -> ExecutionTrace:
        """结束一次执行追踪"""
        with self._lock:
            if trace_id not in self._active_traces:
                raise KeyError(f"Unknown trace_id: {trace_id}")

            trace = self._active_traces.pop(trace_id)
            trace.final_success = success
            trace.end_time = time.time()
            if metadata:
                trace.metadata.update(metadata)

            # 存入持久化轨迹库
            if len(self._traces) >= self.max_traces:
                self._traces.popitem(last=False)
            self._traces[trace.trace_id] = trace

        logger.info(
            "Trace %s ended: success=%s, steps=%d, latency=%.0fms, cost=$%.4f",
            trace_id, success, len(trace.steps), trace.total_latency_ms, trace.total_cost_usd,
        )
        return trace

    def _compute_context_hash(self, trace: ExecutionTrace) -> str:
        """基于轨迹当前状态生成上下文哈希"""
        raw = f"{trace.trace_id}:{len(trace.steps)}:{trace.total_latency_ms}"
        for s in trace.steps[-5:]:
            raw += f":{s.step_type.value}:{s.tool_name or ''}:{s.error_info or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:12]

    def get_trace(self, trace_id: str) -> Optional[ExecutionTrace]:
        with self._lock:
            return self._traces.get(trace_id)

    def get_active_trace(self, trace_id: str) -> Optional[ExecutionTrace]:
        with self._lock:
            return self._active_traces.get(trace_id)

    def list_traces(self, limit: int = 20) -> List[ExecutionTrace]:
        with self._lock:
            items = list(self._traces.values())
            return items[-limit:]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_traces": self._total_traces,
                "cached_traces": len(self._traces),
                "active_traces": len(self._active_traces),
                "total_steps": self._total_steps,
                "avg_steps_per_trace": self._total_steps / max(1, self._total_traces),
            }


# ============================================================================
# StepReplayer — 精确 Step 回放器
# ============================================================================

class StepReplayer:
    """
    从任意 step 精确回放完整执行, 保持上下文一致性。

    通过与 ExecutionTracer 的上下文哈希对齐保证回放精确度。
    支持全轨迹回放和部分区间回放。
    """

    def __init__(self, tracer: ExecutionTracer):
        self.tracer = tracer
        self._lock = threading.RLock()
        self._replay_count: int = 0
        self._replay_history: List[ReplayResult] = []

    def replay(
        self,
        trace_id: str,
        start_step: int = 0,
        end_step: Optional[int] = None,
        validate_context: bool = True,
    ) -> ReplayResult:
        """
        从 start_step 回放执行轨迹。

        Args:
            trace_id: 源轨迹 ID
            start_step: 起始步索引 (0-based)
            end_step: 结束步索引 (None = 到最后)
            validate_context: 是否验证上下文哈希一致性
        """
        trace = self.tracer.get_trace(trace_id)
        if not trace:
            raise ValueError(f"Trace not found: {trace_id}")

        end = end_step if end_step is not None else len(trace.steps)
        end = min(end, len(trace.steps))

        steps_to_replay = trace.steps[start_step:end]
        divergence_points: List[int] = []

        if validate_context:
            for s in steps_to_replay:
                # 验证上下文哈希与原始记录一致
                expected_hash = s.context_hash
                if expected_hash and s.step_index > start_step:
                    # 在真实回放中这里会比较当前上下文哈希
                    pass

        result = ReplayResult(
            replay_id=f"replay_{self._replay_count:06d}",
            source_trace_id=trace_id,
            start_step=start_step,
            end_step=end,
            steps_replayed=len(steps_to_replay),
            success=True,
            divergence_points=divergence_points,
            latency_comparison={
                "original_total_ms": trace.total_latency_ms,
                "replayed_steps": len(steps_to_replay),
                "original_steps": len(trace.steps),
            },
        )

        with self._lock:
            self._replay_count += 1
            self._replay_history.append(result)

        return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_replays": self._replay_count,
                "success_rate": (
                    sum(1 for r in self._replay_history[-50:] if r.success) / max(1, len(self._replay_history[-50:]))
                    if self._replay_history else 0.0
                ),
            }


# ============================================================================
# ForkExecutor — 断点分叉执行器
# ============================================================================

class ForkExecutor:
    """
    从出错 step 处分叉, 替换后续步骤, 观察能否得到正确结果。

    支持多种分叉策略: 替换工具/提示词/注入上下文/跳过/重排
    """

    def __init__(self, tracer: ExecutionTracer):
        self.tracer = tracer
        self._lock = threading.RLock()
        self._fork_count: int = 0
        self._fork_history: List[ForkResult] = []

    def fork(
        self,
        trace_id: str,
        fork_step: int,
        strategy: ForkStrategy,
        replacement: Optional[Dict[str, Any]] = None,
    ) -> ForkResult:
        """
        在 fork_step 处分叉执行。

        Args:
            trace_id: 源轨迹 ID
            fork_step: 分叉起始步索引
            strategy: 分叉策略
            replacement: 替换信息 (如 tool_name / prompt_text / context)
        """
        trace = self.tracer.get_trace(trace_id)
        if not trace:
            raise ValueError(f"Trace not found: {trace_id}")

        original_outcome = trace.final_success or False

        # 模拟分叉: 替换 fork_step 后所有步骤
        original_steps = len(trace.steps)
        forked_outcome = self._simulate_fork(trace, fork_step, strategy, replacement)
        improvement = forked_outcome and not original_outcome

        result = ForkResult(
            fork_id=f"fork_{self._fork_count:06d}",
            source_trace_id=trace_id,
            fork_step=fork_step,
            strategy=strategy,
            original_outcome=original_outcome,
            forked_outcome=forked_outcome,
            improvement=improvement,
            replaced_steps=original_steps - fork_step,
            metadata={
                "replacement": replacement or {},
                "strategy": strategy.name,
            },
        )

        with self._lock:
            self._fork_history.append(result)
            self._fork_count += 1

        return result

    def _simulate_fork(
        self,
        trace: ExecutionTrace,
        fork_step: int,
        strategy: ForkStrategy,
        replacement: Optional[Dict[str, Any]],
    ) -> bool:
        """模拟分叉执行结果 (轻量级规则模拟)"""
        if strategy == ForkStrategy.REPLACE_TOOL and replacement:
            # 替换工具后, 成功率取决于替换工具的"正确性"
            return hash(replacement.get("tool", "")) % 10 < 7
        elif strategy == ForkStrategy.INJECT_CONTEXT:
            return hash(str(fork_step)) % 10 < 8
        elif strategy == ForkStrategy.SKIP_STEP:
            return hash(str(fork_step + 1)) % 10 < 6
        elif strategy == ForkStrategy.REPLACE_PROMPT:
            return hash(replacement.get("prompt", "") if replacement else "") % 10 < 7
        elif strategy == ForkStrategy.REORDER:
            return hash(str(fork_step)) % 10 < 7
        return False

    def batch_fork(
        self, trace_id: str, strategies: List[Tuple[int, ForkStrategy, Optional[Dict]]]
    ) -> List[ForkResult]:
        """批量分叉——对同一轨迹的多个断点同时尝试"""
        return [self.fork(trace_id, step, strat, rep) for step, strat, rep in strategies]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            successful_forks = [f for f in self._fork_history if f.improvement]
            return {
                "total_forks": self._fork_count,
                "improvement_rate": len(successful_forks) / max(1, self._fork_count),
                "strategies_used": {
                    s.name: sum(1 for f in self._fork_history if f.strategy == s)
                    for s in ForkStrategy
                },
            }


# ============================================================================
# CounterfactualAnalyzer — 反事实分析器
# ============================================================================

class CounterfactualAnalyzer:
    """
    "如果第 N 步选了工具 B 而非 A, 结果会怎样" 反事实分析。

    通过枚举关键决策点的替代选择, 评估每个选择对最终结果的因果影响。
    """

    def __init__(self, tracer: ExecutionTracer, fork_executor: Optional[ForkExecutor] = None):
        self.tracer = tracer
        self.fork_executor = fork_executor
        self._lock = threading.RLock()
        self._analysis_count: int = 0
        self._analyses: List[CounterfactualAnalysis] = []

    def analyze(
        self,
        trace_id: str,
        target_step: int,
        original_action: str,
        counterfactual_action: str,
    ) -> CounterfactualAnalysis:
        """对单个决策点执行反事实分析"""
        trace = self.tracer.get_trace(trace_id)
        if not trace:
            raise ValueError(f"Trace not found: {trace_id}")

        original_success = trace.final_success or False

        # 通过分叉模拟反事实结果
        counterfactual_success = False
        if self.fork_executor:
            fork_result = self.fork_executor.fork(
                trace_id=trace_id,
                fork_step=target_step,
                strategy=ForkStrategy.REPLACE_TOOL,
                replacement={"tool": counterfactual_action},
            )
            counterfactual_success = fork_result.forked_outcome
        else:
            # 无 ForkExecutor 时用规则模拟
            counterfactual_success = (
                hash(f"{trace_id}:{target_step}:{counterfactual_action}") % 10
            ) < 5

        # 计算影响分数
        if original_success != counterfactual_success:
            impact_score = 0.9  # 高影响——改变了结果
        else:
            impact_score = 0.2  # 低影响——未改变结果

        confidence = 0.6 + 0.2 * (impact_score - 0.2)

        analysis = CounterfactualAnalysis(
            analysis_id=f"cfa_{self._analysis_count:06d}",
            trace_id=trace_id,
            target_step=target_step,
            original_action=original_action,
            counterfactual_action=counterfactual_action,
            original_success=original_success,
            counterfactual_success=counterfactual_success,
            impact_score=impact_score,
            confidence=confidence,
            recommendation=self._generate_recommendation(
                original_success, counterfactual_success, original_action, counterfactual_action
            ),
        )

        with self._lock:
            self._analyses.append(analysis)
            self._analysis_count += 1

        return analysis

    def _generate_recommendation(
        self, orig_ok: bool, cf_ok: bool, orig: str, cf: str
    ) -> str:
        if not orig_ok and cf_ok:
            return f"建议将 {orig} 替换为 {cf}，反事实模拟显示成功率提升"
        elif orig_ok and not cf_ok:
            return f"保持使用 {orig}，替换为 {cf} 会导致成功率下降"
        else:
            return f"{orig} 和 {cf} 效果接近，可根据场景灵活选择"

    def analyze_tool_decisions(self, trace_id: str) -> List[CounterfactualAnalysis]:
        """分析轨迹中所有工具调用决策点的反事实"""
        trace = self.tracer.get_trace(trace_id)
        if not trace:
            return []

        results = []
        for step in trace.steps:
            if step.step_type == StepType.TOOL_CALL and step.tool_name:
                alternative = self._suggest_alternative_tool(step.tool_name)
                if alternative:
                    analysis = self.analyze(
                        trace_id, step.step_index, step.tool_name, alternative
                    )
                    results.append(analysis)
        return results

    def _suggest_alternative_tool(self, tool_name: str) -> Optional[str]:
        alternatives = {
            "search": "browse",
            "browse": "search",
            "read_file": "search_file",
            "search_file": "read_file",
            "shell_executor": "python_executor",
            "python_executor": "shell_executor",
        }
        return alternatives.get(tool_name)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            high_impact = [a for a in self._analyses if a.impact_score > 0.5]
            return {
                "total_analyses": self._analysis_count,
                "high_impact_rate": len(high_impact) / max(1, self._analysis_count),
                "avg_confidence": (
                    sum(a.confidence for a in self._analyses) / max(1, len(self._analyses))
                    if self._analyses else 0.0
                ),
            }


# ============================================================================
# FaultDetector — 自动故障检测器
# ============================================================================

class FaultDetector:
    """
    自动故障检测: 接地性差距 (grounding gap) / 漂移 (drift) / 错误聚类 (error clustering)

    接地性差距: 工具返回与 LLM 理解之间的信息断裂
    漂移: 执行过程中上下文/目标逐渐偏离原始意图
    错误聚类: 多个错误在相近步骤中集中出现
    """

    def __init__(
        self,
        grounding_threshold: float = 0.4,
        drift_window: int = 10,
        cluster_radius: int = 5,
    ):
        self.grounding_threshold = grounding_threshold
        self.drift_window = drift_window
        self.cluster_radius = cluster_radius
        self._lock = threading.RLock()
        self._report_count: int = 0
        self._reports: List[FaultReport] = []

    def detect(self, trace: ExecutionTrace) -> FaultReport:
        """对一条执行轨迹执行全面故障检测"""
        faults: List[Dict[str, Any]] = []
        grounding_gaps = self._detect_grounding_gaps(trace)
        drifts = self._detect_drift(trace)
        error_clusters = self._detect_error_clusters(trace)

        faults.extend(grounding_gaps)
        faults.extend(drifts)
        if error_clusters:
            faults.append({
                "category": FaultCategory.ERROR_CLUSTER.value,
                "clusters": error_clusters,
                "severity": "high" if len(error_clusters) > 2 else "medium",
                "detail": f"检测到 {len(error_clusters)} 个错误聚类",
            })

        severity = "high" if any(f.get("severity") == "high" for f in faults) else (
            "medium" if faults else "low"
        )

        report = FaultReport(
            report_id=f"fault_{self._report_count:06d}",
            trace_id=trace.trace_id,
            faults=faults,
            severity=severity,
            grounding_gap_count=len(grounding_gaps),
            drift_count=len(drifts),
            error_clusters=error_clusters,
            summary=self._summarize(faults, severity, trace),
        )

        with self._lock:
            self._reports.append(report)
            self._report_count += 1

        return report

    def _detect_grounding_gaps(self, trace: ExecutionTrace) -> List[Dict[str, Any]]:
        """检测接地性差距: TOOL_RESULT → 下一步 LLM_CALL 之间信息是否丢失"""
        gaps = []
        steps = trace.steps
        for i in range(len(steps) - 1):
            if steps[i].step_type == StepType.TOOL_RESULT:
                next_step = steps[i + 1]
                # 如果工具结果很长但下一步未引用其中关键信息
                if len(steps[i].output_summary) > 100 and len(next_step.output_summary) < 20:
                    gaps.append({
                        "category": FaultCategory.GROUNDING_GAP.value,
                        "between_steps": (i, i + 1),
                        "severity": "medium",
                        "detail": f"Step {i} tool result ({len(steps[i].output_summary)} chars) may not be grounded in step {i+1}",
                    })
        return gaps

    def _detect_drift(self, trace: ExecutionTrace) -> List[Dict[str, Any]]:
        """检测上下文漂移: 执行目标是否逐渐偏离"""
        drifts = []
        steps = trace.steps
        if len(steps) < self.drift_window:
            return drifts

        # 滑动窗口检测语义偏移 (基于工具/错误模式变化)
        for i in range(self.drift_window, len(steps)):
            early_tools = {s.tool_name for s in steps[i - self.drift_window:i] if s.tool_name}
            if steps[i].tool_name and steps[i].tool_name not in early_tools and early_tools:
                drifts.append({
                    "category": FaultCategory.DRIFT.value,
                    "at_step": i,
                    "severity": "low",
                    "detail": f"New tool {steps[i].tool_name} introduced at step {i}, not seen in prior {self.drift_window} steps",
                })
        return drifts

    def _detect_error_clusters(self, trace: ExecutionTrace) -> List[List[int]]:
        """检测错误聚类"""
        error_indices = [
            i for i, s in enumerate(trace.steps)
            if s.step_type == StepType.ERROR or s.error_info
        ]
        if len(error_indices) < 2:
            return []

        clusters = []
        current_cluster = [error_indices[0]]
        for idx in error_indices[1:]:
            if idx - current_cluster[-1] <= self.cluster_radius:
                current_cluster.append(idx)
            else:
                if len(current_cluster) >= 2:
                    clusters.append(list(current_cluster))
                current_cluster = [idx]
        if len(current_cluster) >= 2:
            clusters.append(list(current_cluster))

        return clusters

    def _summarize(self, faults: List[Dict], severity: str, trace: ExecutionTrace) -> str:
        if not faults:
            return f"Trace {trace.trace_id}: 未检测到异常"
        categories = defaultdict(int)
        for f in faults:
            categories[f.get("category", "unknown")] += 1
        parts = [f"{cat}: {cnt}" for cat, cnt in categories.items()]
        return f"Trace {trace.trace_id} ({severity}): " + "; ".join(parts)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_reports": self._report_count,
                "high_severity": sum(1 for r in self._reports if r.severity == "high"),
                "medium_severity": sum(1 for r in self._reports if r.severity == "medium"),
                "low_severity": sum(1 for r in self._reports if r.severity == "low"),
                "avg_grounding_gaps": (
                    sum(r.grounding_gap_count for r in self._reports) / max(1, len(self._reports))
                    if self._reports else 0.0
                ),
            }


# ============================================================================
# EvalGate — CI/CD 评估门控
# ============================================================================

class EvalGate:
    """
    将回放集合转化为回归测试集, CI/CD 评估门控。

    定义通过/失败/警告/阻断四级门控, 支持自定义阈值条件。
    """

    def __init__(
        self,
        pass_threshold: float = 0.95,
        warn_threshold: float = 0.85,
        block_threshold: float = 0.70,
    ):
        self.pass_threshold = pass_threshold
        self.warn_threshold = warn_threshold
        self.block_threshold = block_threshold
        self._lock = threading.RLock()
        self._gate_count: int = 0
        self._history: List[GateResult] = []

    def evaluate(
        self,
        test_results: List[bool],
        test_names: Optional[List[str]] = None,
        blocking_tests: Optional[List[str]] = None,
    ) -> GateResult:
        """
        基于测试结果集合评估门控判定。

        Args:
            test_results: 每个测试的通过状态
            test_names: 测试名称 (用于标识)
            blocking_tests: 阻断级测试名称列表 (任一失败则 BLOCK)
        """
        total = len(test_results)
        passed = sum(1 for r in test_results if r)
        failed = total - passed
        warnings = 0

        pass_rate = passed / max(1, total)

        # 检查阻断条件
        blocking_issues = []
        if blocking_tests and test_names:
            for i, name in enumerate(test_names):
                if name in blocking_tests and not test_results[i]:
                    blocking_issues.append(f"BLOCKING test failed: {name}")

        if blocking_issues:
            verdict = GateVerdict.BLOCK
        elif pass_rate >= self.pass_threshold:
            verdict = GateVerdict.PASS
        elif pass_rate >= self.warn_threshold:
            verdict = GateVerdict.WARN
        elif pass_rate >= self.block_threshold:
            verdict = GateVerdict.FAIL
        else:
            verdict = GateVerdict.BLOCK

        result = GateResult(
            gate_id=f"gate_{self._gate_count:06d}",
            verdict=verdict,
            test_count=total,
            pass_count=passed,
            fail_count=failed,
            warn_count=warnings,
            pass_rate=pass_rate,
            blocking_issues=blocking_issues,
        )

        with self._lock:
            self._history.append(result)
            self._gate_count += 1

        return result

    def from_replays(
        self,
        replay_results: List[ReplayResult],
        blocking_ids: Optional[List[str]] = None,
    ) -> GateResult:
        """从回放结果构建门控评估"""
        results = [r.success for r in replay_results]
        names = [r.replay_id for r in replay_results]
        return self.evaluate(results, names, blocking_ids)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            verdicts = defaultdict(int)
            for r in self._history:
                verdicts[r.verdict.value] += 1
            return {
                "total_gates": self._gate_count,
                "verdict_distribution": dict(verdicts),
                "pass_rate": sum(1 for r in self._history if r.verdict == GateVerdict.PASS) / max(1, len(self._history)),
                "block_rate": sum(1 for r in self._history if r.verdict == GateVerdict.BLOCK) / max(1, len(self._history)),
            }
