"""
# status: orphan (2026-08-15 audit, not in runtime path)
P13-4: Carbon-Aware Memory Scheduling
======================================

对标 CEDAR (GreenSys 2026) 延迟/成本/碳三目标联合优化。

核心系统：
  - CarbonIntensityTracker:     追踪实时/预测电网碳强度数据
  - MultiObjectiveScheduler:    三目标联合优化器——尾延迟 SLO / 云成本 / 边际碳排放
  - LowCarbonWindowDetector:    检测低碳窗口期（夜间/可再生能源高峰），调度批量重操作
  - OperationCarbonAuditor:     审计每个记忆操作（蒸馏/重组/压缩/索引）的碳足迹
  - CostLatencyCarbonProfile:   为每种操作维护成本/延迟/碳的三维剖面

接口兼容：
  - memory_observability.py MetricsCollector: 集成遥测出口
  - episodic_rl.py: 记忆操作类型对齐
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class CarbonZone(Enum):
    """碳强度区域。"""
    VERY_LOW = "very_low"       # < 50 gCO2/kWh
    LOW = "low"                 # 50-150 gCO2/kWh
    MODERATE = "moderate"       # 150-350 gCO2/kWh
    HIGH = "high"               # 350-600 gCO2/kWh
    VERY_HIGH = "very_high"     # > 600 gCO2/kWh


class MemoryOperationType(Enum):
    """记忆操作类型——与 episodic_rl.py 对齐。"""
    DISTILLATION = "distillation"         # 蒸馏
    REORGANIZATION = "reorganization"     # 重组
    COMPRESSION = "compression"           # 压缩
    INDEXING = "indexing"                 # 索引构建
    CONSOLIDATION = "consolidation"       # 睡眠巩固
    CLEANUP = "cleanup"                   # 清理/墓碑压缩
    RETRIEVAL = "retrieval"               # 批量检索
    BENCHMARK = "benchmark"               # 基准测试


class SchedulingPolicy(Enum):
    """调度策略。"""
    CARBON_FIRST = "carbon_first"             # 碳优先（尽可能低碳执行）
    LATENCY_FIRST = "latency_first"           # 延迟优先（SLO 保证）
    COST_FIRST = "cost_first"                 # 成本优先
    WEIGHTED_BALANCE = "weighted_balance"     # 加权平衡（三目标）
    ADAPTIVE = "adaptive"                     # 自适应（根据实时条件切换）


class WindowQuality(Enum):
    """低碳窗口质量。"""
    EXCELLENT = "excellent"   # 碳强度极低且持续时间长
    GOOD = "good"             # 碳强度低
    ACCEPTABLE = "acceptable" # 可接受
    POOR = "poor"             # 差，不建议调度重操作
    UNKNOWN = "unknown"       # 无法确定


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class CarbonIntensitySample:
    """碳强度采样点。"""
    timestamp: float
    intensity_gco2_per_kwh: float     # 单位：gCO2/kWh
    zone: CarbonZone = CarbonZone.MODERATE
    source: str = "estimated"          # grid_data / estimated / cached
    confidence: float = 0.8
    region: str = ""


@dataclass
class OperationProfile:
    """单个操作的成本/延迟/碳三维剖面。"""
    operation_type: MemoryOperationType
    avg_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0        # 尾延迟
    cost_per_op_usd: float = 0.0       # 每次操作云成本（USD）
    carbon_per_op_gco2: float = 0.0    # 每次操作碳排放（gCO2）
    energy_per_op_kwh: float = 0.0     # 每次操作能耗（kWh）
    sample_count: int = 0
    last_updated: float = field(default_factory=time.time)


@dataclass
class SchedulingDecision:
    """调度决策。"""
    decision_id: str
    operation_type: MemoryOperationType
    policy: SchedulingPolicy
    scheduled_time: float              # 预定执行时间
    can_defer: bool                    # 是否可延迟
    carbon_zone: CarbonZone
    estimated_carbon_saved_gco2: float = 0.0
    estimated_cost_usd: float = 0.0
    meets_slo: bool = True
    reason: str = ""


@dataclass
class CarbonAuditRecord:
    """碳足迹审计记录。"""
    audit_id: str
    operation_type: MemoryOperationType
    carbon_emitted_gco2: float
    energy_consumed_kwh: float
    intensity_at_time: float           # 执行时的电网碳强度
    cost_usd: float = 0.0
    duration_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SchedulerStats:
    """调度器总体统计。"""
    total_decisions: int = 0
    total_carbon_saved_gco2: float = 0.0
    total_cost_saved_usd: float = 0.0
    slo_violations: int = 0
    deferred_operations: int = 0
    operations_by_zone: Dict[str, int] = field(default_factory=dict)


# ============================================================================
# CarbonIntensityTracker
# ============================================================================

class CarbonIntensityTracker:
    """追踪实时/预测电网碳强度数据。

    数据来源：
      - 电网实时数据（模拟 Electricity Maps / WattTime API）
      - 历史统计预测
      - 区域默认值（中国电网平均值 ~550 gCO2/kWh）
    """

    # 默认区域碳强度（gCO2/kWh）——基于公开数据
    DEFAULT_REGION_INTENSITY = {
        "default": 550.0,
        "cn-north": 620.0,
        "cn-south": 480.0,
        "cn-east": 510.0,
        "cn-west": 350.0,   # 西部可再生能源丰富
    }

    def __init__(
        self,
        region: str = "default",
        history_window_hours: float = 168.0,  # 7 天历史
        forecast_window_hours: float = 24.0,   # 24 小时预测
    ):
        self.region = region
        self.history_window_hours = history_window_hours
        self.forecast_window_hours = forecast_window_hours

        self._lock = threading.RLock()
        self._samples: deque = deque(maxlen=10000)
        self._forecast: List[CarbonIntensitySample] = []
        self._current_intensity: float = self.DEFAULT_REGION_INTENSITY.get(region, 550.0)
        self._last_sample_at: float = 0.0

    def get_current_intensity(self) -> float:
        """获取当前碳强度估计。"""
        with self._lock:
            # 结合最近采样和区域默认值
            if self._samples and time.time() - self._last_sample_at < 3600:
                recent = list(self._samples)[-10:]
                return float(np.mean([s.intensity_gco2_per_kwh for s in recent]))
            return self._current_intensity

    def get_current_zone(self) -> CarbonZone:
        """获取当前碳强度区域。"""
        intensity = self.get_current_intensity()
        return self._intensity_to_zone(intensity)

    def _intensity_to_zone(self, intensity: float) -> CarbonZone:
        """将碳强度值映射到区域。"""
        if intensity < 50:
            return CarbonZone.VERY_LOW
        if intensity < 150:
            return CarbonZone.LOW
        if intensity < 350:
            return CarbonZone.MODERATE
        if intensity < 600:
            return CarbonZone.HIGH
        return CarbonZone.VERY_HIGH

    def record_sample(
        self,
        intensity_gco2_per_kwh: float,
        source: str = "estimated",
        confidence: float = 0.8,
    ) -> None:
        """记录一次碳强度采样。"""
        with self._lock:
            sample = CarbonIntensitySample(
                timestamp=time.time(),
                intensity_gco2_per_kwh=intensity_gco2_per_kwh,
                zone=self._intensity_to_zone(intensity_gco2_per_kwh),
                source=source,
                confidence=confidence,
                region=self.region,
            )
            self._samples.append(sample)
            self._current_intensity = intensity_gco2_per_kwh
            self._last_sample_at = time.time()

    def generate_forecast(self) -> List[CarbonIntensitySample]:
        """生成未来 carbon 强度预测（基于历史模式模拟）。"""
        with self._lock:
            now = time.time()
            recent = list(self._samples)[-48:]  # 近期 48 个采样点

            if not recent:
                # 无历史数据时使用默认昼夜模式
                forecast = []
                for h in range(int(self.forecast_window_hours)):
                    hour_of_day = (time.localtime(now + h * 3600).tm_hour)
                    # 夜间 (0-6) 碳强度较低，白天较高
                    base = self._current_intensity
                    factor = 0.7 if 0 <= hour_of_day < 6 else 1.0
                    forecast.append(CarbonIntensitySample(
                        timestamp=now + h * 3600,
                        intensity_gco2_per_kwh=base * factor,
                        zone=self._intensity_to_zone(base * factor),
                        source="forecast",
                        confidence=0.6,
                        region=self.region,
                    ))
                self._forecast = forecast
                return forecast

            # 基于历史模式：计算每小时平均
            mean_intensity = np.mean([s.intensity_gco2_per_kwh for s in recent])
            std_intensity = np.std([s.intensity_gco2_per_kwh for s in recent])

            forecast = []
            for h in range(int(self.forecast_window_hours)):
                hour_of_day = (time.localtime(now + h * 3600).tm_hour)
                nocturnal_factor = 0.6 + 0.4 * math.sin(math.pi * (hour_of_day - 6) / 12) if 0 <= hour_of_day < 6 else 1.0
                predicted = mean_intensity * nocturnal_factor + np.random.normal(0, std_intensity * 0.1)
                predicted = max(0.0, predicted)
                forecast.append(CarbonIntensitySample(
                    timestamp=now + h * 3600,
                    intensity_gco2_per_kwh=predicted,
                    zone=self._intensity_to_zone(predicted),
                    source="forecast",
                    confidence=0.7,
                    region=self.region,
                ))
            self._forecast = forecast
            return forecast

    def get_historical_stats(self, window_hours: float = 24.0) -> Dict[str, Any]:
        """获取历史统计。"""
        with self._lock:
            now = time.time()
            recent = [s for s in self._samples if now - s.timestamp < window_hours * 3600]
            if not recent:
                return {"samples": 0, "mean": self._current_intensity, "min": self._current_intensity, "max": self._current_intensity}
            intensities = [s.intensity_gco2_per_kwh for s in recent]
            return {
                "samples": len(recent),
                "mean": float(np.mean(intensities)),
                "min": float(np.min(intensities)),
                "max": float(np.max(intensities)),
                "std": float(np.std(intensities)),
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "region": self.region,
                "current_intensity": self._current_intensity,
                "current_zone": self.get_current_zone().value,
                "samples_recorded": len(self._samples),
                "forecast_points": len(self._forecast),
                "history": self.get_historical_stats(),
            }


# ============================================================================
# CostLatencyCarbonProfile
# ============================================================================

class CostLatencyCarbonProfile:
    """为每种记忆操作维护成本/延迟/碳的三维剖面。

    维护每种 MemoryOperationType 的运行时统计，
    支持动态更新和加权查询。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._profiles: Dict[MemoryOperationType, OperationProfile] = {
            op: OperationProfile(operation_type=op) for op in MemoryOperationType
        }

    def record_operation(
        self,
        op_type: MemoryOperationType,
        latency_ms: float,
        cost_usd: float = 0.0,
        carbon_gco2: float = 0.0,
        energy_kwh: float = 0.0,
    ) -> None:
        """记录一次操作的指标，更新剖面。"""
        with self._lock:
            profile = self._profiles[op_type]
            n = profile.sample_count
            # 指数移动平均
            alpha = 0.1 if n > 0 else 1.0
            profile.avg_latency_ms = alpha * latency_ms + (1 - alpha) * profile.avg_latency_ms
            profile.p99_latency_ms = max(profile.p99_latency_ms, latency_ms)
            profile.cost_per_op_usd = alpha * cost_usd + (1 - alpha) * profile.cost_per_op_usd
            profile.carbon_per_op_gco2 = alpha * carbon_gco2 + (1 - alpha) * profile.carbon_per_op_gco2
            profile.energy_per_op_kwh = alpha * energy_kwh + (1 - alpha) * profile.energy_per_op_kwh
            profile.sample_count += 1
            profile.last_updated = time.time()

    def get_profile(self, op_type: MemoryOperationType) -> OperationProfile:
        """获取操作类型的三维剖面。"""
        with self._lock:
            return self._profiles[op_type]

    def estimate_operation_cost(
        self,
        op_type: MemoryOperationType,
        batch_size: int = 1,
        current_carbon_intensity: float = 500.0,
    ) -> Dict[str, float]:
        """估算批量操作的成本/延迟/碳。"""
        profile = self.get_profile(op_type)
        return {
            "estimated_latency_ms": profile.avg_latency_ms * batch_size,
            "p99_latency_ms": profile.p99_latency_ms * batch_size,
            "estimated_cost_usd": profile.cost_per_op_usd * batch_size,
            "estimated_carbon_gco2": profile.carbon_per_op_gco2 * batch_size * (current_carbon_intensity / 500.0),
        }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            result = {}
            for op, profile in self._profiles.items():
                if profile.sample_count > 0:
                    result[op.value] = {
                        "avg_latency_ms": round(profile.avg_latency_ms, 2),
                        "p99_latency_ms": round(profile.p99_latency_ms, 2),
                        "cost_per_op_usd": round(profile.cost_per_op_usd, 6),
                        "carbon_per_op_gco2": round(profile.carbon_per_op_gco2, 4),
                        "samples": profile.sample_count,
                    }
            return result


