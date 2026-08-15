"""
# status: orphan (2026-08-15 audit, not in runtime path)
P23-8: Memory Coverage Auditor — 记忆覆盖率审计

对标方案: Memory Coverage Auditing for Agent Systems (2026)
核心发现: 周期性会话采样查询"承诺记住"的信息是否仍在上下文；
        JSON + 可视化表格报告覆盖缺失与降级路径；
        趋势追踪实现连续审计周期覆盖率的时序对比分析。
三元语: 会话周期采样 → "承诺vs实际"覆盖对比 → JSON+表格报告 → 趋势追踪

设计要点:
- SessionSampler: 周期性会话采样器 — 从生产日志进行周期抽样查询
- CoverageGap: 覆盖缺口记录 — 承诺记住但检索不到的信息条目
- AuditResult: 审计结果 — 含覆盖率、缺口列表、审计时间窗口
- CoverageReporter: 报告生成器 — JSON + Markdown可视化表格
- TrendSample: 趋势采样点 — 记录单次审计周期的覆盖率
- TrendTracker: 趋势追踪器 — 连续审计周期覆盖率对比与时序分析
- MemoryCoverageAuditor: 统一编排器 — 线程安全，支持 statistics() 运行时指标
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ============================================================================
# Enums & Constants
# ============================================================================


class CoverageStatus(Enum):
    """覆盖状态"""
    PRESENT = "present"                               # 存在且正确
    MISSING = "missing"                               # 缺失（承诺记住但不存在）
    DEGRADED = "degraded"                             # 降级（存在但内容不完整或失真）
    STALE = "stale"                                   # 陈旧（存在但时间戳过旧）


class ReportFormat(Enum):
    """报告格式"""
    JSON = "json"                                     # JSON 结构化数据
    MARKDOWN_TABLE = "markdown_table"                 # Markdown 可视化表格
    BOTH = "both"                                     # 同时生成 JSON + 表格


class TrendDirection(Enum):
    """趋势方向"""
    IMPROVING = "improving"                           # 改善中
    STABLE = "stable"                                 # 稳定
    DECLINING = "declining"                           # 恶化中
    VOLATILE = "volatile"                             # 波动大


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class CoverageGap:
    """覆盖缺口 — 承诺记住但检索不到的信息条目"""
    gap_id: str                                       # 缺口唯一ID
    expected_content: str                             # 承诺记住的内容摘要
    content_hash: str                                 # 承诺内容的哈希
    status: CoverageStatus                            # 当前覆盖状态
    last_seen_at: Optional[float] = None              # 最后一次在上下文中出现的时间
    degradation_path: List[str] = field(default_factory=list)  # 降级路径追踪
    notes: str = ""


@dataclass
class AuditResult:
    """单次审计结果"""
    audit_id: str
    window_start: float                               # 审计窗口开始时间
    window_end: float                                 # 审计窗口结束时间
    total_committed: int                              # 承诺记住的总条目数
    present_count: int                                # 存在且正确数
    missing_count: int                                # 缺失数
    degraded_count: int                               # 降级数
    stale_count: int                                  # 陈旧数
    coverage_rate: float                              # 覆盖率 (present / total)
    gaps: List[CoverageGap] = field(default_factory=list)
    session_count: int = 0
    executed_at: float = field(default_factory=time.time)

    @property
    def is_healthy(self) -> bool:
        """覆盖率是否健康 (>95%)"""
        return self.coverage_rate >= 0.95


@dataclass
class TrendSample:
    """趋势采样点"""
    sample_id: str
    audit_id: str
    coverage_rate: float
    present_count: int
    total_committed: int
    missing_count: int
    degraded_count: int
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# SessionSampler — 周期性会话采样器
# ============================================================================


class SessionSampler:
    """周期性会话采样器 — 从生产日志进行周期抽样查询

    核心功能:
    - 按时间窗口批量抽样会话
    - 提取"承诺记住"的声明列表
    - 返回会话与承诺条目的映射
    """

    def __init__(self, sample_interval_hours: float = 24.0):
        self._sample_interval = sample_interval_hours
        self._sampled_sessions: List[Dict[str, Any]] = []
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"sessions_sampled": 0, "commitments_extracted": 0}

    @property
    def sample_interval_hours(self) -> float:
        return self._sample_interval

    def sample(self, sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """对批量会话日志进行采样，提取承诺条目"""
        sampled: List[Dict[str, Any]] = []

        with self._lock:
            for session in sessions:
                commitments = session.get("commitments", [])
                sampled_session = {
                    "session_id": session.get("session_id", uuid.uuid4().hex[:12]),
                    "timestamp": session.get("timestamp", time.time()),
                    "commitments": commitments,
                    "commitment_count": len(commitments),
                }
                sampled.append(sampled_session)
                self._stats["sessions_sampled"] += 1
                self._stats["commitments_extracted"] += len(commitments)

            self._sampled_sessions.extend(sampled)

        return sampled

    def get_commitments(self, window_start: float, window_end: float) -> Dict[str, List[str]]:
        """获取时间窗口内所有承诺条目（session_id -> commitment_hash列表）"""
        result: Dict[str, List[str]] = {}

        with self._lock:
            for session in self._sampled_sessions:
                ts = session.get("timestamp", 0)
                if window_start <= ts <= window_end:
                    session_id = session.get("session_id", "")
                    commitments = session.get("commitments", [])
                    hashes = [
                        hashlib.sha256(c.encode() if isinstance(c, str) else json.dumps(c).encode()).hexdigest()
                        for c in commitments
                    ]
                    result[session_id] = hashes

        return result

    def get_stats(self) -> Dict[str, Any]:
        """获取采样器统计信息"""
        with self._lock:
            return dict(self._stats)


# ============================================================================
# CoverageReporter — 覆盖率报告生成器
# ============================================================================


class CoverageReporter:
    """覆盖率报告生成器 — JSON + Markdown可视化表格

    核心功能:
    - 从AuditResult生成结构化JSON报告
    - 生成Markdown可视化表格（含覆盖率、缺口清单、降级路径）
    - 支持导出为文件
    """

    def __init__(self):
        self._reports: List[Dict[str, Any]] = []
        self._lock = threading.RLock()

    def generate_json(self, audit: AuditResult) -> Dict[str, Any]:
        """生成JSON格式报告"""
        report = {
            "audit_id": audit.audit_id,
            "window": {
                "start": audit.window_start,
                "end": audit.window_end,
            },
            "summary": {
                "total_committed": audit.total_committed,
                "present": audit.present_count,
                "missing": audit.missing_count,
                "degraded": audit.degraded_count,
                "stale": audit.stale_count,
                "coverage_rate": round(audit.coverage_rate, 4),
                "healthy": audit.is_healthy,
            },
            "gaps": [
                {
                    "gap_id": gap.gap_id,
                    "expected_content": gap.expected_content,
                    "status": gap.status.value,
                    "last_seen_at": gap.last_seen_at,
                    "degradation_path": gap.degradation_path,
                    "notes": gap.notes,
                }
                for gap in audit.gaps
            ],
            "sessions_audited": audit.session_count,
            "executed_at": audit.executed_at,
        }

        with self._lock:
            self._reports.append(report)
        return report

    def generate_markdown_table(self, audit: AuditResult) -> str:
        """生成Markdown可视化表格报告"""
        lines: List[str] = []

        # 标题
        lines.append(f"## Memory Coverage Audit Report")
        lines.append(f"**Audit ID**: `{audit.audit_id}`")
        lines.append(f"**Window**: {time.strftime('%Y-%m-%d %H:%M', time.localtime(audit.window_start))} → {time.strftime('%Y-%m-%d %H:%M', time.localtime(audit.window_end))}")
        lines.append(f"**Executed**: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(audit.executed_at))}")
        lines.append("")

        # 汇总表
        lines.append("### Coverage Summary")
        lines.append("")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Total Committed | {audit.total_committed} |")
        lines.append(f"| Present | {audit.present_count} |")
        lines.append(f"| Missing | {audit.missing_count} |")
        lines.append(f"| Degraded | {audit.degraded_count} |")
        lines.append(f"| Stale | {audit.stale_count} |")
        lines.append(f"| **Coverage Rate** | **{audit.coverage_rate:.2%}** |")
        lines.append(f"| Health | {'Healthy' if audit.is_healthy else 'Unhealthy'} |")
        lines.append("")

        # 缺口清单
        if audit.gaps:
            lines.append("### Coverage Gaps")
            lines.append("")
            lines.append("| # | Content | Status | Degradation Path | Notes |")
            lines.append("|---|---------|--------|------------------|-------|")
            for i, gap in enumerate(audit.gaps, 1):
                path = " → ".join(gap.degradation_path) if gap.degradation_path else "N/A"
                lines.append(
                    f"| {i} | {gap.expected_content[:60]} | {gap.status.value} | {path[:60]} | {gap.notes[:30]} |"
                )
            lines.append("")
        else:
            lines.append("**No coverage gaps detected.** All committed information is present and correct.")
            lines.append("")

        return "\n".join(lines)

    def generate_both(self, audit: AuditResult) -> Dict[str, Any]:
        """同时生成 JSON 和 Markdown 报告"""
        return {
            "json": self.generate_json(audit),
            "markdown_table": self.generate_markdown_table(audit),
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取报告生成器统计信息"""
        with self._lock:
            return {"reports_generated": len(self._reports)}


