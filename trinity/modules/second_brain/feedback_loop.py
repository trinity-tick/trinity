"""
# status: orphan (2026-08-15 audit, not in runtime path)
P3-3: User Feedback -> Memory Correction Closed Loop (对标 MindMemOS Feedback)
==============================================================================
Implement explicit correction (user directly points out memory errors) and
implicit feedback (infer dissatisfaction/preference changes from subsequent
interactions), with automated decisions: add / update / archive / delete / reinforce.

MindMemOS Feedback 的设计要点：
  - 显式反馈：用户直接指出记忆错误，系统据此修正。
  - 隐式反馈：从后续交互中推断不满或偏好变化，有选择地强化或降级记忆。
  - 决策引擎：判断信号是临时信息、场景偏好还是长期规律，再决定操作。

Reference:
  - MindMemOS: Entity-Attribute-Time 3D memory, Feedback mining (2026.08)
  - PersonaMem-Evo: implicit correction signal extraction
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Data Structures ──────────────────────────────────────────────────────

class FeedbackType(Enum):
    """Classification of feedback signals."""
    EXPLICIT_CORRECTION = "explicit_correction"    # 用户直接指出错误
    IMPLICIT_DISSATISFACTION = "implicit_dissatisfaction"  # 从交互推断不满
    IMPLICIT_OVERRIDE = "implicit_override"        # 行为覆盖了旧偏好
    IMPLICIT_NEGLECT = "implicit_neglect"          # 长期未使用暗示废弃
    REINFORCEMENT = "reinforcement"                # 重复确认强化记忆


class MemoryAction(Enum):
    """Actions the feedback loop can decide to take on a memory."""
    ADD = "add"           # 新增记忆
    UPDATE = "update"     # 更新内容
    ARCHIVE = "archive"   # 归档（不主动召回）
    DELETE = "delete"     # 删除
    REINFORCE = "reinforce"  # 强化（提高权重/置信度）
    NOOP = "noop"         # 不操作


@dataclass
class FeedbackSignal:
    """A parsed feedback signal extracted from user interaction."""
    signal_id: str
    signal_type: FeedbackType
    target_memory_id: Optional[str]       # 目标记忆ID（可推断）
    target_entity: Optional[str]          # 实体名
    target_attribute: Optional[str]       # 属性名
    suggested_value: Optional[str]        # 建议的新值
    confidence: float                     # 0.0 ~ 1.0
    context: str                          # 原始上下文
    timestamp: float = field(default_factory=time.time)


@dataclass
class FeedbackDecision:
    """The decision made by the feedback loop for a signal."""
    signal_id: str
    action: MemoryAction
    memory_id: str
    rationale: str
    details: Dict[str, Any] = field(default_factory=dict)


# ── Feedback Loop Engine ─────────────────────────────────────────────────

class FeedbackLoop:
    """User-feedback-driven memory correction closed loop.

    Usage::

        loop = FeedbackLoop(memory_store=trinity_instance)
        loop.register_explicit_correction(
            "User prefers Python over Java",
            target_memory_id="mem_123",
        )
        loop.detect_implicit_feedback(
            recent_interactions=[...],
            current_preferences={...},
        )
        decisions = loop.decide_and_apply()
    """

    def __init__(
        self,
        memory_store: Any = None,
        decision_threshold: float = 0.5,
        neglect_days: int = 30,
        archive_path: Optional[str] = None,
    ):
        """Initialize the FeedbackLoop.

        Args:
            memory_store: Trinity memory store (must support memory CRUD ops).
            decision_threshold: Confidence threshold for auto-applying decisions.
            neglect_days: Number of days without reference before considering
                          implicit neglect.
            archive_path: Path for archived memories.
        """
        self._store = memory_store
        self.decision_threshold = decision_threshold
        self.neglect_days = neglect_days
        self.archive_path = archive_path or ""

        self._pending_signals: List[FeedbackSignal] = []
        self._decision_log: List[FeedbackDecision] = []
        self._signal_counter: int = 0

    # ── Explicit Feedback ──────────────────────────────────────────────

    def register_explicit_correction(
        self,
        correction_text: str,
        target_memory_id: Optional[str] = None,
        target_entity: Optional[str] = None,
        target_attribute: Optional[str] = None,
        suggested_value: Optional[str] = None,
        confidence: float = 0.95,
        context: str = "",
    ) -> FeedbackSignal:
        """Register an explicit user correction.

        Example: "No, I actually prefer dark mode now."
        The user is directly stating that a memory is wrong.

        Args:
            correction_text: The user's correction statement.
            target_memory_id: ID of the memory being corrected (if known).
            target_entity: Entity the correction is about.
            target_attribute: Attribute being corrected.
            suggested_value: The corrected value.
            confidence: Confidence in this correction (default high for explicit).
            context: Additional context about the interaction.

        Returns:
            The registered FeedbackSignal.
        """
        self._signal_counter += 1
        signal = FeedbackSignal(
            signal_id=f"explicit_{self._signal_counter}_{int(time.time())}",
            signal_type=FeedbackType.EXPLICIT_CORRECTION,
            target_memory_id=target_memory_id,
            target_entity=target_entity,
            target_attribute=target_attribute,
            suggested_value=suggested_value or correction_text,
            confidence=confidence,
            context=context or correction_text,
        )
        self._pending_signals.append(signal)
        logger.info(
            "Explicit correction registered: entity=%s, attr=%s -> %s",
            target_entity, target_attribute, suggested_value,
        )
        return signal

    # ── Implicit Feedback Detection ────────────────────────────────────

    def detect_implicit_feedback(
        self,
        recent_interactions: List[str],
        current_preferences: Optional[Dict[str, Any]] = None,
        previous_preferences: Optional[Dict[str, Any]] = None,
    ) -> List[FeedbackSignal]:
        """Detect implicit feedback from recent interactions.

        Detection strategies:
          1. Preference override: user's behavior contradicts old preferences.
          2. Dissatisfaction patterns: negative sentiment, retries, corrections.
          3. Neglect: preferences not referenced for a long time.

        Args:
            recent_interactions: List of recent user interaction texts.
            current_preferences: Current known preferences.
            previous_preferences: Previous snapshot for comparison.

        Returns:
            List of detected implicit FeedbackSignal objects.
        """
        signals: List[FeedbackSignal] = []

        if not recent_interactions:
            return signals

        # ── Strategy 1: Preference Override Detection ──
        if current_preferences and previous_preferences:
            signals.extend(
                self._detect_preference_overrides(
                    current_preferences, previous_preferences
                )
            )

        # ── Strategy 2: Dissatisfaction Pattern Detection ──
        signals.extend(self._detect_dissatisfaction(recent_interactions))

        # ── Strategy 3: Neglect Detection ──
        if current_preferences:
            signals.extend(self._detect_neglect(current_preferences))

        self._pending_signals.extend(signals)
        logger.info("Detected %d implicit feedback signals", len(signals))
        return signals

    def _detect_preference_overrides(
        self,
        current: Dict[str, Any],
        previous: Dict[str, Any],
    ) -> List[FeedbackSignal]:
        """Detect when current preferences contradict previous ones."""
        signals: List[FeedbackSignal] = []

        for key in current:
            if key in previous and current[key] != previous[key]:
                self._signal_counter += 1
                signals.append(FeedbackSignal(
                    signal_id=f"implicit_override_{self._signal_counter}_{int(time.time())}",
                    signal_type=FeedbackType.IMPLICIT_OVERRIDE,
                    target_memory_id=None,
                    target_entity="user",
                    target_attribute=key,
                    suggested_value=str(current[key]),
                    confidence=0.7,
                    context=f"Old value '{previous[key]}' → New value '{current[key]}'",
                ))

        return signals

    def _detect_dissatisfaction(
        self,
        interactions: List[str],
    ) -> List[FeedbackSignal]:
        """Detect dissatisfaction signals from interaction text.

        Looks for patterns like:
          - "no, that's wrong"
          - "I don't like this"
          - "change it to ..."
          - "not what I meant"
        """
        signals: List[FeedbackSignal] = []

        dissatisfaction_markers = [
            "不对", "错了", "不是这样", "搞错了", "不喜欢", "不满意",
            "换成", "改成", "应该用", "实际上", "重新来",
            "wrong", "incorrect", "don't like", "change to",
            "not what I", "actually", "redo", "rephrase",
        ]

        for interaction in interactions:
            interaction_lower = interaction.lower()
            for marker in dissatisfaction_markers:
                if marker in interaction_lower:
                    self._signal_counter += 1
                    signals.append(FeedbackSignal(
                        signal_id=f"implicit_dissat_{self._signal_counter}_{int(time.time())}",
                        signal_type=FeedbackType.IMPLICIT_DISSATISFACTION,
                        target_memory_id=None,
                        target_entity=None,
                        target_attribute=None,
                        suggested_value=None,
                        confidence=0.55,
                        context=interaction[:200],
                    ))
                    break  # One signal per interaction

        return signals

    def _detect_neglect(
        self,
        preferences: Dict[str, Any],
    ) -> List[FeedbackSignal]:
        """Detect preferences that have been neglected (not referenced)."""
        signals: List[FeedbackSignal] = []
        now = time.time()
        threshold = self.neglect_days * 86400

        for key, val in preferences.items():
            if isinstance(val, dict) and "last_referenced" in val:
                last_ref = val["last_referenced"]
                if isinstance(last_ref, (int, float)) and now - last_ref > threshold:
                    self._signal_counter += 1
                    signals.append(FeedbackSignal(
                        signal_id=f"implicit_neglect_{self._signal_counter}_{int(time.time())}",
                        signal_type=FeedbackType.IMPLICIT_NEGLECT,
                        target_memory_id=val.get("memory_id"),
                        target_entity="user",
                        target_attribute=key,
                        suggested_value=None,
                        confidence=0.4,
                        context=f"Not referenced for {(now - last_ref) / 86400:.1f} days",
                    ))

        return signals

    # ── Reinforcement ──────────────────────────────────────────────────

    def register_reinforcement(
        self,
        target_memory_id: str,
        context: str = "",
        confidence: float = 0.8,
    ) -> FeedbackSignal:
        """Register a reinforcement signal (user confirms/repeats existing memory).

        Args:
            target_memory_id: ID of the memory being reinforced.
            context: Interaction context.
            confidence: How strong the reinforcement is.

        Returns:
            The registered FeedbackSignal.
        """
        self._signal_counter += 1
        signal = FeedbackSignal(
            signal_id=f"reinforce_{self._signal_counter}_{int(time.time())}",
            signal_type=FeedbackType.REINFORCEMENT,
            target_memory_id=target_memory_id,
            target_entity=None,
            target_attribute=None,
            suggested_value=None,
            confidence=confidence,
            context=context,
        )
        self._pending_signals.append(signal)
        return signal

    # ── Decision Engine ────────────────────────────────────────────────

    def decide_and_apply(self) -> List[FeedbackDecision]:
        """Process pending signals, make decisions, and apply to memory store.

        Decision logic:
          - EXPLICIT_CORRECTION (conf > 0.8) → UPDATE or ADD
          - IMPLICIT_OVERRIDE (conf > 0.6) → UPDATE with archive old
          - IMPLICIT_DISSATISFACTION (conf > 0.5) → ARCHIVE suspect memory
          - IMPLICIT_NEGLECT (conf > 0.3) → ARCHIVE
          - REINFORCEMENT → REINFORCE (boost weight)

        Returns:
            List of FeedbackDecision applied.
        """
        decisions: List[FeedbackDecision] = []
        to_process = self._pending_signals[:]
        self._pending_signals.clear()

        for signal in to_process:
            decision = self._make_decision(signal)
            if decision.action != MemoryAction.NOOP:
                self._apply_decision(decision)
            decisions.append(decision)
            self._decision_log.append(decision)

        logger.info(
            "Feedback loop processed %d signals: %s",
            len(decisions),
            {a.value: sum(1 for d in decisions if d.action == a)
             for a in MemoryAction},
        )
        return decisions

    def _make_decision(self, signal: FeedbackSignal) -> FeedbackDecision:
        """Determine what action to take for a given signal."""
        mem_id = signal.target_memory_id or self._resolve_memory_id(signal)
        threshold = self.decision_threshold

        if signal.signal_type == FeedbackType.EXPLICIT_CORRECTION:
            if signal.confidence >= threshold:
                if signal.target_memory_id:
                    return FeedbackDecision(
                        signal_id=signal.signal_id,
                        action=MemoryAction.UPDATE,
                        memory_id=signal.target_memory_id,
                        rationale=f"Explicit correction: {signal.suggested_value}",
                        details={"new_value": signal.suggested_value},
                    )
                else:
                    return FeedbackDecision(
                        signal_id=signal.signal_id,
                        action=MemoryAction.ADD,
                        memory_id=mem_id,
                        rationale=f"New memory from explicit correction: {signal.suggested_value}",
                        details={
                            "entity": signal.target_entity,
                            "attribute": signal.target_attribute,
                            "value": signal.suggested_value,
                        },
                    )

        elif signal.signal_type == FeedbackType.IMPLICIT_OVERRIDE:
            if signal.confidence >= threshold:
                return FeedbackDecision(
                    signal_id=signal.signal_id,
                    action=MemoryAction.UPDATE,
                    memory_id=mem_id,
                    rationale=f"Implicit override: {signal.context}",
                    details={
                        "attribute": signal.target_attribute,
                        "new_value": signal.suggested_value,
                        "archive_old": True,
                    },
                )
            else:
                return FeedbackDecision(
                    signal_id=signal.signal_id,
                    action=MemoryAction.ARCHIVE,
                    memory_id=mem_id,
                    rationale="Low-confidence implicit override; archiving old value.",
                )

        elif signal.signal_type == FeedbackType.IMPLICIT_DISSATISFACTION:
            return FeedbackDecision(
                signal_id=signal.signal_id,
                action=MemoryAction.ARCHIVE,
                memory_id=mem_id,
                rationale=f"Dissatisfaction detected in: {signal.context[:100]}",
            )

        elif signal.signal_type == FeedbackType.IMPLICIT_NEGLECT:
            return FeedbackDecision(
                signal_id=signal.signal_id,
                action=MemoryAction.ARCHIVE,
                memory_id=mem_id,
                rationale=f"Neglected for too long: {signal.context}",
            )

        elif signal.signal_type == FeedbackType.REINFORCEMENT:
            return FeedbackDecision(
                signal_id=signal.signal_id,
                action=MemoryAction.REINFORCE,
                memory_id=mem_id,
                rationale=f"Reinforcement from context: {signal.context[:100]}",
                details={"boost_factor": 1.5},
            )

        return FeedbackDecision(
            signal_id=signal.signal_id,
            action=MemoryAction.NOOP,
            memory_id=mem_id,
            rationale="No applicable action for signal type.",
        )

    def _resolve_memory_id(self, signal: FeedbackSignal) -> str:
        """Resolve a memory ID from entity + attribute if not provided."""
        if signal.target_memory_id:
            return signal.target_memory_id
        entity = signal.target_entity or "unknown"
        attr = signal.target_attribute or "unknown"
        return f"mem_{entity}_{attr}_{int(time.time())}"

    def _apply_decision(self, decision: FeedbackDecision) -> None:
        """Apply the decision to the memory store."""
        if self._store is None:
            logger.warning("No memory store configured; decision not applied: %s",
                           decision.action.value)
            return

        try:
            if decision.action == MemoryAction.ADD:
                detail = decision.details
                self._store.ingest(
                    f"[{detail.get('entity', 'unknown')}] "
                    f"{detail.get('attribute', 'unknown')}: "
                    f"{detail.get('value', '')} "
                    f"(source: feedback_loop)"
                )

            elif decision.action == MemoryAction.UPDATE:
                mem_id = decision.memory_id
                new_val = decision.details.get("new_value", "")
                if hasattr(self._store, "update_memory"):
                    self._store.update_memory(mem_id, {"content": new_val})
                elif hasattr(self._store, "ingest"):
                    self._store.ingest(
                        f"[UPDATE {mem_id}] {new_val} (source: feedback_loop)"
                    )

            elif decision.action == MemoryAction.ARCHIVE:
                mem_id = decision.memory_id
                if hasattr(self._store, "archive_memory"):
                    self._store.archive_memory(mem_id)
                elif hasattr(self._store, "update_memory"):
                    self._store.update_memory(mem_id, {"active": False})

            elif decision.action == MemoryAction.DELETE:
                mem_id = decision.memory_id
                if hasattr(self._store, "delete_memory"):
                    self._store.delete_memory(mem_id)

            elif decision.action == MemoryAction.REINFORCE:
                mem_id = decision.memory_id
                boost = decision.details.get("boost_factor", 1.5)
                if hasattr(self._store, "reinforce_memory"):
                    self._store.reinforce_memory(mem_id, boost)

        except Exception as e:
            logger.error("Failed to apply decision %s: %s", decision.action.value, e)

    # ── Analysis & Reporting ───────────────────────────────────────────

    def get_decision_summary(self) -> Dict[str, Any]:
        """Return summary statistics of decisions made."""
        action_counts = defaultdict(int)
        for d in self._decision_log:
            action_counts[d.action.value] += 1

        return {
            "total_decisions": len(self._decision_log),
            "action_distribution": dict(action_counts),
            "pending_signals": len(self._pending_signals),
        }

    def export_decision_log(self, path: str) -> str:
        """Export decision log to JSON file."""
        log_data = []
        for d in self._decision_log[-1000:]:  # last 1000
            log_data.append({
                "signal_id": d.signal_id,
                "action": d.action.value,
                "memory_id": d.memory_id,
                "rationale": d.rationale,
                "details": d.details,
            })

        os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
        logger.info("Decision log exported to %s (%d entries)", path, len(log_data))
        return path

    def clear_pending(self) -> int:
        """Clear pending signals without processing. Returns count cleared."""
        count = len(self._pending_signals)
        self._pending_signals.clear()
        return count