# ============================================================================
# LowCarbonWindowDetector
# ============================================================================

class LowCarbonWindowDetector:
    """检测低碳窗口期（夜间/可再生能源高峰），调度批量重操作。

    使用 CarbonIntensityTracker 的预测数据，识别最优执行窗口：
      - 夜间低峰（0:00-6:00）：通常碳强度最低
      - 可再生能源高峰（随地区而异）
      - 连续低碳时段：持续时间够长才适合批量重操作
    """

    def __init__(
        self,
        tracker: CarbonIntensityTracker,
        min_window_duration_hours: float = 2.0,
        max_carbon_threshold: float = 200.0,
    ):
        self.tracker = tracker
        self.min_window_duration_hours = min_window_duration_hours
        self.max_carbon_threshold = max_carbon_threshold

        self._lock = threading.RLock()
        self._detected_windows: List[Dict[str, Any]] = []

    def detect_windows(self) -> List[Dict[str, Any]]:
        """检测未来的低碳窗口期。"""
        with self._lock:
            forecast = self.tracker._forecast
            if not forecast:
                forecast = self.tracker.generate_forecast()

            windows = []
            current_start = None
            current_samples = []

            for sample in forecast:
                if sample.intensity_gco2_per_kwh <= self.max_carbon_threshold:
                    if current_start is None:
                        current_start = sample.timestamp
                    current_samples.append(sample)
                else:
                    if current_start is not None and current_samples:
                        duration_hours = (sample.timestamp - current_start) / 3600.0
                        if duration_hours >= self.min_window_duration_hours:
                            windows.append(self._make_window(current_start, sample.timestamp, current_samples))
                        current_start = None
                        current_samples = []

            # 处理末尾窗口
            if current_start is not None and current_samples:
                end_time = forecast[-1].timestamp + 3600
                duration_hours = (end_time - current_start) / 3600.0
                if duration_hours >= self.min_window_duration_hours:
                    windows.append(self._make_window(current_start, end_time, current_samples))

            self._detected_windows = windows
            return windows

    def _make_window(
        self, start: float, end: float, samples: List[CarbonIntensitySample]
    ) -> Dict[str, Any]:
        """构建窗口描述。"""
        intensities = [s.intensity_gco2_per_kwh for s in samples]
        avg_intensity = float(np.mean(intensities)) if intensities else 0.0

        if avg_intensity < 50:
            quality = WindowQuality.EXCELLENT
        elif avg_intensity < 150:
            quality = WindowQuality.GOOD
        elif avg_intensity < 250:
            quality = WindowQuality.ACCEPTABLE
        else:
            quality = WindowQuality.POOR

        return {
            "start": start,
            "end": end,
            "duration_hours": (end - start) / 3600.0,
            "avg_intensity": avg_intensity,
            "min_intensity": float(np.min(intensities)),
            "quality": quality.value,
            "suitable_for": self._get_suitable_operations(quality),
        }

    def _get_suitable_operations(self, quality: WindowQuality) -> List[str]:
        """根据窗口质量推荐适合的操作类型。"""
        if quality == WindowQuality.EXCELLENT:
            return [op.value for op in MemoryOperationType]
        elif quality == WindowQuality.GOOD:
            return [
                MemoryOperationType.DISTILLATION.value,
                MemoryOperationType.CONSOLIDATION.value,
                MemoryOperationType.COMPRESSION.value,
                MemoryOperationType.INDEXING.value,
            ]
        elif quality == WindowQuality.ACCEPTABLE:
            return [
                MemoryOperationType.CLEANUP.value,
                MemoryOperationType.REORGANIZATION.value,
            ]
        return []

    def get_next_window(self) -> Optional[Dict[str, Any]]:
        """获取最近的低碳窗口。"""
        windows = self.detect_windows()
        now = time.time()
        future = [w for w in windows if w["start"] > now]
        if future:
            return min(future, key=lambda w: w["start"])
        return None

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "detected_windows": len(self._detected_windows),
                "min_window_duration_h": self.min_window_duration_hours,
                "max_carbon_threshold": self.max_carbon_threshold,
                "next_window": self.get_next_window(),
            }


