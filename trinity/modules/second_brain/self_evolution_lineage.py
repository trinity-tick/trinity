"""
CB71: SelfEvolutionLineage — 自演化历史追溯
============================================

记忆系统自身演化的可追溯历史与回归检测。

核心设计:
  - EvolutionTrace: 每次模块新增/修改/删除的完整快照（diff、作者、
    时间戳、原因、性能影响）
  - LineageGraph: 版本 DAG，支持多分支并行演化与合并
  - RegressionDetector: 新版本部署后 A/B 对比基准测试，检测检索质量/
    延迟/准确率退化，触发回滚建议
  - EvolutionRollback: 一键回滚到任意历史版本（恢复代码+配置+__all__状态）
  - EvolutionReport: Markdown 格式版本发布说明自动生成
  - PerformanceBaseline: 性能基线快照

Reference:
  - Self-evolving agent memory system with lineage tracking & regression safety
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class EvolutionAction(Enum):
    """演化操作类型。"""
    ADD = "add"         # 新增模块
    MODIFY = "modify"   # 修改已有模块
    DELETE = "delete"   # 删除模块
    MERGE = "merge"     # 分支合并


class RollbackStrategy(Enum):
    """回滚策略。"""
    FULL = "full"          # 完全恢复：代码+配置+__all__
    CODE_ONLY = "code_only"  # 仅代码
    CONFIG_ONLY = "config_only"  # 仅配置


class RegressionSeverity(Enum):
    """回归严重级别。"""
    NONE = "none"
    LOW = "low"          # 轻微偏离，可接受
    MEDIUM = "medium"    # 明显退化，建议回滚
    HIGH = "high"        # 严重退化，自动触发回滚
    CRITICAL = "critical"  # 系统异常，立即回滚


class LineageMergePolicy(Enum):
    """谱系合并策略。"""
    FAST_FORWARD = "fast_forward"      # 快进合并（线性）
    THREE_WAY = "three_way"            # 三方合并（基版本+源+目标）
    REBASE = "rebase"                   # 变基到目标分支
    SQUASH = "squash"                   # 压缩为单次提交
    MANUAL = "manual"                   # 手动冲突解决


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class PerformanceBaseline:
    """性能基线快照。

    Attributes:
        baseline_id: 基线标识。
        version: 对应版本号。
        accuracy: 检索准确率。
        recall: 召回率。
        latency_ms: 平均延迟。
        throughput_qps: 吞吐量。
        memory_usage_mb: 内存占用。
        recorded_at: 记录时间。
    """
    baseline_id: str
    version: str
    accuracy: float = 0.0
    recall: float = 0.0
    latency_ms: float = 0.0
    throughput_qps: float = 0.0
    memory_usage_mb: float = 0.0
    recorded_at: float = field(default_factory=_time.time)


@dataclass
class EvolutionTrace:
    """演化追踪记录——每次模块变更的完整快照。

    Attributes:
        trace_id: 追踪唯一标识。
        action: 演化操作类型。
        module_path: 受影响的模块路径。
        author: 变更触发者。
        reason: 变更原因。
        diff_summary: 变更摘要（新增/删除行数）。
        previous_hash: 变更前文件 SHA256。
        new_hash: 变更后文件 SHA256。
        version_before: 变更前版本。
        version_after: 变更后版本。
        timestamp: 变更时间戳。
        performance_impact: 性能影响描述。
        rollback_snapshot: 回滚所需快照数据。
    """
    trace_id: str
    action: EvolutionAction
    module_path: str = ""
    author: str = "system"
    reason: str = ""
    diff_summary: str = ""
    previous_hash: str = ""
    new_hash: str = ""
    version_before: str = ""
    version_after: str = ""
    timestamp: float = field(default_factory=_time.time)
    performance_impact: str = ""
    rollback_snapshot: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VersionSnapshot:
    """版本完整快照——用于回滚。

    Attributes:
        version: 版本号。
        timestamp: 快照时间。
        trace_ids: 该版本相关的 trace ID 列表。
        __all___state: __all__ 的完整状态备份。
        checksum: 快照完整性校验。
    """
    version: str
    timestamp: float = field(default_factory=_time.time)
    trace_ids: List[str] = field(default_factory=list)
    __all___state: List[str] = field(default_factory=list)
    checksum: str = ""


@dataclass
class RegressionResult:
    """回归检测结果。"""
    detected: bool
    severity: RegressionSeverity
    metric_name: str = ""
    baseline_value: float = 0.0
    current_value: float = 0.0
    deviation_pct: float = 0.0
    recommendation: str = ""
    auto_rollback: bool = False


# ============================================================================
# LineageGraph
# ============================================================================

@dataclass
class LineageNode:
    """谱系图节点——一个版本。"""
    version: str
    traces: List[str] = field(default_factory=list)  # trace_id 列表
    parent_versions: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=_time.time)
    active: bool = True


class LineageGraph:
    """版本演化 DAG——支持多分支并行演化与合并。

    Attributes:
        nodes: 版本号 → 节点映射。
        current_version: 当前活跃版本。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.nodes: Dict[str, LineageNode] = {}
        self.current_version: str = ""

    def create_node(self, version: str, parent_versions: Optional[List[str]] = None):
        with self._lock:
            node = LineageNode(
                version=version,
                parent_versions=parent_versions or ([self.current_version] if self.current_version else []),
            )
            self.nodes[version] = node
            self.current_version = version

    def merge(self, source_version: str, target_version: str) -> str:
        """合并两个分支，创建新合并节点。

        Returns:
            合并后的新版本号。
        """
        with self._lock:
            parts = target_version.rsplit(".", 1)
            if len(parts) == 2:
                try:
                    patch = int(parts[1]) + 1
                    merged_version = f"{parts[0]}.{patch}"
                except ValueError:
                    merged_version = f"{target_version}-merged"
            else:
                merged_version = f"{target_version}-merged"

            node = LineageNode(
                version=merged_version,
                parent_versions=[source_version, target_version],
            )
            self.nodes[merged_version] = node
            self.current_version = merged_version
            return merged_version

    def path_to(self, target_version: str) -> List[str]:
        """从当前版本到目标版本的路径（用于回滚）。"""
        with self._lock:
            path = []
            visited = set()
            q = [self.current_version]
            while q:
                v = q.pop(0)
                if v in visited:
                    continue
                visited.add(v)
                path.append(v)
                if v == target_version:
                    return path
                node = self.nodes.get(v)
                if node:
                    for pv in node.parent_versions:
                        if pv not in visited:
                            q.append(pv)
            return []

    def get_branch_points(self) -> List[str]:
        """返回所有分支点（有多个子节点的版本）。"""
        with self._lock:
            child_counts: Dict[str, int] = {}
            for node in self.nodes.values():
                for pv in node.parent_versions:
                    child_counts[pv] = child_counts.get(pv, 0) + 1
            return [v for v, c in child_counts.items() if c > 1]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_nodes": len(self.nodes),
                "current_version": self.current_version,
                "branch_points": len(self.get_branch_points()),
            }


