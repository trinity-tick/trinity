"""
P11-2: Event-Anchored Preference Adapter (对标 FinPerMA arXiv 2608.04095)
==========================================================================

三层冲击模型 (ImpactModel) 将外部事件锚定于用户偏好变化：
  - Layer1: 确定性规则引擎 (ImpactConstraint)
  - Layer2: 受控叙述生成 (NarrativeSynthesis)
  - Layer3: 自动化质量筛查 (AutoQualityScreen)

PostShockCheckpoint: 检测系统是否已将重大事件集成到持久用户模型中。
分离 StableProfileRecall（稳定画像回召）与 EventConditionedAdaptation
（事件条件偏好适配）。事件流锚定于日期 (2020–2026)，支持
macro / industry / personal 三级事件粒度。

Reference:
  - FinPerMA — Financial Persona Memory Adapter, arXiv:2608.04095 (Aug 4, 2026)
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举 ────────────────────────────────────────────────────────────

class EventScope(Enum):
    """事件作用域层级"""
    MACRO = "macro"           # 宏观事件（政策/经济/全球）
    INDUSTRY = "industry"     # 行业事件（技术/竞争/标准）
    PERSONAL = "personal"     # 个人事件（经历/偏好/交互）


class ImpactLevel(Enum):
    """冲击强度等级"""
    NEGLIGIBLE = 0            # 可忽略
    MINOR = 1                 # 轻微调整
    MODERATE = 2              # 中等偏离
    SIGNIFICANT = 3           # 显著偏移
    PARADIGM_SHIFT = 4        # 范式转变


class ShockStatus(Enum):
    """冲击后检测状态"""
    DETECTED = "detected"               # 已检测到
    PROCESSING = "processing"           # 集成为持久模型
    INTEGRATED = "integrated"           # 已完成集成
    REJECTED = "rejected"               # 被规则引擎拒绝
    PENDING_REVIEW = "pending_review"   # 待人工审核


class AdaptationMode(Enum):
    """适配模式"""
    OFFLINE = "offline"           # 离线批处理
    ONLINE = "online"             # 在线增量
    HYBRID = "hybrid"             # 混合模式


# ── 数据类 ──────────────────────────────────────────────────────────

@dataclass
class ImpactConstraint:
    """Layer1 确定性规则约束"""
    constraint_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    scope: EventScope = EventScope.PERSONAL
    condition: str = ""                       # 规则条件表达式
    min_confidence: float = 0.7               # 最小置信度阈值
    max_deviation: float = 0.3                # 最大允许偏离（基线百分比）
    cooldown_days: int = 30                   # 同一事件类型的冷却期
    requires_human_review: bool = False

    def evaluate(
        self, event: "EventRecord", baseline: Optional["PreferenceBaseline"] = None
    ) -> Tuple[bool, str]:
        """评估事件是否通过规则约束。"""
        reasons = []
        # 冷却检查
        if event.age_days < self.cooldown_days and event.similar_prior:
            reasons.append(f"cooldown: {event.age_days}d < {self.cooldown_days}d")
            return False, "; ".join(reasons)
        # 置信度检查
        if event.confidence < self.min_confidence:
            reasons.append(
                f"confidence {event.confidence:.2f} < {self.min_confidence}"
            )
            return False, "; ".join(reasons)
        return True, "passed"


@dataclass
class EventRecord:
    """事件记录 —— 锚定于具体日期"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    anchor_date: str = ""                     # YYYY-MM-DD 格式
    scope: EventScope = EventScope.PERSONAL
    title: str = ""
    description: str = ""
    keywords: List[str] = field(default_factory=list)
    confidence: float = 1.0
    source: str = ""                          # 事件来源
    age_days: float = 0.0                      # 相对今天的天数
    similar_prior: bool = False               # 是否存在相似历史事件
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "anchor_date": self.anchor_date,
            "scope": self.scope.value,
            "title": self.title,
            "keywords": self.keywords,
            "confidence": self.confidence,
        }


@dataclass
class PreferenceBaseline:
    """偏好基线快照（稳定画像）"""
    profile_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    snapshot_time: float = field(default_factory=time.time)
    domains: Dict[str, float] = field(default_factory=dict)   # domain -> preference_score
    top_interests: List[str] = field(default_factory=list)
    risk_tolerance: float = 0.5
    version: int = 1


