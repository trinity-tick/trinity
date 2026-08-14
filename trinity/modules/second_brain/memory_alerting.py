"""
P23-7: Memory Alerting — 记忆异常告警系统

对标方案: Memory Alerting for Agentic Systems (2026)
核心发现: 阈值驱动的自动告警触发体系，覆盖高延迟/高错误率/低命中率/腐化检测；
        多通道通知（PagerDuty/邮件/SMS）支持告警升级与静默期管理；
        分级告警策略防止告警风暴，保障运维效率。
三元语: 阈值监控 → 告警触发 → 多通道分发 → 升级策略 → 静默管理

设计要点:
- AlertSeverity: 告警严重级别（INFO / WARNING / CRITICAL / EMERGENCY）
- AlertChannel: 通知通道（PagerDuty / 邮件 / SMS / Webhook）
- AlertStatus: 告警生命周期状态（FIRING / ACKNOWLEDGED / RESOLVED / SILENCED）
- AlertRule: 告警规则定义 — 指标类型、阈值、持续时长、通道、升级策略
- AlertEvent: 告警事件实例 — 含触发时间、当前等级、通道发送状态
- ThresholdMonitor: 阈值监控器 — 持续采样SLI指标, 评估告警条件
- AlertDispatcher: 告警分发器 — 按规则路由到指定通道, 记录发送状态
- SilenceManager: 静默期管理器 — 支持按标签/时间窗口/规则ID的静默
- MemoryAlertingEngine: 统一编排器 — 线程安全，支持 statistics() 运行时指标
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


class AlertSeverity(Enum):
    """告警严重级别"""
    INFO = "info"                                     # 信息级
    WARNING = "warning"                               # 警告级
    CRITICAL = "critical"                             # 严重级
    EMERGENCY = "emergency"                           # 紧急级（需立即响应）


class AlertChannel(Enum):
    """告警通知通道"""
    PAGERDUTY = "pagerduty"                           # PagerDuty 值班告警
    EMAIL = "email"                                   # 邮件通知
    SMS = "sms"                                       # 短信通知
    WEBHOOK = "webhook"                               # Webhook 回调


class AlertStatus(Enum):
    """告警生命周期状态"""
    FIRING = "firing"                                 # 触发中
    ACKNOWLEDGED = "acknowledged"                     # 已确认
    RESOLVED = "resolved"                             # 已恢复
    SILENCED = "silenced"                             # 已静默


# ============================================================================
# Dataclasses
# ============================================================================


@dataclass
class AlertRule:
    """告警规则定义"""
    rule_id: str                                      # 规则唯一ID
    name: str                                         # 规则名称
    metric_name: str                                  # 监控指标名称
    severity: AlertSeverity                           # 默认严重级别
    threshold: float                                  # 阈值
    comparison: str                                   # 比较方式: gt / lt / gte / lte
    sustained_duration_s: int                         # 持续时长（秒），避免瞬态抖动
    channels: List[AlertChannel]                      # 通知通道列表
    escalation_policy: Dict[AlertSeverity, List[AlertChannel]] = field(default_factory=dict)
    escalation_delay_s: int = 300                     # 升级延迟（秒）
    labels: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "metric_name": self.metric_name,
            "severity": self.severity.value,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "sustained_duration_s": self.sustained_duration_s,
            "channels": [c.value for c in self.channels],
            "enabled": self.enabled,
            "labels": self.labels,
        }


@dataclass
class AlertEvent:
    """告警事件实例"""
    event_id: str                                     # 事件唯一ID
    rule_id: str                                      # 关联规则ID
    severity: AlertSeverity                           # 当前严重级别
    status: AlertStatus                               # 当前状态
    metric_value: float                               # 触发时的指标值
    threshold: float                                  # 触发阈值
    fired_at: float = field(default_factory=time.time)
    acknowledged_at: Optional[float] = None
    resolved_at: Optional[float] = None
    last_escalated_at: Optional[float] = None
    message: str = ""
    channel_results: Dict[str, str] = field(default_factory=dict)  # channel -> status
    labels: Dict[str, str] = field(default_factory=dict)


@dataclass
class SilenceWindow:
    """静默窗口"""
    silence_id: str
    rule_ids: List[str]                               # 静默的规则ID列表（空=全部）
    labels: Dict[str, str]                            # 按标签匹配静默
    starts_at: float = field(default_factory=time.time)
    ends_at: float = field(default_factory=lambda: time.time() + 3600.0)  # 默认1小时
    reason: str = ""
    created_by: str = "system"


# ============================================================================
# ThresholdMonitor — 阈值监控器
# ============================================================================


class ThresholdMonitor:
    """阈值监控器 — 持续采样SLI指标并评估告警条件

    核心功能:
    - 规则注册与管理
    - 指标采样与阈值对比
    - 持续时间检测（防止瞬态抖动误告警）
    - 告警事件生成与去重
    """

    def __init__(self):
        self._rules: Dict[str, AlertRule] = {}
        self._event_history: Dict[str, List[AlertEvent]] = {}
        self._breach_timers: Dict[str, float] = {}     # rule_id -> first_breach_time
        self._active_events: Dict[str, AlertEvent] = {}  # rule_id -> current active event
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"rules_registered": 0, "events_fired": 0, "events_resolved": 0}

    def register_rule(self, rule: AlertRule):
        """注册告警规则"""
        with self._lock:
            self._rules[rule.rule_id] = rule
            self._event_history[rule.rule_id] = []
            self._stats["rules_registered"] += 1

    def unregister_rule(self, rule_id: str):
        """注销告警规则"""
        with self._lock:
            self._rules.pop(rule_id, None)

    def evaluate(self, metric_name: str, value: float, labels: Optional[Dict[str, str]] = None) -> List[AlertEvent]:
        """评估单个指标采样值，返回新触发的告警事件列表"""
        triggered: List[AlertEvent] = []
        now = time.time()

        with self._lock:
            for rule in self._rules.values():
                if not rule.enabled or rule.metric_name != metric_name:
                    continue

                breached = self._check_breach(rule, value)
                if breached:
                    if rule.rule_id not in self._breach_timers:
                        self._breach_timers[rule.rule_id] = now
                    elif now - self._breach_timers[rule.rule_id] >= rule.sustained_duration_s:
                        # 持续超过阈值，触发告警
                        if rule.rule_id not in self._active_events:
                            event = AlertEvent(
                                event_id=f"evt_{rule.rule_id}_{uuid.uuid4().hex[:8]}",
                                rule_id=rule.rule_id,
                                severity=rule.severity,
                                status=AlertStatus.FIRING,
                                metric_value=value,
                                threshold=rule.threshold,
                                message=f"{metric_name} = {value:.4f}, threshold {rule.comparison} {rule.threshold}",
                                labels=labels or {},
                            )
                            self._active_events[rule.rule_id] = event
                            self._event_history[rule.rule_id].append(event)
                            self._stats["events_fired"] += 1
                            triggered.append(event)
                else:
                    # 恢复正常
                    if rule.rule_id in self._breach_timers:
                        del self._breach_timers[rule.rule_id]
                    if rule.rule_id in self._active_events:
                        event = self._active_events.pop(rule.rule_id)
                        event.status = AlertStatus.RESOLVED
                        event.resolved_at = now
                        self._stats["events_resolved"] += 1

        return triggered

    def _check_breach(self, rule: AlertRule, value: float) -> bool:
        """检查是否越过阈值"""
        if rule.comparison == "gt":
            return value > rule.threshold
        elif rule.comparison == "lt":
            return value < rule.threshold
        elif rule.comparison == "gte":
            return value >= rule.threshold
        elif rule.comparison == "lte":
            return value <= rule.threshold
        return False

    def acknowledge_event(self, event_id: str) -> Optional[AlertEvent]:
        """确认告警事件"""
        with self._lock:
            for events in self._event_history.values():
                for event in events:
                    if event.event_id == event_id and event.status == AlertStatus.FIRING:
                        event.status = AlertStatus.ACKNOWLEDGED
                        event.acknowledged_at = time.time()
                        return event
        return None

    def get_active_events(self) -> List[AlertEvent]:
        """获取当前活跃告警"""
        with self._lock:
            return list(self._active_events.values())

    def get_stats(self) -> Dict[str, Any]:
        """获取监控器统计信息"""
        with self._lock:
            return {
                **self._stats,
                "active_events": len(self._active_events),
            }


# ============================================================================
# AlertDispatcher — 告警分发器
# ============================================================================


class AlertDispatcher:
    """告警分发器 — 按规则路由到指定通道

    核心功能:
    - 多通道通知路由（PagerDuty / 邮件 / SMS / Webhook）
    - 告警升级策略（超时未确认自动升级到更严重级别 + 更广泛通道）
    - 发送状态追踪
    """

    def __init__(self):
        self._channel_handlers: Dict[AlertChannel, bool] = {
            AlertChannel.PAGERDUTY: True,
            AlertChannel.EMAIL: True,
            AlertChannel.SMS: True,
            AlertChannel.WEBHOOK: True,
        }
        self._escalation_policies: Dict[str, Dict[AlertSeverity, List[AlertChannel]]] = {}
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {"dispatched": 0, "escalated": 0, "failed": 0}

    def enable_channel(self, channel: AlertChannel):
        """启用通知通道"""
        with self._lock:
            self._channel_handlers[channel] = True

    def disable_channel(self, channel: AlertChannel):
        """禁用通知通道"""
        with self._lock:
            self._channel_handlers[channel] = False

    def dispatch(self, event: AlertEvent, rule: AlertRule) -> Dict[str, str]:
        """分发告警事件到指定通道"""
        results: Dict[str, str] = {}

        with self._lock:
            channels = rule.channels
            for channel in channels:
                if not self._channel_handlers.get(channel, False):
                    results[channel.value] = "channel_disabled"
                    continue
                results[channel.value] = self._send(channel, event)
                self._stats["dispatched"] += 1

            event.channel_results = results
            return results

    def escalate(self, event: AlertEvent, rule: AlertRule, new_severity: AlertSeverity) -> Dict[str, str]:
        """升级告警 — 提升严重级别并使用升级策略通道"""
        results: Dict[str, str] = {}

        with self._lock:
            old_severity = event.severity
            event.severity = new_severity
            event.last_escalated_at = time.time()

            escalate_channels = rule.escalation_policy.get(new_severity, rule.channels)
            for channel in escalate_channels:
                if not self._channel_handlers.get(channel, False):
                    results[channel.value] = "channel_disabled"
                    continue
                results[channel.value] = self._send(channel, event)
                self._stats["escalated"] += 1

            event.channel_results.update(results)
            return results

    def _send(self, channel: AlertChannel, event: AlertEvent) -> str:
        """模拟发送通知（生产环境对接真实通道SDK）"""
        logger.info(f"Dispatching alert {event.event_id} via {channel.value}: {event.message}")
        return "sent"

    def get_stats(self) -> Dict[str, Any]:
        """获取分发器统计信息"""
        with self._lock:
            return dict(self._stats)


# ============================================================================
# SilenceManager — 静默期管理器
# ============================================================================


class SilenceManager:
    """静默期管理器 — 支持按标签/时间窗口/规则ID静默

    核心功能:
    - 创建/删除静默窗口
    - 基于规则ID + 标签匹配的静默判定
    - 自动过期清理
    """

    def __init__(self):
        self._silences: Dict[str, SilenceWindow] = {}
        self._lock = threading.RLock()

    def create_silence(
        self,
        rule_ids: Optional[List[str]] = None,
        labels: Optional[Dict[str, str]] = None,
        duration_s: int = 3600,
        reason: str = "",
    ) -> SilenceWindow:
        """创建静默窗口"""
        silence_id = f"silence_{uuid.uuid4().hex[:12]}"
        now = time.time()
        window = SilenceWindow(
            silence_id=silence_id,
            rule_ids=rule_ids or [],
            labels=labels or {},
            starts_at=now,
            ends_at=now + duration_s,
            reason=reason,
        )

        with self._lock:
            self._silences[silence_id] = window

        return window

    def delete_silence(self, silence_id: str) -> bool:
        """删除静默窗口"""
        with self._lock:
            if silence_id in self._silences:
                del self._silences[silence_id]
                return True
            return False

    def is_silenced(self, rule_id: str, labels: Optional[Dict[str, str]] = None) -> bool:
        """判断指定规则+标签是否处于静默期"""
        now = time.time()
        with self._lock:
            # 清理过期的静默窗口
            expired = [sid for sid, w in self._silences.items() if w.ends_at <= now]
            for sid in expired:
                del self._silences[sid]

            for window in self._silences.values():
                if now < window.starts_at or now >= window.ends_at:
                    continue
                # 规则ID匹配：空列表=匹配全部
                if window.rule_ids and rule_id not in window.rule_ids:
                    continue
                # 标签匹配：空字典=匹配全部
                if window.labels and labels:
                    if not all(labels.get(k) == v for k, v in window.labels.items()):
                        continue
                return True

        return False

    def list_silences(self) -> List[SilenceWindow]:
        """列出所有活跃静默窗口"""
        now = time.time()
        with self._lock:
            return [w for w in self._silences.values() if w.ends_at > now]

    def get_stats(self) -> Dict[str, Any]:
        """获取静默管理器统计信息"""
        with self._lock:
            now = time.time()
            active = sum(1 for w in self._silences.values() if w.ends_at > now)
            expired = len(self._silences) - active
            return {
                "total_silences": len(self._silences),
                "active_silences": active,
                "expired_silences": expired,
            }


# ============================================================================
# MemoryAlertingEngine — 记忆告警统一编排器
# ============================================================================


class MemoryAlertingEngine:
    """记忆异常告警引擎 — 线程安全

    功能:
    - 协调阈值监控、告警分发、静默管理
    - 支持完整的告警生命周期管理
    - 运行时指标暴露 (statistics())
    """

    def __init__(self):
        self._monitor = ThresholdMonitor()
        self._dispatcher = AlertDispatcher()
        self._silence_manager = SilenceManager()
        self._lock = threading.RLock()

    @property
    def monitor(self) -> ThresholdMonitor:
        return self._monitor

    @property
    def dispatcher(self) -> AlertDispatcher:
        return self._dispatcher

    @property
    def silences(self) -> SilenceManager:
        return self._silence_manager

    def register_and_evaluate(
        self,
        rule: AlertRule,
        metric_value: float,
        labels: Optional[Dict[str, str]] = None,
    ) -> Optional[AlertEvent]:
        """注册规则并立即采样评估

        完整流程：检查静默 → 评估阈值 → 触发告警 → 多通道分发
        """
        with self._lock:
            self._monitor.register_rule(rule)

            # 检查静默
            if self._silence_manager.is_silenced(rule.rule_id, labels):
                logger.debug(f"Rule {rule.rule_id} is silenced, skipping evaluation")
                return None

            # 评估
            events = self._monitor.evaluate(rule.metric_name, metric_value, labels)

            if not events:
                return None

            # 分发到指定通道
            for event in events:
                self._dispatcher.dispatch(event, rule)

            return events[0] if events else None

    def check_escalation(self, event_id: str) -> Optional[AlertEvent]:
        """检查告警是否需要升级（超时未确认）"""
        with self._lock:
            rule = self._monitor._rules.get(event_id.split("_")[1], None) if "_" in event_id else None
            if rule is None:
                return None

            for events in self._monitor._event_history.values():
                for event in events:
                    if event.event_id != event_id:
                        continue
                    if event.status != AlertStatus.FIRING:
                        return None

                    time_since_fire = time.time() - event.fired_at
                    if time_since_fire >= rule.escalation_delay_s:
                        new_severity = self._next_severity(event.severity)
                        if new_severity != event.severity:
                            self._dispatcher.escalate(event, rule, new_severity)
                            return event
            return None

    def _next_severity(self, current: AlertSeverity) -> AlertSeverity:
        """获取下一级严重级别"""
        order = [AlertSeverity.INFO, AlertSeverity.WARNING, AlertSeverity.CRITICAL, AlertSeverity.EMERGENCY]
        try:
            idx = order.index(current)
            if idx + 1 < len(order):
                return order[idx + 1]
        except ValueError:
            pass
        return current

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标"""
        with self._lock:
            return {
                "monitor": self._monitor.get_stats(),
                "dispatcher": self._dispatcher.get_stats(),
                "silences": self._silence_manager.get_stats(),
            }