# ============================================================================
# MultiObjectiveScheduler
# ============================================================================

class MultiObjectiveScheduler:
    """三目标联合优化器——尾延迟 SLO / 云成本 / 边际碳排放。

    使用加权评分法结合帕累托前沿，为每个记忆操作计算最优
    执行时间和执行策略。

    目标函数：
      Score = w_latency * f_latency(SLO) + w_cost * f_cost(budget) + w_carbon * f_carbon(intensity)
    """

    def __init__(
        self,
        tracker: CarbonIntensityTracker,
        profile: CostLatencyCarbonProfile,
        latency_slo_ms: float = 30000.0,  # 尾延迟 SLO（30s）
        daily_cost_budget_usd: float = 1.0,
        carbon_budget_kg_per_day: float = 0.5,  # 日碳预算 0.5 kg
        policy: SchedulingPolicy = SchedulingPolicy.WEIGHTED_BALANCE,
        weights: Optional[Dict[str, float]] = None,
    ):
        self.tracker = tracker
        self.profile = profile
        self.latency_slo_ms = latency_slo_ms
        self.daily_cost_budget_usd = daily_cost_budget_usd
        self.carbon_budget_kg_per_day = carbon_budget_kg_per_day
        self.policy = policy
        self.weights = weights or {"latency": 0.4, "cost": 0.3, "carbon": 0.3}

        self._lock = threading.RLock()
        self._decisions: List[SchedulingDecision] = []
        self._stats = SchedulerStats()

    def schedule(
        self,
        op_type: MemoryOperationType,
        batch_size: int = 1,
        force_immediate: bool = False,
    ) -> SchedulingDecision:
        """为记忆操作生成调度决策。"""
        with self._lock:
            current_zone = self.tracker.get_current_zone()
            current_intensity = self.tracker.get_current_intensity()
            op_est = self.profile.estimate_operation_cost(op_type, batch_size, current_intensity)

            decision_id = f"sched_{uuid.uuid4().hex[:12]}"
            decision = SchedulingDecision(
                decision_id=decision_id,
                operation_type=op_type,
                policy=self.policy,
                scheduled_time=time.time(),
                can_defer=False,
                carbon_zone=current_zone,
                estimated_cost_usd=op_est["estimated_cost_usd"],
                meets_slo=op_est["p99_latency_ms"] <= self.latency_slo_ms,
            )

            if force_immediate:
                decision.reason = "force_immediate"
                self._decisions.append(decision)
                self._stats.total_decisions += 1
                return decision

            # 计算三目标得分
            scores = self._compute_scores(op_type, op_est, current_intensity)

            if self.policy == SchedulingPolicy.CARBON_FIRST:
                decision = self._carbon_first_schedule(decision, scores, current_intensity)
            elif self.policy == SchedulingPolicy.LATENCY_FIRST:
                decision = self._latency_first_schedule(decision, scores, op_est)
            elif self.policy == SchedulingPolicy.COST_FIRST:
                decision = self._cost_first_schedule(decision, scores, op_est)
            elif self.policy == SchedulingPolicy.WEIGHTED_BALANCE:
                decision = self._weighted_balance_schedule(decision, scores, op_est, current_intensity)
            else:
                decision = self._adaptive_schedule(decision, scores, op_est, current_intensity)

            self._decisions.append(decision)
            self._stats.total_decisions += 1
            if not decision.meets_slo:
                self._stats.slo_violations += 1
            if decision.can_defer:
                self._stats.deferred_operations += 1

            zone_key = decision.carbon_zone.value
            self._stats.operations_by_zone[zone_key] = self._stats.operations_by_zone.get(zone_key, 0) + 1

            return decision

    def _compute_scores(
        self,
        op_type: MemoryOperationType,
        op_est: Dict[str, float],
        current_intensity: float,
    ) -> Dict[str, float]:
        """计算三目标归一化得分（0-1，越高越好）。"""
        # 延迟得分：SLO 以内线性得分，超过 SLO 指数衰减
        latency_ratio = op_est["p99_latency_ms"] / max(self.latency_slo_ms, 1.0)
        latency_score = max(0.0, 1.0 - latency_ratio)

        # 成本得分：日预算以内得分高
        cost_ratio = op_est["estimated_cost_usd"] / max(self.daily_cost_budget_usd, 1e-6)
        cost_score = max(0.0, 1.0 - cost_ratio * 10)  # 单次不超过日预算 10%

        # 碳得分：碳强度越低得分越高
        carbon_threshold = (self.carbon_budget_kg_per_day * 1000.0) / 24.0  # 每小时预算
        carbon_score = max(0.0, 1.0 - current_intensity / 800.0)

        return {
            "latency": latency_score,
            "cost": cost_score,
            "carbon": carbon_score,
        }

    def _carbon_first_schedule(
        self, decision: SchedulingDecision, scores: Dict[str, float], intensity: float
    ) -> SchedulingDecision:
        zone = self.tracker.get_current_zone()
        if zone in (CarbonZone.HIGH, CarbonZone.VERY_HIGH):
            decision.can_defer = True
            decision.scheduled_time = time.time() + 3600  # 延迟 1 小时
            decision.carbon_zone = zone
            decision.estimated_carbon_saved_gco2 = self.profile.get_profile(
                decision.operation_type
            ).carbon_per_op_gco2
            decision.reason = f"carbon_first: deferring due to {zone.value} intensity ({intensity:.0f} gCO2/kWh)"
        else:
            decision.reason = f"carbon_first: executing at {zone.value} intensity ({intensity:.0f} gCO2/kWh)"
        return decision

    def _latency_first_schedule(
        self, decision: SchedulingDecision, scores: Dict[str, float], op_est: Dict[str, float]
    ) -> SchedulingDecision:
        if scores["latency"] < 0.5:
            decision.meets_slo = False
            decision.reason = f"latency_first: SLO violation risk (p99={op_est['p99_latency_ms']:.0f}ms vs {self.latency_slo_ms}ms)"
        else:
            decision.reason = "latency_first: within SLO"
        return decision

    def _cost_first_schedule(
        self, decision: SchedulingDecision, scores: Dict[str, float], op_est: Dict[str, float]
    ) -> SchedulingDecision:
        if scores["cost"] < 0.3:
            decision.can_defer = True
            decision.reason = f"cost_first: deferring due to cost {op_est['estimated_cost_usd']:.6f} USD"
        else:
            decision.reason = f"cost_first: cost acceptable {op_est['estimated_cost_usd']:.6f} USD"
        return decision

    def _weighted_balance_schedule(
        self,
        decision: SchedulingDecision,
        scores: Dict[str, float],
        op_est: Dict[str, float],
        intensity: float,
    ) -> SchedulingDecision:
        w = self.weights
        total = w["latency"] * scores["latency"] + w["cost"] * scores["cost"] + w["carbon"] * scores["carbon"]

        if total < 0.4:
            decision.can_defer = True
            decision.estimated_carbon_saved_gco2 = self.profile.get_profile(
                decision.operation_type
            ).carbon_per_op_gco2
            decision.reason = f"weighted_balance: score={total:.2f} < 0.4, deferring"
        else:
            decision.reason = f"weighted_balance: score={total:.2f} >= 0.4, executing"

        self._stats.total_carbon_saved_gco2 += decision.estimated_carbon_saved_gco2
        return decision

    def _adaptive_schedule(
        self,
        decision: SchedulingDecision,
        scores: Dict[str, float],
        op_est: Dict[str, float],
        intensity: float,
    ) -> SchedulingDecision:
        # 自适应：根据当前条件动态选择策略
        if scores["latency"] < 0.3:
            return self._latency_first_schedule(decision, scores, op_est)
        if intensity > 500:
            return self._carbon_first_schedule(decision, scores, intensity)
        return self._weighted_balance_schedule(decision, scores, op_est, intensity)

    def set_weights(self, latency: float, cost: float, carbon: float) -> None:
        """动态调整三目标权重。"""
        total = latency + cost + carbon
        with self._lock:
            self.weights = {
                "latency": latency / total,
                "cost": cost / total,
                "carbon": carbon / total,
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "policy": self.policy.value,
                "weights": self.weights,
                "latency_slo_ms": self.latency_slo_ms,
                "daily_cost_budget_usd": self.daily_cost_budget_usd,
                "carbon_budget_kg_per_day": self.carbon_budget_kg_per_day,
                **dataclasses.asdict(self._stats),
            }