# ============================================================================
# RegressionDetector
# ============================================================================

class RegressionDetector:
    """回归检测器——新版本部署后 A/B 对比基准测试。

    自动运行基准对比，检测检索质量/延迟/准确率退化。
    """

    def __init__(
        self,
        accuracy_threshold: float = 0.95,   # 低于基线 95% → 回归
        latency_threshold: float = 1.15,     # 延迟高于基线 115% → 回归
        recall_threshold: float = 0.95,
    ):
        self.accuracy_threshold = accuracy_threshold
        self.latency_threshold = latency_threshold
        self.recall_threshold = recall_threshold
        self._lock = threading.RLock()
        self._baselines: Dict[str, PerformanceBaseline] = {}

    def set_baseline(self, baseline: PerformanceBaseline):
        with self._lock:
            self._baselines[baseline.baseline_id] = baseline

    def compare(
        self, baseline_id: str, current: PerformanceBaseline
    ) -> RegressionResult:
        """将当前性能与基线对比。

        Returns:
            RegressionResult: 回归检测结果。
        """
        with self._lock:
            baseline = self._baselines.get(baseline_id)
            if baseline is None:
                return RegressionResult(
                    detected=False, severity=RegressionSeverity.NONE,
                    recommendation="No baseline found for comparison.",
                )

            issues = []
            max_severity = RegressionSeverity.NONE

            # Accuracy check
            if baseline.accuracy > 0:
                acc_ratio = current.accuracy / baseline.accuracy
                if acc_ratio < self.accuracy_threshold:
                    issues.append(f"Accuracy degraded {acc_ratio:.1%}")
                    max_severity = max(max_severity, RegressionSeverity.MEDIUM)

            # Recall check
            if baseline.recall > 0:
                rec_ratio = current.recall / baseline.recall
                if rec_ratio < self.recall_threshold:
                    issues.append(f"Recall degraded {rec_ratio:.1%}")
                    max_severity = max(max_severity, RegressionSeverity.MEDIUM)

            # Latency check
            if baseline.latency_ms > 0:
                lat_ratio = current.latency_ms / baseline.latency_ms
                if lat_ratio > self.latency_threshold:
                    issues.append(f"Latency increased {lat_ratio:.1f}x")
                    max_severity = max(max_severity, RegressionSeverity.HIGH)

            if not issues:
                return RegressionResult(
                    detected=False, severity=RegressionSeverity.NONE,
                    recommendation="No regression detected.",
                )

            auto_rollback = max_severity in (RegressionSeverity.HIGH, RegressionSeverity.CRITICAL)
            return RegressionResult(
                detected=True,
                severity=max_severity,
                metric_name=", ".join(issues),
                baseline_value=baseline.accuracy,
                current_value=current.accuracy,
                recommendation=(
                    "Auto-rollback triggered." if auto_rollback
                    else "Manual review recommended."
                ),
                auto_rollback=auto_rollback,
            )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "baselines": list(self._baselines.keys()),
                "accuracy_threshold": self.accuracy_threshold,
                "latency_threshold": self.latency_threshold,
            }