# ============================================================================
# TrendTracker — 覆盖率趋势追踪器
# ============================================================================


class TrendTracker:
    """覆盖率趋势追踪器 — 连续审计周期覆盖率对比

    核心功能:
    - 记录每次审计的覆盖率快照
    - 趋势分析（改善/稳定/恶化/波动）
    - 移动平均与异常检测
    """

    def __init__(self, window_size: int = 10):
        self._samples: List[TrendSample] = []
        self._window_size = window_size
        self._lock = threading.RLock()

    def record(self, audit: AuditResult) -> TrendSample:
        """记录一次审计结果作为趋势采样点"""
        sample = TrendSample(
            sample_id=f"trend_{uuid.uuid4().hex[:8]}",
            audit_id=audit.audit_id,
            coverage_rate=audit.coverage_rate,
            present_count=audit.present_count,
            total_committed=audit.total_committed,
            missing_count=audit.missing_count,
            degraded_count=audit.degraded_count,
        )

        with self._lock:
            self._samples.append(sample)

        return sample

    def analyze_trend(self, recent_n: Optional[int] = None) -> Dict[str, Any]:
        """分析覆盖率趋势"""
        n = recent_n or self._window_size
        with self._lock:
            window = self._samples[-n:] if len(self._samples) >= n else self._samples
            if len(window) < 2:
                return {"direction": TrendDirection.STABLE.value, "samples": len(window), "reason": "Insufficient data"}

            rates = [s.coverage_rate for s in window]
            avg = sum(rates) / len(rates)
            first_half = sum(rates[:len(rates)//2]) / max(len(rates)//2, 1)
            second_half = sum(rates[len(rates)//2:]) / max(len(rates) - len(rates)//2, 1)
            delta = second_half - first_half
            variance = sum((r - avg) ** 2 for r in rates) / len(rates)

            if abs(delta) < 0.01:
                direction = TrendDirection.STABLE
            elif delta > 0.01:
                direction = TrendDirection.IMPROVING
            elif delta < -0.01:
                direction = TrendDirection.DECLINING
            else:
                direction = TrendDirection.STABLE

            if variance > 0.01:
                direction = TrendDirection.VOLATILE

            return {
                "direction": direction.value,
                "samples_analyzed": len(window),
                "current_rate": rates[-1],
                "average_rate": round(avg, 4),
                "trend_delta": round(delta, 4),
                "variance": round(variance, 6),
                "first_half_avg": round(first_half, 4),
                "second_half_avg": round(second_half, 4),
            }

    def moving_average(self, n: int = 5) -> List[float]:
        """计算覆盖率移动平均"""
        with self._lock:
            rates = [s.coverage_rate for s in self._samples]
            if len(rates) < n:
                return []
            return [sum(rates[i:i+n])/n for i in range(len(rates) - n + 1)]

    def get_samples(self, limit: int = 20) -> List[Dict[str, Any]]:
        """获取最近N个趋势采样点"""
        with self._lock:
            recent = self._samples[-limit:]
            return [
                {
                    "sample_id": s.sample_id,
                    "coverage_rate": s.coverage_rate,
                    "present_count": s.present_count,
                    "missing_count": s.missing_count,
                    "timestamp": s.timestamp,
                }
                for s in recent
            ]

    def get_stats(self) -> Dict[str, Any]:
        """获取趋势追踪器统计信息"""
        with self._lock:
            return {
                "total_samples": len(self._samples),
                "window_size": self._window_size,
                **({"latest_rate": self._samples[-1].coverage_rate} if self._samples else {}),
            }


# ============================================================================
# MemoryCoverageAuditor — 记忆力覆盖审计统一编排器
# ============================================================================


class MemoryCoverageAuditor:
    """记忆覆盖率审计引擎 — 线程安全

    功能:
    - 协调会话采样 → 覆盖对比 → 报告生成 → 趋势追踪
    - 支持周期性自动化审计
    - 运行时指标暴露 (statistics())
    """

    def __init__(self, sample_interval_hours: float = 24.0):
        self._sampler = SessionSampler(sample_interval_hours)
        self._reporter = CoverageReporter()
        self._tracker = TrendTracker()
        self._audit_history: List[AuditResult] = []
        self._lock = threading.RLock()

    @property
    def sampler(self) -> SessionSampler:
        return self._sampler

    @property
    def reporter(self) -> CoverageReporter:
        return self._reporter

    @property
    def tracker(self) -> TrendTracker:
        return self._tracker

    def run_audit(
        self,
        sessions: List[Dict[str, Any]],
        memory_lookup_fn: callable,
        window_start: Optional[float] = None,
        window_end: Optional[float] = None,
    ) -> AuditResult:
        """执行一轮完整审计

        1. 采样会话 → 2. 提取承诺 → 3. 对比实际记忆 → 4. 生成结果
        """
        with self._lock:
            now = time.time()
            if window_start is None:
                window_start = now - 86400  # 默认审计最近24h
            if window_end is None:
                window_end = now

            # 1. 会话采样
            sampled = self._sampler.sample(sessions)

            # 2. 对比"承诺记住" vs "实际存在"
            all_gaps: List[CoverageGap] = []
            total_committed = 0
            present = 0
            missing = 0
            degraded = 0
            stale = 0

            for session in sampled:
                commitments = session.get("commitments", [])
                for commitment in commitments:
                    total_committed += 1
                    content = commitment if isinstance(commitment, str) else json.dumps(commitment)
                    content_hash = hashlib.sha256(content.encode()).hexdigest()

                    try:
                        result = memory_lookup_fn(content_hash)
                    except Exception:
                        result = None

                    if result is not None:
                        if result.get("stale", False):
                            status = CoverageStatus.STALE
                            stale += 1
                        elif result.get("degraded", False):
                            status = CoverageStatus.DEGRADED
                            degraded += 1
                        else:
                            status = CoverageStatus.PRESENT
                            present += 1
                    else:
                        status = CoverageStatus.MISSING
                        missing += 1

                    if status != CoverageStatus.PRESENT:
                        gap = CoverageGap(
                            gap_id=f"gap_{uuid.uuid4().hex[:12]}",
                            expected_content=content[:200],
                            content_hash=content_hash,
                            status=status,
                            last_seen_at=result.get("last_seen") if result else None,
                            degradation_path=result.get("degradation_path", []) if result else ["not_found"],
                            notes=f"Session: {session.get('session_id', 'unknown')}",
                        )
                        all_gaps.append(gap)

            audit = AuditResult(
                audit_id=f"audit_{uuid.uuid4().hex[:12]}",
                window_start=window_start,
                window_end=window_end,
                total_committed=total_committed,
                present_count=present,
                missing_count=missing,
                degraded_count=degraded,
                stale_count=stale,
                coverage_rate=present / max(total_committed, 1),
                gaps=all_gaps,
                session_count=len(sampled),
            )

            self._audit_history.append(audit)

            # 记录趋势
            self._tracker.record(audit)

            return audit

    def generate_report(self, audit: AuditResult, fmt: ReportFormat = ReportFormat.BOTH) -> Dict[str, Any]:
        """生成审计报告"""
        with self._lock:
            if fmt == ReportFormat.JSON:
                return {"json": self._reporter.generate_json(audit)}
            elif fmt == ReportFormat.MARKDOWN_TABLE:
                return {"markdown_table": self._reporter.generate_markdown_table(audit)}
            else:
                return self._reporter.generate_both(audit)

    def get_latest_trend(self, n: int = 10) -> Dict[str, Any]:
        """获取最新趋势分析"""
        return self._tracker.analyze_trend(n)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        with self._lock:
            return {
                "sampler": self._sampler.get_stats(),
                "reporter": self._reporter.get_stats(),
                "tracker": self._tracker.get_stats(),
                "total_audits": len(self._audit_history),
                "latest_coverage": (
                    self._audit_history[-1].coverage_rate if self._audit_history else None
                ),
            }