@dataclass
class PreferenceShift:
    """偏好偏移记录"""
    shift_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_id: str = ""
    domain: str = ""
    baseline_score: float = 0.0
    adapted_score: float = 0.0
    delta: float = 0.0
    impact_level: ImpactLevel = ImpactLevel.NEGLIGIBLE
    reasoning: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class PostShockRecord:
    """冲击后集成状态记录"""
    checkpoint_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_id: str = ""
    status: ShockStatus = ShockStatus.DETECTED
    detected_at: float = field(default_factory=time.time)
    integrated_at: Optional[float] = None
    stable_profile_snapshot: Optional[PreferenceBaseline] = None
    adapted_profile_snapshot: Optional[PreferenceBaseline] = None
    integration_notes: str = ""


# ── PostShockDetector ───────────────────────────────────────────────

class PostShockDetector:
    """冲击后检测器：检测系统是否已集成重大事件。

    工作流：
      1. 监听新事件入库
      2. 对 events 评估 impact_level
      3. 若 impact_level >= SIGNIFICANT 且未集成 → 标记为 DETECTED
      4. 跟踪集成进度 → PROCESSING → INTEGRATED / REJECTED
    """

    def __init__(self, integration_threshold: ImpactLevel = ImpactLevel.SIGNIFICANT):
        self.integration_threshold = integration_threshold
        self._checkpoints: Dict[str, PostShockRecord] = {}
        self._lock = threading.RLock()
        self._integration_queue: deque[PostShockRecord] = deque()
        logger.info(
            f"[PostShockDetector] Initialized (threshold={integration_threshold.name})"
        )

    def detect(self, event: EventRecord, impact: ImpactLevel) -> PostShockRecord:
        """评估事件是否构成冲击并创建检查点。"""
        with self._lock:
            if impact.value < self.integration_threshold.value:
                record = PostShockRecord(
                    event_id=event.event_id,
                    status=ShockStatus.REJECTED,
                )
                self._checkpoints[event.event_id] = record
                return record

            record = PostShockRecord(
                event_id=event.event_id,
                status=ShockStatus.DETECTED,
            )
            self._checkpoints[event.event_id] = record
            self._integration_queue.append(record)
            logger.info(
                f"[PostShockDetector] Shock detected: {event.title} "
                f"(impact={impact.name})"
            )
            return record

    def mark_integrated(
        self,
        event_id: str,
        stable_profile: PreferenceBaseline,
        adapted_profile: PreferenceBaseline,
    ) -> PostShockRecord:
        """标记事件已成功集成到持久用户模型中。"""
        with self._lock:
            record = self._checkpoints.get(event_id)
            if record is None:
                record = PostShockRecord(event_id=event_id)
                self._checkpoints[event_id] = record
            record.status = ShockStatus.INTEGRATED
            record.stable_profile_snapshot = stable_profile
            record.adapted_profile_snapshot = adapted_profile
            record.integrated_at = time.time()
            return record

    def pending_shocks(self) -> List[PostShockRecord]:
        """获取尚未完全集成的冲击列表。"""
        return [
            r
            for r in self._checkpoints.values()
            if r.status in (ShockStatus.DETECTED, ShockStatus.PROCESSING)
        ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            statuses = defaultdict(int)
            for r in self._checkpoints.values():
                statuses[r.status.value] += 1
            return {
                "total_checkpoints": len(self._checkpoints),
                "by_status": dict(statuses),
                "pending": len(self.pending_shocks()),
                "queue_size": len(self._integration_queue),
            }


# ── ImpactModel ─────────────────────────────────────────────────────

class ImpactModel:
    """三层冲击模型。

    Layer1 (ImpactConstraint): 确定性规则引擎
      - 冷却期检查
      - 置信度阈值
      - 最大允许偏离

    Layer2 (Narrative Synthesis): 受控叙述生成
      - 将事件转换为领域偏好的自然语言描述
      - 受 Layer1 约束，仅处理通过规则筛选的事件

    Layer3 (Auto Quality Screen): 自动化质量筛查
      - 检查生成结果的一致性、偏差
      - 标记需人工审核的异常
    """

    def __init__(self):
        self._constraints: List[ImpactConstraint] = []
        self._lock = threading.RLock()
        # 内置规则
        self._add_default_constraints()
        logger.info("[ImpactModel] Initialized with default constraints")

    def _add_default_constraints(self) -> None:
        """添加默认约束规则。"""
        defaults = [
            ImpactConstraint(
                scope=EventScope.MACRO,
                condition="age_days > 0",
                min_confidence=0.80,
                max_deviation=0.15,
                cooldown_days=90,
                requires_human_review=True,
            ),
            ImpactConstraint(
                scope=EventScope.INDUSTRY,
                condition="age_days >= 0",
                min_confidence=0.75,
                max_deviation=0.20,
                cooldown_days=30,
            ),
            ImpactConstraint(
                scope=EventScope.PERSONAL,
                condition="age_days >= 0",
                min_confidence=0.70,
                max_deviation=0.30,
                cooldown_days=14,
            ),
        ]
        self._constraints.extend(defaults)

    # ── Layer 1 ──

    def layer1_evaluate(self, event: EventRecord) -> Tuple[bool, str, Optional[ImpactConstraint]]:
        """Layer1: 确定性规则评估。"""
        for constraint in self._constraints:
            if constraint.scope == event.scope:
                passed, reason = constraint.evaluate(event)
                if not passed:
                    return False, reason, constraint
                return True, reason, constraint
        return False, "no matching constraint", None

    # ── Layer 2 ──

    def layer2_narrative(
        self, event: EventRecord, baseline: Optional[PreferenceBaseline] = None
    ) -> str:
        """Layer2: 受控叙述生成。

        将事件转化为偏好适配描述，受 Layer1 约束。
        实际生产环境可接入 LLM 生成；此处提供基于模板的确定性生成。
        """
        scope_prefix = {
            EventScope.MACRO: "[宏观]",
            EventScope.INDUSTRY: "[行业]",
            EventScope.PERSONAL: "[个人]",
        }
        prefix = scope_prefix.get(event.scope, "")

        narrative_parts = [f"{prefix} 事件锚定于 {event.anchor_date}: {event.title}"]
        if event.description:
            narrative_parts.append(f"详情: {event.description}")
        if event.keywords:
            narrative_parts.append(f"关键信号: {', '.join(event.keywords)}")

        if baseline:
            narrative_parts.append(
                f"基线风险容忍度: {baseline.risk_tolerance:.2f}, "
                f"兴趣领域: {baseline.top_interests[:5]}"
            )

        return "\n".join(narrative_parts)

    # ── Layer 3 ──

    def layer3_quality_screen(
        self, narrative: str, event: EventRecord
    ) -> Tuple[bool, List[str]]:
        """Layer3: 自动化质量筛查。

        检查项：
          - 叙述与事件一致性
          - 输出长度合理性
          - 关键词覆盖率
        """
        issues: List[str] = []

        if len(narrative) < 20:
            issues.append("narrative too short")
        if len(narrative) > 5000:
            issues.append("narrative too long, possible hallucination")

        if event.keywords:
            covered = sum(1 for kw in event.keywords if kw.lower() in narrative.lower())
            if covered < len(event.keywords) * 0.5:
                issues.append(
                    f"keyword coverage {covered}/{len(event.keywords)} below 50%"
                )

        return len(issues) == 0, issues

    # ── Full Pipeline ──

    def process(
        self, event: EventRecord, baseline: Optional[PreferenceBaseline] = None
    ) -> Tuple[ImpactLevel, str, Dict[str, Any]]:
        """执行完整三层冲击模型管道。"""
        report: Dict[str, Any] = {"event_id": event.event_id, "layers": {}}

        # Layer 1
        l1_passed, l1_reason, constraint = self.layer1_evaluate(event)
        report["layers"]["L1"] = {"passed": l1_passed, "reason": l1_reason}
        if not l1_passed:
            return ImpactLevel.NEGLIGIBLE, f"Layer1 rejected: {l1_reason}", report

        # Layer 2
        narrative = self.layer2_narrative(event, baseline)
        report["layers"]["L2"] = {"narrative": narrative[:200] + "..."}

        # Layer 3
        l3_passed, l3_issues = self.layer3_quality_screen(narrative, event)
        report["layers"]["L3"] = {"passed": l3_passed, "issues": l3_issues}

        # 综合判定冲击等级
        impact = self._assess_impact(event, l3_passed)

        return impact, narrative, report

    def _assess_impact(self, event: EventRecord, quality_passed: bool) -> ImpactLevel:
        """综合评估冲击等级。"""
        base = ImpactLevel.MINOR
        if event.scope == EventScope.MACRO:
            base = ImpactLevel.SIGNIFICANT
        elif event.scope == EventScope.INDUSTRY:
            base = ImpactLevel.MODERATE
        if not quality_passed:
            base = ImpactLevel(base.value - 1) if base.value > 0 else base
        return base

    def add_constraint(self, constraint: ImpactConstraint) -> None:
        with self._lock:
            self._constraints.append(constraint)

    def statistics(self) -> Dict[str, Any]:
        return {
            "constraint_count": len(self._constraints),
            "constraints_by_scope": {
                s.value: len([c for c in self._constraints if c.scope == s])
                for s in EventScope
            },
        }


# ── PreferenceShiftTracker ──────────────────────────────────────────

class PreferenceShiftTracker:
    """偏好偏移追踪器。

    记录事件触发的偏好变化，支持：
      - 按 domain 聚合偏移量
      - 按时间范围查询历史偏移
      - 偏移趋势分析
    """

    def __init__(self, window_days: int = 365):
        self.window_days = window_days
        self._shifts: List[PreferenceShift] = []
        self._domain_accumulator: Dict[str, List[float]] = defaultdict(list)
        self._lock = threading.RLock()

    def record_shift(self, shift: PreferenceShift) -> None:
        with self._lock:
            self._shifts.append(shift)
            self._domain_accumulator[shift.domain].append(shift.delta)

    def domain_trend(self, domain: str, n: int = 10) -> List[float]:
        """获取指定领域的最近 n 次偏移趋势。"""
        with self._lock:
            deltas = self._domain_accumulator.get(domain, [])
            return deltas[-n:] if n > 0 else deltas

    def cumulative_shift(self, domain: str) -> float:
        """某领域的累计偏移量。"""
        with self._lock:
            return sum(self._domain_accumulator.get(domain, []))

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            domains = {
                d: {
                    "count": len(v),
                    "cumulative": sum(v),
                    "mean_delta": sum(v) / len(v) if v else 0.0,
                }
                for d, v in self._domain_accumulator.items()
            }
            return {
                "total_shifts": len(self._shifts),
                "domains": domains,
            }


# ── StableProfileRecall ─────────────────────────────────────────────

class StableProfileRecall:
    """稳定画像回召：在无外部事件冲击时提供基线偏好。

    与 EventConditionedAdaptation 分离：
      - StableProfileRecall: 长期稳定偏好（不含临时事件影响）
      - EventConditionedAdaptation: 在事件上下文中调整的偏好
    """

    def __init__(self):
        self._baselines: Dict[str, PreferenceBaseline] = {}
        self._current_baseline: Optional[PreferenceBaseline] = None
        self._lock = threading.RLock()

    def set_baseline(self, baseline: PreferenceBaseline) -> None:
        with self._lock:
            self._baselines[baseline.profile_id] = baseline
            self._current_baseline = baseline

    def recall(self) -> Optional[PreferenceBaseline]:
        """回召当前稳定画像。"""
        return self._current_baseline

    def recall_domain(self, domain: str) -> Optional[float]:
        """回召特定领域的稳定偏好得分。"""
        if self._current_baseline is None:
            return None
        return self._current_baseline.domains.get(domain)

    def statistics(self) -> Dict[str, Any]:
        return {
            "baseline_count": len(self._baselines),
            "current_profile_id": (
                self._current_baseline.profile_id if self._current_baseline else None
            ),
        }


# ── EventConditionedAdaptation ──────────────────────────────────────

class EventConditionedAdaptation:
    """事件条件偏好适配器。

    当检测到事件时，基于稳定画像 + 事件上下文动态调整偏好输出。
    支持 macro / industry / personal 三级事件，按 scope 加权。
    """

    def __init__(self):
        self._active_events: Dict[str, EventRecord] = {}
        self._adaptation_cache: Dict[str, PreferenceBaseline] = {}
        self._lock = threading.RLock()

    def adapt(
        self,
        baseline: PreferenceBaseline,
        events: List[EventRecord],
        impact_model: Optional[ImpactModel] = None,
    ) -> Tuple[PreferenceBaseline, List[PreferenceShift]]:
        """基于当前事件动态调整偏好。

        返回 (adapted_profile, shifts)。
        各 scope 事件按固定权重影响偏好：
          MACRO: 0.10, INDUSTRY: 0.07, PERSONAL: 0.05
        """
        scope_weights = {
            EventScope.MACRO: 0.10,
            EventScope.INDUSTRY: 0.07,
            EventScope.PERSONAL: 0.05,
        }

        adapted_domains = dict(baseline.domains)
        shifts: List[PreferenceShift] = []

        for event in events:
            with self._lock:
                self._active_events[event.event_id] = event

            weight = scope_weights.get(event.scope, 0.01)

            for domain in baseline.domains:
                # 模拟事件对偏好的方向性影响
                signal = self._extract_signal(event, domain)
                delta = signal * weight
                old_score = adapted_domains.get(domain, 0.0)
                new_score = max(0.0, min(1.0, old_score + delta))
                adapted_domains[domain] = new_score

                if abs(delta) > 0.01:
                    shifts.append(
                        PreferenceShift(
                            event_id=event.event_id,
                            domain=domain,
                            baseline_score=old_score,
                            adapted_score=new_score,
                            delta=delta,
                            impact_level=self._delta_to_impact(delta),
                            reasoning=f"Event {event.title} (scope={event.scope.value})",
                        )
                    )

        adapted_profile = PreferenceBaseline(
            domains=adapted_domains,
            top_interests=baseline.top_interests,
            risk_tolerance=baseline.risk_tolerance,
            version=baseline.version + 1,
        )

        cache_key = "+".join(e.event_id for e in events)
        with self._lock:
            self._adaptation_cache[cache_key] = adapted_profile

        return adapted_profile, shifts

    @staticmethod
    def _extract_signal(event: EventRecord, domain: str) -> float:
        """从事件提取对特定 domain 的偏好信号。"""
        text = (event.title + " " + event.description + " " + " ".join(event.keywords)).lower()
        if domain.lower() in text:
            return 0.3  # 正向关联
        # 简单启发式：宏观事件对所有领域有微弱影响
        if event.scope == EventScope.MACRO:
            return 0.05
        return 0.0

    @staticmethod
    def _delta_to_impact(delta: float) -> ImpactLevel:
        abs_d = abs(delta)
        if abs_d < 0.02:
            return ImpactLevel.NEGLIGIBLE
        elif abs_d < 0.05:
            return ImpactLevel.MINOR
        elif abs_d < 0.10:
            return ImpactLevel.MODERATE
        elif abs_d < 0.20:
            return ImpactLevel.SIGNIFICANT
        else:
            return ImpactLevel.PARADIGM_SHIFT

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_events": len(self._active_events),
                "adaptation_cache_size": len(self._adaptation_cache),
            }


