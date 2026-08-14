"""
P23-6: Memory Observability SLI — 记忆可观测性指标体系

对标方案: Memory Observability SLI for Agentic Systems (2026)
核心发现: 通过三大核心SLI（上下文利用率/记忆新鲜度/检索命中率）构成腐化预警雷达；
        OpenTelemetry全链路插桩覆盖记忆读取→压缩→写入生命周期；
        Baggage传播保证user_id/tenant_id/task_type自动注入子Span。
三元语: SLI指标采集 → OTel Span插桩(Gantt Trace) → Baggage传播 → Grafana兼容暴露

设计要点:
- SLIMetricType: 三大SLI指标类型枚举
- SLICollector: SLI采集器 — 计算上下文利用率(97%红线)、记忆新鲜度(压缩后降幅)、检索命中率(向量重排异常)
- SLIReading: 单次SLI采样读数, 含时间戳、值、告警标记
- OpenTelemetryInstrumentation: OTel Span插桩 — 记忆读取/压缩/写入全生命周期Gantt Trace
- SpanSnapshot: Span快照 — 含trace_id/span_id/parent_span_id/timestamps/baggage
- BaggagePropagator: Baggage传播器 — user_id/tenant_id/task_type自动注入所有子Span
- MemoryObservabilityEngine: 统一编排器 — 线程安全，支持 statistics() 运行时指标
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


class SLIMetricType(Enum):
    """SLI指标类型"""
    CONTEXT_UTILIZATION = "context_utilization"       # 上下文利用率 → 97%红线告警
    MEMORY_FRESHNESS = "memory_freshness"             # 记忆新鲜度 → 压缩后降幅检测
    RETRIEVAL_HIT_RATE = "retrieval_hit_rate"         # 检索命中率 → 向量重排异常检测


class SpanKind(Enum):
    """Span类型"""
    MEMORY_READ = "memory_read"                       # 记忆读取
    MEMORY_COMPRESS = "memory_compress"               # 记忆压缩
    MEMORY_WRITE = "memory_write"                     # 记忆写入
    RETRIEVAL_RERANK = "retrieval_rerank"             # 检索重排
    CONTEXT_MERGE = "context_merge"                   # 上下文合并


class AlertLevel(Enum):
    """告警等级"""
    OK = "ok"                                         # 正常
    WARNING = "warning"                               # 预警（接近红线）
    CRITICAL = "critical"                             # 严重（越过红线）


class BaggageKey(Enum):
    """Baggage传播键"""
    USER_ID = "user_id"
    TENANT_ID = "tenant_id"
    TASK_TYPE = "task_type"
    SESSION_ID = "session_id"
    AGENT_ID = "agent_id"


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class SLIReading:
    """单次SLI采样读数"""
    metric_type: SLIMetricType
    value: float                                      # 指标值 [0, 1] 或百分比
    threshold: float                                  # 告警阈值
    alert_level: AlertLevel                           # 告警等级
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def breached(self) -> bool:
        """是否越过阈值"""
        return self.alert_level in (AlertLevel.WARNING, AlertLevel.CRITICAL)


@dataclass
class SpanSnapshot:
    """Span快照记录"""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    span_kind: SpanKind
    start_time: float
    end_time: Optional[float] = None
    duration_ms: float = 0.0
    baggage: Dict[str, str] = field(default_factory=dict)
    attributes: Dict[str, Any] = field(default_factory=dict)
    status: str = "ok"                                # ok / error


@dataclass
class GanttTrace:
    """Gantt式全链路Trace — 记忆读取→压缩→写入完整追踪"""
    trace_id: str
    root_span_id: str
    spans: List[SpanSnapshot] = field(default_factory=list)
    total_duration_ms: float = 0.0
    created_at: float = field(default_factory=time.time)

    def add_span(self, span: SpanSnapshot):
        self.spans.append(span)

    def finalize(self):
        """计算总耗时"""
        if self.spans:
            min_start = min(s.start_time for s in self.spans)
            max_end = max((s.end_time or s.start_time) for s in self.spans)
            self.total_duration_ms = (max_end - min_start) * 1000.0


@dataclass
class FreshnessSnapshot:
    """记忆新鲜度快照"""
    before_compression: int                           # 压缩前记忆条目数
    after_compression: int                            # 压缩后记忆条目数
    freshness_drop_pct: float                         # 新鲜度降幅百分比
    compression_ratio: float                          # 压缩比
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# SLICollector — SLI采集器
# ============================================================================


class SLICollector:
    """SLI采集器 — 计算与存储三大核心SLI指标

    三大指标：
    1. 上下文利用率 (Context Utilization): 上下文窗口有效使用率, 97%红线告警
    2. 记忆新鲜度 (Memory Freshness): 压缩操作后记忆条目降幅, 异常骤降告警
    3. 检索命中率 (Retrieval Hit Rate): 向量检索+重排后的命中率, 异常低值告警
    """

    # 默认告警阈值
    DEFAULT_UTILIZATION_THRESHOLD = 0.97               # 97% 红线
    DEFAULT_FRESHNESS_DROP_MAX = 42.0                  # 新鲜度最大降幅 42%
    DEFAULT_HIT_RATE_MIN = 0.30                        # 最低命中率 0.3

    def __init__(self):
        self._readings: Dict[SLIMetricType, List[SLIReading]] = {
            SLIMetricType.CONTEXT_UTILIZATION: [],
            SLIMetricType.MEMORY_FRESHNESS: [],
            SLIMetricType.RETRIEVAL_HIT_RATE: [],
        }
        self._freshness_snapshots: List[FreshnessSnapshot] = []
        self._lock = threading.RLock()
        self._thresholds = {
            SLIMetricType.CONTEXT_UTILIZATION: self.DEFAULT_UTILIZATION_THRESHOLD,
            SLIMetricType.MEMORY_FRESHNESS: self.DEFAULT_FRESHNESS_DROP_MAX,
            SLIMetricType.RETRIEVAL_HIT_RATE: self.DEFAULT_HIT_RATE_MIN,
        }

    def set_threshold(self, metric: SLIMetricType, value: float):
        """设置指标告警阈值"""
        with self._lock:
            self._thresholds[metric] = value

    def record_context_utilization(
        self, used_tokens: int, max_tokens: int, metadata: Optional[Dict[str, Any]] = None
    ) -> SLIReading:
        """记录上下文利用率

        utilization = used_tokens / max_tokens
        告警: 超过 97% 红线时触发 WARNING / CRITICAL
        """
        ratio = used_tokens / max(max_tokens, 1)
        threshold = self._thresholds[SLIMetricType.CONTEXT_UTILIZATION]

        if ratio >= threshold:
            alert = AlertLevel.CRITICAL
        elif ratio >= threshold - 0.05:
            alert = AlertLevel.WARNING
        else:
            alert = AlertLevel.OK

        reading = SLIReading(
            metric_type=SLIMetricType.CONTEXT_UTILIZATION,
            value=ratio,
            threshold=threshold,
            alert_level=alert,
            metadata={
                "used_tokens": used_tokens,
                "max_tokens": max_tokens,
                **(metadata or {}),
            },
        )

        with self._lock:
            self._readings[SLIMetricType.CONTEXT_UTILIZATION].append(reading)

        return reading

    def record_memory_freshness(
        self, before_compression: int, after_compression: int
    ) -> SLIReading:
        """记录记忆新鲜度

        freshness_drop_pct = (before - after) / before * 100
        告警: 压缩后降幅超过 42% 时触发
        """
        if before_compression <= 0:
            drop_pct = 0.0
        else:
            drop_pct = ((before_compression - after_compression) / before_compression) * 100.0

        threshold = self._thresholds[SLIMetricType.MEMORY_FRESHNESS]

        if drop_pct >= threshold:
            alert = AlertLevel.CRITICAL
        elif drop_pct >= threshold * 0.8:
            alert = AlertLevel.WARNING
        else:
            alert = AlertLevel.OK

        reading = SLIReading(
            metric_type=SLIMetricType.MEMORY_FRESHNESS,
            value=drop_pct,
            threshold=threshold,
            alert_level=alert,
            metadata={
                "before_compression": before_compression,
                "after_compression": after_compression,
                "compression_ratio": after_compression / max(before_compression, 1),
            },
        )

        snapshot = FreshnessSnapshot(
            before_compression=before_compression,
            after_compression=after_compression,
            freshness_drop_pct=drop_pct,
            compression_ratio=after_compression / max(before_compression, 1),
        )

        with self._lock:
            self._readings[SLIMetricType.MEMORY_FRESHNESS].append(reading)
            self._freshness_snapshots.append(snapshot)

        return reading

    def record_retrieval_hit_rate(
        self, queries_total: int, hits: int, metadata: Optional[Dict[str, Any]] = None
    ) -> SLIReading:
        """记录检索命中率

        hit_rate = hits / queries_total
        告警: 命中率低于 0.3 时触发异常（向量重排异常检测）
        """
        hit_rate = hits / max(queries_total, 1)
        threshold = self._thresholds[SLIMetricType.RETRIEVAL_HIT_RATE]

        if hit_rate <= threshold:
            alert = AlertLevel.CRITICAL
        elif hit_rate <= threshold * 1.5:
            alert = AlertLevel.WARNING
        else:
            alert = AlertLevel.OK

        reading = SLIReading(
            metric_type=SLIMetricType.RETRIEVAL_HIT_RATE,
            value=hit_rate,
            threshold=threshold,
            alert_level=alert,
            metadata={
                "queries_total": queries_total,
                "hits": hits,
                **(metadata or {}),
            },
        )

        with self._lock:
            self._readings[SLIMetricType.RETRIEVAL_HIT_RATE].append(reading)

        return reading

    def get_latest_reading(self, metric: SLIMetricType) -> Optional[SLIReading]:
        """获取最新SLI读数"""
        with self._lock:
            readings = self._readings.get(metric, [])
            return readings[-1] if readings else None

    def get_alert_count(self, level: Optional[AlertLevel] = None) -> int:
        """获取告警计数"""
        with self._lock:
            count = 0
            for readings in self._readings.values():
                for r in readings:
                    if level is None or r.alert_level == level:
                        count += 1
            return count

    def get_stats(self) -> Dict[str, Any]:
        """获取SLI采集统计"""
        with self._lock:
            return {
                "total_readings": sum(len(v) for v in self._readings.values()),
                "by_metric": {k.value: len(v) for k, v in self._readings.items()},
                "critical_alerts": self.get_alert_count(AlertLevel.CRITICAL),
                "warning_alerts": self.get_alert_count(AlertLevel.WARNING),
                "freshness_snapshots": len(self._freshness_snapshots),
                "thresholds": {k.value: v for k, v in self._thresholds.items()},
            }


# ============================================================================
# OpenTelemetryInstrumentation — OTel Span 插桩
# ============================================================================


class OpenTelemetryInstrumentation:
    """OpenTelemetry Span 插桩器

    覆盖记忆读取→压缩→写入全生命周期，生成 Gantt 式 Trace 图。
    支持 Baggage 传播，user_id/tenant_id/task_type 自动注入子 Span。
    """

    def __init__(self, service_name: str = "trinity-memory"):
        self._service_name = service_name
        self._traces: Dict[str, GanttTrace] = {}
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"traces_created": 0, "spans_recorded": 0}

    def start_trace(self, baggage: Optional[Dict[str, str]] = None) -> GanttTrace:
        """创建新的全链路 Trace"""
        trace_id = uuid.uuid4().hex[:32]
        root_span_id = uuid.uuid4().hex[:16]

        trace = GanttTrace(trace_id=trace_id, root_span_id=root_span_id)
        trace.baggage = baggage or {}

        with self._lock:
            self._traces[trace_id] = trace
            self._stats["traces_created"] += 1

        return trace

    def start_span(
        self,
        trace: GanttTrace,
        kind: SpanKind,
        parent_span_id: Optional[str] = None,
        attributes: Optional[Dict[str, Any]] = None,
        baggage: Optional[Dict[str, str]] = None,
    ) -> SpanSnapshot:
        """在已有 Trace 中启动新 Span"""
        span_id = uuid.uuid4().hex[:16]
        span = SpanSnapshot(
            trace_id=trace.trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            span_kind=kind,
            start_time=time.time(),
            baggage=baggage or {},
            attributes=attributes or {},
        )

        trace.add_span(span)

        with self._lock:
            self._stats["spans_recorded"] += 1

        return span

    def end_span(self, span: SpanSnapshot, status: str = "ok"):
        """结束 Span，记录耗时"""
        span.end_time = time.time()
        span.duration_ms = (span.end_time - span.start_time) * 1000.0
        span.status = status

    def end_trace(self, trace: GanttTrace) -> GanttTrace:
        """结束 Trace 并计算全链路总耗时"""
        trace.finalize()
        return trace

    def get_trace(self, trace_id: str) -> Optional[GanttTrace]:
        """获取指定 Trace"""
        with self._lock:
            return self._traces.get(trace_id)

    def to_grafana_payload(self, trace: GanttTrace) -> Dict[str, Any]:
        """导出为 Grafana Tempo 兼容格式"""
        span_list = []
        for span in trace.spans:
            span_list.append({
                "traceId": span.trace_id,
                "spanId": span.span_id,
                "parentSpanId": span.parent_span_id or "",
                "operationName": span.span_kind.value,
                "startTimeUnixNano": str(int(span.start_time * 1e9)),
                "durationMs": span.duration_ms,
                "attributes": {**span.attributes, **span.baggage},
                "status": span.status,
            })

        return {
            "resourceSpans": [{
                "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": self._service_name}}]},
                "scopeSpans": [{"spans": span_list}],
            }],
        }

    def get_stats(self) -> Dict[str, Any]:
        """获取插桩统计信息"""
        with self._lock:
            return dict(self._stats)


# ============================================================================
# BaggagePropagator — Baggage 传播器
# ============================================================================


class BaggagePropagator:
    """Baggage传播器 — user_id/tenant_id/task_type 自动注入子Span

    遵循 W3C Baggage 规范（RFC 0022），将上下文属性自动注入所有下游Span。
    """

    def __init__(self):
        self._default_baggage: Dict[str, str] = {}
        self._lock = threading.RLock()

    def set_default_baggage(self, key: BaggageKey, value: str):
        """设置默认Baggage键值"""
        with self._lock:
            self._default_baggage[key.value] = value

    def set_defaults(self, user_id: str = "", tenant_id: str = "", task_type: str = ""):
        """批量设置核心Baggage"""
        with self._lock:
            if user_id:
                self._default_baggage[BaggageKey.USER_ID.value] = user_id
            if tenant_id:
                self._default_baggage[BaggageKey.TENANT_ID.value] = tenant_id
            if task_type:
                self._default_baggage[BaggageKey.TASK_TYPE.value] = task_type

    def propagate(self, parent_baggage: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """传播Baggage — 合并默认值 + 父Span上下文"""
        with self._lock:
            merged = dict(self._default_baggage)
            if parent_baggage:
                merged.update(parent_baggage)
            # 自动注入 session_id
            if BaggageKey.SESSION_ID.value not in merged:
                merged[BaggageKey.SESSION_ID.value] = uuid.uuid4().hex[:12]
            return merged

    def inject_into_span(self, span: SpanSnapshot, extra: Optional[Dict[str, str]] = None):
        """将Baggage注入指定Span"""
        baggage = self.propagate(span.baggage if span.baggage else None)
        if extra:
            baggage.update(extra)
        span.baggage = baggage

    def extract_from_span(self, span: SpanSnapshot, key: BaggageKey) -> Optional[str]:
        """从Span中提取指定Baggage值"""
        return span.baggage.get(key.value)

    def get_defaults(self) -> Dict[str, str]:
        """获取当前默认Baggage"""
        with self._lock:
            return dict(self._default_baggage)


# ============================================================================
# MemoryObservabilityEngine — 记忆可观测性统一编排器
# ============================================================================


class MemoryObservabilityEngine:
    """记忆可观测性引擎 — 线程安全

    功能:
    - 协调 SLI 采集、OTel 插桩、Baggage 传播
    - 暴露 Grafana 兼容指标
    - 运行时指标暴露 (statistics())
    """

    def __init__(self, service_name: str = "trinity-memory"):
        self._sli_collector = SLICollector()
        self._otel = OpenTelemetryInstrumentation(service_name)
        self._baggage = BaggagePropagator()
        self._lock = threading.RLock()

    @property
    def sli_collector(self) -> SLICollector:
        return self._sli_collector

    @property
    def instrumentation(self) -> OpenTelemetryInstrumentation:
        return self._otel

    @property
    def baggage(self) -> BaggagePropagator:
        return self._baggage

    def instrument_memory_operation(
        self,
        operation_type: SpanKind,
        attributes: Optional[Dict[str, Any]] = None,
        parent_span: Optional[SpanSnapshot] = None,
    ) -> SpanSnapshot:
        """为记忆操作插桩

        创建新Trace（若无父Span）并在其上启动Span，自动传播Baggage。
        """
        with self._lock:
            if parent_span is not None:
                trace = self._otel.get_trace(parent_span.trace_id)
                if trace is None:
                    trace = self._otel.start_trace()
                span = self._otel.start_span(
                    trace, operation_type,
                    parent_span_id=parent_span.span_id,
                    attributes=attributes,
                )
            else:
                trace = self._otel.start_trace()
                span = self._otel.start_span(
                    trace, operation_type,
                    parent_span_id=trace.root_span_id,
                    attributes=attributes,
                )

            # 自动注入 Baggage
            self._baggage.inject_into_span(span)
            return span

    def export_grafana_dashboard(self, trace_id: str) -> Optional[Dict[str, Any]]:
        """导出指定Trace为Grafana兼容仪表盘数据"""
        trace = self._otel.get_trace(trace_id)
        if trace is None:
            return None
        return self._otel.to_grafana_payload(trace)

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        with self._lock:
            return {
                "sli": self._sli_collector.get_stats(),
                "otel": self._otel.get_stats(),
                "baggage_defaults": self._baggage.get_defaults(),
            }