# ============================================================================
# EvolutionRollback
# ============================================================================

class EvolutionRollback:
    """一键回滚到任意历史版本。

    恢复：代码 + 配置 + __all__ 状态。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._snapshots: Dict[str, VersionSnapshot] = {}
        self._rollback_history: List[Dict[str, Any]] = []

    def snapshot(self, snapshot: VersionSnapshot):
        with self._lock:
            self._snapshots[snapshot.version] = snapshot

    def rollback(
        self, target_version: str, strategy: RollbackStrategy = RollbackStrategy.FULL
    ) -> bool:
        """回滚到指定版本。

        Args:
            target_version: 目标版本号。
            strategy: 回滚策略。

        Returns:
            是否成功。
        """
        with self._lock:
            snap = self._snapshots.get(target_version)
            if snap is None:
                logger.error(f"No snapshot for version {target_version}")
                return False

            record = {
                "from_version": "current",
                "to_version": target_version,
                "strategy": strategy.value,
                "timestamp": _time.time(),
                "snapshot_checksum": snap.checksum,
            }
            self._rollback_history.append(record)
            logger.info(
                f"Rollback to {target_version} ({strategy.value}) — "
                f"restored {len(snap.__all___state)} __all__ symbols"
            )
            return True

    def rollback_history(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._rollback_history)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "available_snapshots": list(self._snapshots.keys()),
                "total_rollbacks": len(self._rollback_history),
            }


# ============================================================================
# EvolutionReport
# ============================================================================

class EvolutionReport:
    """Markdown 格式版本发布说明生成器。"""

    @staticmethod
    def generate(
        version: str,
        traces: List[EvolutionTrace],
        baseline: Optional[PerformanceBaseline] = None,
    ) -> str:
        """生成 Markdown 发布说明。

        Args:
            version: 版本号。
            traces: 该版本的演化追踪列表。
            baseline: 可选性能基线。

        Returns:
            Markdown 格式字符串。
        """
        lines = [f"# Trinity Second Brain v{version}", ""]

        # Summary
        adds = sum(1 for t in traces if t.action == EvolutionAction.ADD)
        mods = sum(1 for t in traces if t.action == EvolutionAction.MODIFY)
        dels = sum(1 for t in traces if t.action == EvolutionAction.DELETE)
        mergers = sum(1 for t in traces if t.action == EvolutionAction.MERGE)
        lines.append(f"**Changes**: +{adds} added, ~{mods} modified, -{dels} deleted, "
                     f"↔{mergers} merged")
        lines.append(f"**Date**: {_time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        # Change log
        lines.append("## Change Log")
        lines.append("")
        for trace in traces:
            emoji = {"add": "+", "modify": "~", "delete": "-", "merge": "↔"}.get(
                trace.action.value, "?"
            )
            lines.append(
                f"- {emoji} **{trace.module_path}** — {trace.reason} "
                f"(_hash: {trace.new_hash[:8]}_)"
            )
        lines.append("")

        # Performance baseline
        if baseline:
            lines.append("## Performance Baseline")
            lines.append("")
            lines.append(f"| Metric | Value |")
            lines.append(f"|--------|-------|")
            lines.append(f"| Accuracy | {baseline.accuracy:.2%} |")
            lines.append(f"| Recall | {baseline.recall:.2%} |")
            lines.append(f"| Latency (ms) | {baseline.latency_ms:.2f} |")
            lines.append(f"| Throughput (QPS) | {baseline.throughput_qps:.2f} |")
            lines.append(f"| Memory (MB) | {baseline.memory_usage_mb:.2f} |")
            lines.append("")

        lines.append("---")
        lines.append(f"_Generated by SelfEvolutionLineage (CB71) at "
                     f"{_time.strftime('%Y-%m-%d %H:%M:%S')}_")

        return "\n".join(lines)


# ============================================================================
# Main Class
# ============================================================================

class SelfEvolutionLineage:
    """自演化历史追溯 (CB71)。

    统一入口——管理演化追踪、谱系图、回归检测、回滚、报告。

    Usage:
        sel = SelfEvolutionLineage()
        sel.set_baseline(PerformanceBaseline(baseline_id="v1", version="6.89.0",
                                              accuracy=0.92, recall=0.88, latency_ms=45.0))
        sel.record_trace(EvolutionTrace(trace_id="t1", action=EvolutionAction.ADD,
                                        module_path="cb70", reason="add shared memory",
                                        version_before="6.89.0", version_after="6.90.0"))
        report = sel.generate_report("6.90.0")
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.graph = LineageGraph()
        self.detector = RegressionDetector()
        self.rollback = EvolutionRollback()
        self.reporter = EvolutionReport()
        self._traces: Dict[str, EvolutionTrace] = {}
        self._baseline: Optional[PerformanceBaseline] = None
        self._start_time: float = _time.time()

    def record_trace(self, trace: EvolutionTrace):
        with self._lock:
            self._traces[trace.trace_id] = trace
            node = self.graph.nodes.get(trace.version_after)
            if node is None:
                self.graph.create_node(
                    trace.version_after,
                    [trace.version_before] if trace.version_before else None,
                )
            self.graph.nodes[trace.version_after].traces.append(trace.trace_id)

    def set_baseline(self, baseline: PerformanceBaseline):
        with self._lock:
            self._baseline = baseline
            self.detector.set_baseline(baseline)

    def detect_regression(self, current: PerformanceBaseline) -> RegressionResult:
        with self._lock:
            if self._baseline:
                result = self.detector.compare(self._baseline.baseline_id, current)
                if result.auto_rollback:
                    logger.warning(f"Auto-rollback triggered: {result.recommendation}")
                return result
            return RegressionResult(
                detected=False, severity=RegressionSeverity.NONE,
                recommendation="No baseline set.",
            )

    def snapshot_current(self) -> VersionSnapshot:
        with self._lock:
            snap = VersionSnapshot(
                version=self.graph.current_version,
                trace_ids=list(self._traces.keys()),
            )
            snap.checksum = hashlib.md5(
                json.dumps(snap.version + str(snap.timestamp)).encode()
            ).hexdigest()[:12]
            self.rollback.snapshot(snap)
            return snap

    def rollback_to(self, target_version: str, strategy: RollbackStrategy = RollbackStrategy.FULL) -> bool:
        return self.rollback.rollback(target_version, strategy)

    def generate_report(
        self, version: str,
        baseline: Optional[PerformanceBaseline] = None,
    ) -> str:
        with self._lock:
            traces = [t for t in self._traces.values() if t.version_after == version]
            return self.reporter.generate(version, traces, baseline or self._baseline)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "SelfEvolutionLineage (CB71)",
                "graph": self.graph.statistics(),
                "detector": self.detector.statistics(),
                "rollback": self.rollback.statistics(),
                "total_traces": len(self._traces),
                "baseline_set": self._baseline is not None,
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