# ── EventGroundedAdapter (主入口) ───────────────────────────────────

class EventGroundedAdapter:
    """事件锚定偏好适配器主入口。

    整合 ImpactModel + PostShockDetector + PreferenceShiftTracker +
    StableProfileRecall + EventConditionedAdaptation 五组件。

    典型工作流：
      1. 接收 EventRecord
      2. ImpactModel.process() 执行三层冲击模型
      3. PostShockDetector.detect() 检测是否需要集成
      4. 若冲击显著，EventConditionedAdaptation.adapt() 调整偏好
      5. PreferenceShiftTracker.record_shift() 记录偏移
    """

    def __init__(self, adaptation_mode: AdaptationMode = AdaptationMode.HYBRID):
        self.adaptation_mode = adaptation_mode
        self.impact_model = ImpactModel()
        self.shock_detector = PostShockDetector()
        self.shift_tracker = PreferenceShiftTracker()
        self.stable_recall = StableProfileRecall()
        self.event_adapter = EventConditionedAdaptation()
        self._lock = threading.RLock()
        logger.info(
            f"[EventGroundedAdapter] Initialized (mode={adaptation_mode.value})"
        )

    def set_stable_baseline(self, baseline: PreferenceBaseline) -> None:
        self.stable_recall.set_baseline(baseline)

    def process_event(self, event: EventRecord) -> Dict[str, Any]:
        """处理单个事件：完整三层管道 + 冲击检测 + 偏好适配。"""
        result: Dict[str, Any] = {"event_id": event.event_id}

        # 获取稳定画像
        baseline = self.stable_recall.recall()

        # 三层冲击模型
        impact, narrative, report = self.impact_model.process(event, baseline)
        result["impact_level"] = impact.name
        result["narrative"] = narrative[:500]
        result["layers"] = report.get("layers", {})

        # 冲击检测
        checkpoint = self.shock_detector.detect(event, impact)
        result["shock_status"] = checkpoint.status.value

        # 偏好适配（仅当冲击显著）
        if impact.value >= ImpactLevel.SIGNIFICANT.value and baseline:
            adapted, shifts = self.event_adapter.adapt(baseline, [event])
            for shift in shifts:
                self.shift_tracker.record_shift(shift)
            result["preference_shifts"] = len(shifts)
            if shifts and impact.value >= ImpactLevel.PARADIGM_SHIFT.value:
                self.shock_detector.mark_integrated(
                    event.event_id, baseline, adapted
                )
                result["shock_status"] = ShockStatus.INTEGRATED.value

        return result

    def statistics(self) -> Dict[str, Any]:
        return {
            "adaptation_mode": self.adaptation_mode.value,
            "impact_model": self.impact_model.statistics(),
            "shock_detector": self.shock_detector.statistics(),
            "shift_tracker": self.shift_tracker.statistics(),
            "stable_recall": self.stable_recall.statistics(),
            "event_adapter": self.event_adapter.statistics(),
        }

    def __repr__(self) -> str:
        return (
            f"EventGroundedAdapter(mode={self.adaptation_mode.value}, "
            f"shocks_pending={len(self.shock_detector.pending_shocks())})"
        )
