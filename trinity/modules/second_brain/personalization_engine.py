"""
# status: orphan (2026-08-15 audit, not in runtime path)
P9-3: PAHF Dual-Feedback Personalization Engine (对标 Meta 2026)
==================================================================

核心设计（基于 "Learning Personalized Agents from Human Feedback", Meta 2026）：
  - 三步循环：
    1. PreActionClarifier（行动前澄清）：识别歧义并追问偏好
    2. PreferenceRetriever（偏好记忆检索）：从显式逐用户记忆中获取偏好上下文
    3. PostActionIntegrator（行动后反馈整合）：用户纠正/确认后更新记忆
  - 偏好漂移自适应：检测偏好变化 → 调整记忆权重 → 快速收敛到新偏好
  - 四阶段评测协议：
    1. Initial Learning（初始学习）
    2. Context Dependency（上下文依赖）
    3. Preference Shift（偏好偏移）
    4. Recovery Speed（恢复速度）
  - 显式逐用户记忆：每位用户独立偏好存储，不混用
  - 双反馈通道：行动前澄清 + 行动后确认

设计要点：
  - 与 existing memory infrastructure 兼容
  - Explicit per-user memory: 隔离不同用户的偏好上下文
  - Dual feedback: 前向澄清 + 后向确认形成闭环
  - Drift detection: 指数加权移动平均检测偏好变化

Reference:
  - Liang et al., "Learning Personalized Agents from Human Feedback" (Meta, ICLR 2026)
  - PAHF: Personalized Agents from Human Feedback (arXiv:2602.16173)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque, OrderedDict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class FeedbackType(Enum):
    """反馈类型。"""
    EXPLICIT_CONFIRM = "explicit_confirm"     # 显式确认
    EXPLICIT_CORRECTION = "explicit_correction"  # 显式纠正
    IMPLICIT_ACCEPT = "implicit_accept"       # 隐式接受（无反馈）
    IMPLICIT_OVERRIDE = "implicit_override"   # 隐式覆盖（用户自己做了别的）
    PREFERENCE_QUERY_RESULT = "preference_query_result"  # 偏好追问结果


class ClarificationNeed(Enum):
    """澄清需求等级。"""
    NONE = "none"           # 无歧义
    LOW = "low"             # 轻微歧义
    MEDIUM = "medium"       # 需要追问
    HIGH = "high"           # 严重歧义，必须追问
    CRITICAL = "critical"   # 歧义导致无法行动


class PreferenceDomain(Enum):
    """偏好领域。"""
    FORMAT = "format"                 # 输出格式偏好
    STYLE = "style"                   # 风格偏好（详细/简洁等）
    CONTENT = "content"               # 内容偏好
    PRIVACY = "privacy"               # 隐私偏好
    NOTIFICATION = "notification"     # 通知偏好
    WORKFLOW = "workflow"             # 工作流偏好
    LANGUAGE = "language"             # 语言偏好
    INTERACTION = "interaction"       # 交互方式偏好
    SEARCH = "search"                 # 搜索偏好
    CUSTOM = "custom"                 # 自定义偏好


class EvalPhase(Enum):
    """PAHF 四阶段评测协议。"""
    INITIAL_LEARNING = "initial_learning"         # 阶段1: 初始学习
    CONTEXT_DEPENDENCY = "context_dependency"     # 阶段2: 上下文依赖
    PREFERENCE_SHIFT = "preference_shift"         # 阶段3: 偏好偏移
    RECOVERY_SPEED = "recovery_speed"             # 阶段4: 恢复速度


# ── 数据结构 ────────────────────────────────────────────────────────


@dataclass
class PreferenceEntry:
    """单条偏好记忆。

    Args:
        entry_id: 条目唯一标识
        user_id: 用户编号
        domain: 偏好领域
        key: 偏好键（如 "output_format" → "markdown"）
        value: 偏好值
        confidence: 置信度 (0.0-1.0)
        weight: 权重（用于 DRIFT 调整）
        source: 来源（反馈类型）
        created_at: 创建时间
        last_updated: 最后更新时间
        activation_count: 激活次数
        metadata: 扩展元数据
    """
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    domain: PreferenceDomain = PreferenceDomain.FORMAT
    key: str = ""
    value: str = ""
    confidence: float = 0.5
    weight: float = 1.0
    source: FeedbackType = FeedbackType.EXPLICIT_CONFIRM
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    activation_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ClarificationQuestion:
    """澄清问题。

    Args:
        question_id: 问题唯一标识
        user_id: 用户编号
        text: 问题文本
        domain: 偏好领域
        options: 可选答案列表
        need_level: 澄清需求等级
        context: 触发澄清的上下文摘要
        asked_at: 提问时间
    """
    question_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    text: str = ""
    domain: PreferenceDomain = PreferenceDomain.FORMAT
    options: List[str] = field(default_factory=list)
    need_level: ClarificationNeed = ClarificationNeed.MEDIUM
    context: str = ""
    asked_at: float = field(default_factory=time.time)


@dataclass
class FeedbackRecord:
    """反馈记录。

    Args:
        record_id: 记录唯一标识
        user_id: 用户编号
        feedback_type: 反馈类型
        action_id: 对应的行动标识
        preference_changes: 由此反馈引起的偏好变更
        raw_response: 用户原始响应
        timestamp: 记录时间
    """
    record_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str = ""
    feedback_type: FeedbackType = FeedbackType.EXPLICIT_CONFIRM
    action_id: str = ""
    preference_changes: List[Dict[str, Any]] = field(default_factory=list)
    raw_response: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class PreferenceSnapshot:
    """偏好快照（用于漂移检测）。

    Args:
        user_id: 用户编号
        domain: 偏好领域
        preferences: 该领域的偏好值签名
        taken_at: 快照时间
    """
    user_id: str
    domain: PreferenceDomain
    preferences: Dict[str, str] = field(default_factory=dict)
    taken_at: float = field(default_factory=time.time)


@dataclass
class EvalMetrics:
    """PAHF 评测指标。

    Args:
        phase: 评测阶段
        personalization_error: 个性化误差（与 ground truth 的偏差）
        response_time_ms: 响应时间（毫秒）
        clarification_count: 澄清次数
        convergence_time_s: 收敛时间（秒）
        drift_detection_latency_s: 漂移检测延迟（秒）
    """
    phase: EvalPhase
    personalization_error: float = 0.0
    response_time_ms: float = 0.0
    clarification_count: int = 0
    convergence_time_s: float = 0.0
    drift_detection_latency_s: float = 0.0


# ── 行动前澄清器 ──────────────────────────────────────────────────


class PreActionClarifier:
    """行动前澄清器 — PAHF 三步循环的第 1 步。

    在行动前识别歧义，判断是否需要向用户追问偏好。
    维护歧义检测规则库和澄清历史。
    """

    def __init__(self, threshold: float = 0.6):
        self._lock = threading.RLock()
        self._threshold = threshold
        self._question_history: Dict[str, List[ClarificationQuestion]] = defaultdict(list)
        self._ambiguity_rules: Dict[PreferenceDomain, List[str]] = {
            PreferenceDomain.FORMAT: ["output format", "display style", "layout"],
            PreferenceDomain.STYLE: ["verbose", "concise", "detailed", "simple"],
            PreferenceDomain.CONTENT: ["which", "prefer", "choose", "between"],
            PreferenceDomain.PRIVACY: ["share", "disclose", "store", "retain"],
            PreferenceDomain.NOTIFICATION: ["notify", "alert", "remind"],
            PreferenceDomain.WORKFLOW: ["how to proceed", "next step", "workflow"],
            PreferenceDomain.LANGUAGE: ["language", "translate", "locale"],
            PreferenceDomain.INTERACTION: ["click", "tap", "swipe", "command"],
            PreferenceDomain.SEARCH: ["search engine", "source", "provider"],
        }
        self._total_clarifications: int = 0

    def assess_ambiguity(
        self,
        user_id: str,
        context: str,
        domain: Optional[PreferenceDomain] = None,
    ) -> ClarificationNeed:
        """评估当前上下文的歧义程度。

        Args:
            user_id: 用户编号
            context: 当前行动上下文
            domain: 限定领域（None=自动检测）

        Returns:
            ClarificationNeed: 澄清需求等级
        """
        with self._lock:
            if not context.strip():
                return ClarificationNeed.NONE

            need = ClarificationNeed.NONE
            context_lower = context.lower()

            # 检查各领域歧义关键词
            for d, keywords in self._ambiguity_rules.items():
                if domain and d != domain:
                    continue
                score = 0.0
                for kw in keywords:
                    if kw in context_lower:
                        score += 0.25
                score = min(score, 1.0)

                if score >= 0.8:
                    need = max(need, ClarificationNeed.CRITICAL)
                elif score >= 0.6:
                    need = max(need, ClarificationNeed.HIGH)
                elif score >= 0.4:
                    need = max(need, ClarificationNeed.MEDIUM)
                elif score >= 0.2:
                    need = max(need, ClarificationNeed.LOW)

            # 已知偏好可降低澄清需求
            # (实际由 PreferenceRetriever 协作判定)

            return need

    def generate_clarification(
        self,
        user_id: str,
        domain: PreferenceDomain,
        context: str,
        options: Optional[List[str]] = None,
    ) -> ClarificationQuestion:
        """生成澄清问题。

        Args:
            user_id: 用户编号
            domain: 偏好领域
            context: 上下文描述
            options: 可选答案（None=自由文本）

        Returns:
            ClarificationQuestion: 澄清问题
        """
        with self._lock:
            need = self.assess_ambiguity(user_id, context, domain)
            q = ClarificationQuestion(
                user_id=user_id,
                text=f"[{domain.value}] {context}. Which do you prefer?",
                domain=domain,
                options=options or [],
                need_level=need,
                context=context,
            )
            self._question_history[user_id].append(q)
            self._total_clarifications += 1
            logger.info(f"Clarification generated: {q.question_id} for user {user_id} [{domain.value}]")
            return q

    def should_clarify(
        self,
        user_id: str,
        context: str,
        preference_confidence: float,
    ) -> bool:
        """判断是否应该发起澄清。

        综合考虑歧义程度和现有偏好置信度。

        Args:
            user_id: 用户编号
            context: 上下文
            preference_confidence: 现有偏好置信度 (0.0-1.0)

        Returns:
            bool: 是否应该澄清
        """
        need = self.assess_ambiguity(user_id, context)
        if need == ClarificationNeed.NONE:
            return False
        if need in (ClarificationNeed.CRITICAL, ClarificationNeed.HIGH):
            return True
        # MEDIUM/LOW: 取决于偏好置信度
        if preference_confidence < self._threshold:
            return True
        return False

    def get_history(self, user_id: str) -> List[ClarificationQuestion]:
        """获取用户澄清历史。"""
        with self._lock:
            return list(self._question_history.get(user_id, []))

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            return {
                "total_clarifications": self._total_clarifications,
                "unique_users": len(self._question_history),
                "threshold": self._threshold,
                "per_user_avg": (
                    self._total_clarifications / len(self._question_history)
                    if self._question_history
                    else 0.0
                ),
            }


# ── 偏好记忆检索器 ───────────────────────────────────────────────


class PreferenceRetriever:
    """偏好记忆检索器 — PAHF 三步循环的第 2 步。

    从显式逐用户记忆中检索偏好上下文，支持置信度加权组合和
    最新优先排序。
    """

    def __init__(self, max_preferences_per_user: int = 1000):
        self._lock = threading.RLock()
        self._preferences: Dict[str, Dict[str, PreferenceEntry]] = defaultdict(dict)
        self._max_per_user = max_preferences_per_user

    def store_preference(self, entry: PreferenceEntry) -> None:
        """存储偏好记忆。

        Args:
            entry: 偏好条目
        """
        with self._lock:
            key = f"{entry.domain.value}:{entry.key}"
            existing = self._preferences[entry.user_id].get(key)
            if existing:
                # 更新而非覆盖，以保持历史线索
                entry.activation_count = existing.activation_count
                entry.created_at = existing.created_at
            self._preferences[entry.user_id][key] = entry
            # 容量限制：LRU 策略淘汰旧偏好
            if len(self._preferences[entry.user_id]) > self._max_per_user:
                oldest = min(
                    self._preferences[entry.user_id].values(),
                    key=lambda e: e.last_updated,
                    default=None,
                )
                if oldest:
                    old_key = f"{oldest.domain.value}:{oldest.key}"
                    del self._preferences[entry.user_id][old_key]

    def retrieve(
        self,
        user_id: str,
        domain: Optional[PreferenceDomain] = None,
        context_hint: Optional[str] = None,
    ) -> Dict[str, PreferenceEntry]:
        """检索用户偏好。

        Args:
            user_id: 用户编号
            domain: 限定领域（None=所有领域）
            context_hint: 上下文提示（用于优先级排序）

        Returns:
            Dict[str, PreferenceEntry]: key → 偏好条目映射
        """
        with self._lock:
            user_prefs = self._preferences.get(user_id, {})
            if not user_prefs:
                return {}

            results = {}
            for key, entry in user_prefs.items():
                if domain and entry.domain != domain:
                    continue
                results[key] = entry
                entry.activation_count += 1

            return results

    def get_preference_value(
        self,
        user_id: str,
        domain: PreferenceDomain,
        key: str,
        default: str = "",
    ) -> str:
        """获取单个偏好值。

        Args:
            user_id: 用户编号
            domain: 偏好领域
            key: 偏好键
            default: 默认值

        Returns:
            str: 偏好值
        """
        with self._lock:
            pkey = f"{domain.value}:{key}"
            entry = self._preferences.get(user_id, {}).get(pkey)
            if entry is None:
                return default
            entry.activation_count += 1
            return entry.value

    def get_context_json(self, user_id: str, domain: Optional[PreferenceDomain] = None) -> Dict[str, Any]:
        """获取偏好上下文（JSON 格式，供下游使用）。

        Args:
            user_id: 用户编号
            domain: 限定领域

        Returns:
            Dict[str, Any]: 偏好上下文
        """
        prefs = self.retrieve(user_id, domain)
        context: Dict[str, Any] = {"user_id": user_id, "preferences": {}}
        for key, entry in sorted(prefs.items(), key=lambda x: x[1].confidence, reverse=True):
            context["preferences"][key] = {
                "value": entry.value,
                "confidence": entry.confidence,
                "weight": entry.weight,
                "source": entry.source.value,
            }
        return context

    def get_confidence(self, user_id: str, domain: PreferenceDomain, key: str) -> float:
        """获取偏好置信度。"""
        with self._lock:
            pkey = f"{domain.value}:{key}"
            entry = self._preferences.get(user_id, {}).get(pkey)
            return entry.confidence if entry else 0.0

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            total_entries = sum(len(prefs) for prefs in self._preferences.values())
            return {
                "total_users": len(self._preferences),
                "total_preferences": total_entries,
                "avg_per_user": total_entries / len(self._preferences) if self._preferences else 0.0,
                "domain_distribution": {
                    d.value: sum(
                        1
                        for prefs in self._preferences.values()
                        for e in prefs.values()
                        if e.domain == d
                    )
                    for d in PreferenceDomain
                },
            }


# ── 行动后反馈整合器 ────────────────────────────────────────────────


class PostActionIntegrator:
    """行动后反馈整合器 — PAHF 三步循环的第 3 步。

    接受用户纠正/确认后更新偏好记忆，驱动偏好漂移自适应。
    """

    # 偏好漂移检测参数
    DRIFT_EWMA_ALPHA = 0.15       # 指数加权移动平均系数
    DRIFT_THRESHOLD = 0.30         # 漂移检测阈值
    DRIFT_WEIGHT_DECAY = 0.05      # 旧偏好权重衰减率

    def __init__(self):
        self._lock = threading.RLock()
        self._feedback_history: Dict[str, List[FeedbackRecord]] = defaultdict(list)
        self._snapshots: Dict[str, List[PreferenceSnapshot]] = defaultdict(list)
        self._drift_ewma: Dict[str, float] = defaultdict(float)
        self._drift_events: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

    def integrate_feedback(
        self,
        user_id: str,
        feedback: FeedbackRecord,
        retriever: PreferenceRetriever,
    ) -> Dict[str, Any]:
        """整合用户反馈并更新偏好记忆。

        Args:
            user_id: 用户编号
            feedback: 反馈记录
            retriever: 偏好检索器实例

        Returns:
            Dict[str, Any]: 整合结果（含漂移检测信息）
        """
        with self._lock:
            self._feedback_history[user_id].append(feedback)
            result: Dict[str, Any] = {
                "feedback_id": feedback.record_id,
                "changes_made": 0,
                "drift_detected": False,
                "drift_magnitude": 0.0,
            }

            # 更新偏好记忆
            for change in feedback.preference_changes:
                domain = PreferenceDomain(change.get("domain", "format"))
                key = change.get("key", "")
                value = change.get("value", "")
                new_confidence = change.get("confidence", 0.5)

                entry = PreferenceEntry(
                    user_id=user_id,
                    domain=domain,
                    key=key,
                    value=value,
                    confidence=new_confidence,
                    source=feedback.feedback_type,
                )
                retriever.store_preference(entry)
                result["changes_made"] += 1

            # 执行偏好漂移检测
            drift_info = self._detect_drift(user_id, retriever)
            if drift_info["drift_detected"]:
                result["drift_detected"] = True
                result["drift_magnitude"] = drift_info["magnitude"]
                self._apply_drift_adaptation(user_id, retriever, drift_info)

            return result

    def _detect_drift(
        self,
        user_id: str,
        retriever: PreferenceRetriever,
    ) -> Dict[str, Any]:
        """检测用户偏好是否发生漂移。

        使用指数加权移动平均 (EWMA) 对比当前偏好快照与历史快照。
        """
        current_prefs = retriever.get_context_json(user_id)
        current_signature = hashlib.md5(
            json.dumps(current_prefs.get("preferences", {}), sort_keys=True).encode()
        ).hexdigest()

        # 获取上次快照
        snapshots = self._snapshots.get(user_id, [])
        if not snapshots:
            # 创建初始快照
            self._snapshots[user_id].append(
                PreferenceSnapshot(
                    user_id=user_id,
                    domain=PreferenceDomain.CUSTOM,
                    preferences={"_sig": current_signature},
                )
            )
            self._drift_ewma[user_id] = 0.0
            return {"drift_detected": False, "magnitude": 0.0}

        prev_snapshot = snapshots[-1]
        prev_sig = prev_snapshot.preferences.get("_sig", "")

        if prev_sig == current_signature:
            return {"drift_detected": False, "magnitude": 0.0}

        # 计算漂移幅度（基于偏好变化的数量）
        current_pref_dict = current_prefs.get("preferences", {})
        prev_pref_dict = retriever.get_context_json(user_id).get("preferences", {})

        changed_count = 0
        total_keys = max(len(current_pref_dict), len(prev_pref_dict))
        if total_keys == 0:
            return {"drift_detected": False, "magnitude": 0.0}

        for key, info in current_pref_dict.items():
            if key not in prev_pref_dict or prev_pref_dict[key].get("value") != info.get("value"):
                changed_count += 1

        drift_magnitude = changed_count / total_keys

        # EWMA 更新
        prev_ewma = self._drift_ewma.get(user_id, 0.0)
        new_ewma = self.DRIFT_EWMA_ALPHA * drift_magnitude + (1 - self.DRIFT_EWMA_ALPHA) * prev_ewma
        self._drift_ewma[user_id] = new_ewma

        # 更新快照
        self._snapshots[user_id].append(
            PreferenceSnapshot(
                user_id=user_id,
                domain=PreferenceDomain.CUSTOM,
                preferences={"_sig": current_signature},
            )
        )

        drift_detected = new_ewma > self.DRIFT_THRESHOLD
        if drift_detected:
            self._drift_events[user_id].append({
                "timestamp": time.time(),
                "magnitude": drift_magnitude,
                "ewma": new_ewma,
            })
            logger.info(f"Preference drift detected for user {user_id}: magnitude={drift_magnitude:.3f}, ewma={new_ewma:.3f}")

        return {"drift_detected": drift_detected, "magnitude": drift_magnitude}

    def _apply_drift_adaptation(
        self,
        user_id: str,
        retriever: PreferenceRetriever,
        drift_info: Dict[str, Any],
    ) -> None:
        """应用漂移自适应：调整旧偏好权重以加速收敛。

        降低被漂移偏好的权重，提高新偏好的置信度。
        """
        with self._lock:
            prefs = retriever.retrieve(user_id)
            now = time.time()
            for key, entry in prefs.items():
                age_days = (now - entry.last_updated) / 86400.0
                # 衰减旧偏好权重
                entry.weight *= max(0.1, 1.0 - self.DRIFT_WEIGHT_DECAY * age_days)
                entry.confidence = max(0.1, entry.confidence - 0.05)

    def get_drift_history(self, user_id: str) -> List[Dict[str, Any]]:
        """获取用户偏好漂移历史。"""
        with self._lock:
            return list(self._drift_events.get(user_id, []))

    def get_feedback_history(self, user_id: str) -> List[FeedbackRecord]:
        """获取用户反馈历史。"""
        with self._lock:
            return list(self._feedback_history.get(user_id, []))

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            total_feedback = sum(len(fb) for fb in self._feedback_history.values())
            total_drifts = sum(len(de) for de in self._drift_events.values())
            return {
                "total_feedback_records": total_feedback,
                "unique_users": len(self._feedback_history),
                "total_drift_events": total_drifts,
                "drift_ewma_values": dict(self._drift_ewma),
            }


# ── PAHF 主引擎 ──────────────────────────────────────────────────────


class PAHFEngine:
    """PAHF 双反馈个性化引擎。

    组合 PreActionClarifier + PreferenceRetriever + PostActionIntegrator
    实现完整的三步循环，并提供四阶段评测协议。

    Usage:
        engine = PAHFEngine()
        # 三步循环
        need_clarify = engine.should_clarify(user_id, action_context)
        if need_clarify:
            question = engine.clarify(user_id, domain, context)
            answer = get_user_response(question)
        prefs = engine.get_preference_context(user_id)
        result = execute_action(context, prefs)
        engine.integrate_feedback(user_id, feedback)
        # 评测
        engine.start_eval_session(user_id)
        metrics = engine.end_eval_session(user_id)
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.clarifier = PreActionClarifier()
        self.retriever = PreferenceRetriever()
        self.integrator = PostActionIntegrator()
        self._eval_sessions: Dict[str, Dict[str, Any]] = {}
        self._total_interactions: int = 0

    # ── 三步循环 ─────────────────────────────────────────────────────

    def should_clarify(
        self,
        user_id: str,
        action_context: str,
        domain: Optional[PreferenceDomain] = None,
    ) -> bool:
        """判断是否需要在行动前澄清（第 1 步入口）。

        Args:
            user_id: 用户编号
            action_context: 行动上下文
            domain: 偏好领域

        Returns:
            bool: 是否应发起澄清
        """
        with self._lock:
            self._total_interactions += 1
            if not action_context.strip():
                return False

            # 获取当前偏好置信度
            confidence = 0.0
            if domain:
                prefs = self.retriever.retrieve(user_id, domain)
                if prefs:
                    confidence = np.mean([e.confidence for e in prefs.values()])

            return self.clarifier.should_clarify(user_id, action_context, confidence)

    def clarify(
        self,
        user_id: str,
        domain: PreferenceDomain,
        context: str,
        options: Optional[List[str]] = None,
    ) -> ClarificationQuestion:
        """生成澄清问题（第 1 步）。

        Args:
            user_id: 用户编号
            domain: 偏好领域
            context: 上下文
            options: 可选答案

        Returns:
            ClarificationQuestion: 澄清问题
        """
        return self.clarifier.generate_clarification(user_id, domain, context, options)

    def get_preference_context(
        self,
        user_id: str,
        domain: Optional[PreferenceDomain] = None,
    ) -> Dict[str, Any]:
        """获取偏好上下文（第 2 步）。

        Args:
            user_id: 用户编号
            domain: 偏好领域

        Returns:
            Dict[str, Any]: 偏好上下文 JSON
        """
        return self.retriever.get_context_json(user_id, domain)

    def get_preference_value(
        self,
        user_id: str,
        domain: PreferenceDomain,
        key: str,
        default: str = "",
    ) -> str:
        """获取单个偏好值（第 2 步快捷方式）。

        Args:
            user_id: 用户编号
            domain: 偏好领域
            key: 偏好键
            default: 默认值

        Returns:
            str: 偏好值
        """
        return self.retriever.get_preference_value(user_id, domain, key, default)

    def go_integrate_feedback(
        self,
        user_id: str,
        feedback_type: FeedbackType,
        action_id: str,
        preference_changes: List[Dict[str, Any]],
        raw_response: str = "",
    ) -> Dict[str, Any]:
        """整合反馈（第 3 步）。

        Args:
            user_id: 用户编号
            feedback_type: 反馈类型
            action_id: 行动标识
            preference_changes: 偏好变更列表
            raw_response: 用户原始响应

        Returns:
            Dict[str, Any]: 整合结果
        """
        fb = FeedbackRecord(
            user_id=user_id,
            feedback_type=feedback_type,
            action_id=action_id,
            preference_changes=preference_changes,
            raw_response=raw_response,
        )
        return self.integrator.integrate_feedback(user_id, fb, self.retriever)

    # ── 四阶段评测协议 ─────────────────────────────────────────────────

    def start_eval_session(self, user_id: str) -> None:
        """开始评测会话。"""
        with self._lock:
            self._eval_sessions[user_id] = {
                "start_time": time.time(),
                "phase": EvalPhase.INITIAL_LEARNING,
                "metrics": [],
            }

    def record_eval_metric(
        self,
        user_id: str,
        metric: EvalMetrics,
    ) -> None:
        """记录评测指标。"""
        with self._lock:
            if user_id in self._eval_sessions:
                self._eval_sessions[user_id]["metrics"].append(metric)

    def advance_eval_phase(self, user_id: str, phase: EvalPhase) -> None:
        """切换评测阶段。"""
        with self._lock:
            if user_id in self._eval_sessions:
                self._eval_sessions[user_id]["phase"] = phase

    def end_eval_session(self, user_id: str) -> Dict[str, Any]:
        """结束评测会话并返回汇总结果。

        Returns:
            Dict[str, Any]: 四阶段评测结果
        """
        with self._lock:
            session = self._eval_sessions.pop(user_id, None)
            if not session:
                return {}

            metrics: List[EvalMetrics] = session.get("metrics", [])
            phase_results: Dict[str, Dict[str, Any]] = {}

            for phase in EvalPhase:
                phase_metrics = [m for m in metrics if m.phase == phase]
                if phase_metrics:
                    avg_error = np.mean([m.personalization_error for m in phase_metrics])
                    avg_latency = np.mean([m.response_time_ms for m in phase_metrics])
                    total_clarify = sum(m.clarification_count for m in phase_metrics)
                    avg_convergence = np.mean([m.convergence_time_s for m in phase_metrics])
                    phase_results[phase.value] = {
                        "avg_personalization_error": float(avg_error),
                        "avg_response_time_ms": float(avg_latency),
                        "total_clarifications": total_clarify,
                        "avg_convergence_time_s": float(avg_convergence),
                    }

            return {
                "user_id": user_id,
                "duration_s": time.time() - session["start_time"],
                "total_metrics": len(metrics),
                "phases": phase_results,
            }

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            return {
                "total_interactions": self._total_interactions,
                "clarifier": self.clarifier.statistics(),
                "retriever": self.retriever.statistics(),
                "integrator": self.integrator.statistics(),
                "active_eval_sessions": len(self._eval_sessions),
            }