# ============================================================================
# OperationCarbonAuditor
# ============================================================================

class OperationCarbonAuditor:
    """审计每个记忆操作的碳足迹。

    记录每次操作的碳排、能耗、成本等详细信息，
    提供聚合统计和趋势分析，数据可导出至 MetricsCollector。
    """

    def __init__(self, max_records: int = 10000):
        self.max_records = max_records
        self._lock = threading.RLock()
        self._records: deque = deque(maxlen=max_records)
        self._daily_totals: Dict[str, Dict[str, float]] = defaultdict(
            lambda: {"carbon_gco2": 0.0, "energy_kwh": 0.0, "cost_usd": 0.0, "operations": 0}
        )

    def record(
        self,
        op_type: MemoryOperationType,
        carbon_gco2: float,
        energy_kwh: float = 0.0,
        cost_usd: float = 0.0,
        duration_ms: float = 0.0,
        intensity_gco2: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """记录一次操作的碳审计。"""
        audit_id = f"audit_{uuid.uuid4().hex[:12]}"
        record = CarbonAuditRecord(
            audit_id=audit_id,
            operation_type=op_type,
            carbon_emitted_gco2=carbon_gco2,
            energy_consumed_kwh=energy_kwh,
            intensity_at_time=intensity_gco2,
            cost_usd=cost_usd,
            duration_ms=duration_ms,
            metadata=metadata or {},
        )

        with self._lock:
            self._records.append(record)

            # 更新日汇总
            day_key = time.strftime("%Y-%m-%d", time.localtime(record.timestamp))
            day = self._daily_totals[day_key]
            day["carbon_gco2"] += carbon_gco2
            day["energy_kwh"] += energy_kwh
            day["cost_usd"] += cost_usd
            day["operations"] += 1

        return audit_id

    def get_daily_summary(self, date_str: Optional[str] = None) -> Dict[str, Any]:
        """获取指定日期的碳足迹汇总。"""
        if date_str is None:
            date_str = time.strftime("%Y-%m-%d", time.localtime())
        with self._lock:
            return dict(self._daily_totals.get(date_str, {}))

    def get_total_footprint(self) -> Dict[str, float]:
        """获取总碳足迹。"""
        with self._lock:
            total_carbon = sum(r.carbon_emitted_gco2 for r in self._records)
            total_energy = sum(r.energy_consumed_kwh for r in self._records)
            total_cost = sum(r.cost_usd for r in self._records)
            return {
                "total_carbon_gco2": total_carbon,
                "total_carbon_kgco2": total_carbon / 1000.0,
                "total_energy_kwh": total_energy,
                "total_cost_usd": total_cost,
                "total_operations": len(self._records),
            }

    def get_footprint_by_type(self) -> Dict[str, Dict[str, float]]:
        """按操作类型聚合碳足迹。"""
        with self._lock:
            by_type: Dict[str, Dict[str, List[float]]] = defaultdict(
                lambda: {"carbon": [], "cost": [], "count": 0}
            )
            for r in self._records:
                t = r.operation_type.value
                by_type[t]["carbon"].append(r.carbon_emitted_gco2)
                by_type[t]["cost"].append(r.cost_usd)
                by_type[t]["count"] += 1

            return {
                t: {
                    "total_carbon": sum(v["carbon"]),
                    "avg_carbon": np.mean(v["carbon"]) if v["carbon"] else 0.0,
                    "total_cost": sum(v["cost"]),
                    "count": v["count"],
                }
                for t, v in by_type.items()
            }

    def export_to_metrics_collector(self) -> Dict[str, Any]:
        """导出数据到 MetricsCollector 兼容格式。

        与 memory_observability.py 的 MetricsCollector 接口对齐。
        """
        totals = self.get_total_footprint()
        by_type = self.get_footprint_by_type()
        daily = {str(k): dict(v) for k, v in dict(self._daily_totals).items()}

        return {
            "source": "OperationCarbonAuditor",
            "timestamp": time.time(),
            "totals": totals,
            "by_operation_type": by_type,
            "daily_totals": daily,
        }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时指标。"""
        with self._lock:
            return {
                "total_records": len(self._records),
                "max_records": self.max_records,
                **self.get_total_footprint(),
                "daily_totals_days": len(self._daily_totals),
            }
