"""
PreferenceFeedbackMemory — PAHF Personalized Agents from Human Feedback
========================================================================
P40-2 · arXiv 2026

实现 PAHF 偏好反馈记忆: pre_action_clarification() 行动前询问模糊偏好,
post_action_correction() 行动后根据反馈修正记忆, detect_preference_drift() 检测
偏好漂移, 在线持续学习无需离线训练, per-user 独立记忆空间。

设计要点:
  - 双反馈通道: 事前澄清 + 事后修正
  - 偏好漂移检测: 滑动窗口 + 统计检验
  - UserMemorySpace: 每用户独立状态隔离
  - 完全在线: 无需离线训练, 实时交互学习
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums (重命名: FeedbackType→PAHFFeedbackType, 避免冲突)
# ---------------------------------------------------------------------------

class PAHFFeedbackType(Enum):
    """PAHF 反馈类型。"""
    IMPLICIT = auto()            # 隐式反馈 (行为观察)
    EXPLICIT_CORRECTION = auto() # 显式修正
    PREFERENCE_CLARIFICATION = auto()  # 偏好澄清
    RATING = auto()              # 评分反馈
    DEMONSTRATION = auto()       # 示范反馈


class DriftStatus(Enum):
    """偏好漂移状态。"""
    STABLE = auto()
    MILD_DRIFT = auto()
    SIGNIFICANT_DRIFT = auto()
    ABRUPT_CHANGE = auto()


class ClarificationScope(Enum):
    """澄清范围。"""
    GOAL = auto()
    STYLE = auto()
    CONSTRAINT = auto()
    FORMAT = auto()
    PRIORITY = auto()


# ---------------------------------------------------------------------------
# Data Classes (重命名: FeedbackRecord→PAHFFeedbackRecord, CorrectionRecord→PAHFCorrectionRecord)
# ---------------------------------------------------------------------------

@dataclass
class PAHFFeedbackRecord:
    """一条 PAHF 反馈记录。"""
    record_id: str
    user_id: str
    feedback_type: PAHFFeedbackType
    context: str
    user_response: str
    confidence: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class PAHFCorrectionRecord:
    """一次修正记录——行动后的偏好更新。"""
    correction_id: str
    user_id: str
    action_description: str
    original_behavior: str
    corrected_behavior: str
    reason: str = ""
    applied: bool = False
    timestamp: float = field(default_factory=time.time)


@dataclass
class ClarificationRequest:
    """行动前的偏好澄清请求。"""
    request_id: str
    user_id: str
    scope: ClarificationScope
    question: str
    options: List[str] = field(default_factory=list)
    resolved: bool = False
    resolution: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class PreferenceProfile:
    """用户偏好画像——per-user 独立空间。"""
    user_id: str
    preferences: Dict[str, Any] = field(default_factory=dict)
    preference_history: deque = field(default_factory=lambda: deque(maxlen=100))
    drift_status: DriftStatus = DriftStatus.STABLE
    last_update: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# PreferenceDriftDetector
# ---------------------------------------------------------------------------

class PreferenceDriftDetector:
    """偏好漂移检测器。

    使用滑动窗口 + 余弦相似度比较历史偏好向量, 检测用户偏好的漂移。

    Parameters
    ----------
    window_size : int
        滑动窗口大小。
    drift_threshold : float
        余弦相似度低于此值视为漂移。
    """

    def __init__(self, window_size: int = 20, drift_threshold: float = 0.85) -> None:
        self.window_size = window_size
        self.drift_threshold = drift_threshold
        self._embedding_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size * 2))
        self._lock = threading.RLock()

    def record_preference(self, user_id: str, embedding: List[float]) -> None:
        """记录一次偏好嵌入。"""
        with self._lock:
            self._embedding_history[user_id].append(np.array(embedding, dtype=np.float64))

    def detect_preference_drift(self, user_id: str) -> Tuple[DriftStatus, float]:
        """检测偏好漂移。

        Returns
        -------
        Tuple[DriftStatus, float]
            (漂移状态, 最近窗口间余弦相似度)。
        """
        with self._lock:
            history = list(self._embedding_history[user_id])
            if len(history) < self.window_size:
                return DriftStatus.STABLE, 1.0

            # 对比前半窗口与后半窗口
            mid = len(history) // 2
            early = np.mean(history[:mid], axis=0)
            recent = np.mean(history[mid:], axis=0)

            sim = float(_cosine_similarity(early, recent))

            if sim >= self.drift_threshold:
                return DriftStatus.STABLE, sim
            elif sim >= self.drift_threshold - 0.1:
                return DriftStatus.MILD_DRIFT, sim
            elif sim >= self.drift_threshold - 0.25:
                return DriftStatus.SIGNIFICANT_DRIFT, sim
            return DriftStatus.ABRUPT_CHANGE, sim


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0.0 or nb == 0.0:
        return 1.0
    return float(np.dot(a, b) / (na * nb))


# ---------------------------------------------------------------------------
# PreferenceFeedbackMemory
# ---------------------------------------------------------------------------

class PreferenceFeedbackMemory:
    """PAHF 偏好反馈记忆系统。

    per-user 独立记忆空间, 双反馈通道, 在线持续学习。

    Parameters
    ----------
    embedding_dim : int
        偏好嵌入维度。
    history_capacity : int
        每用户历史容量。
    """

    def __init__(self, embedding_dim: int = 64, history_capacity: int = 500) -> None:
        self.embedding_dim = embedding_dim
        self.history_capacity = history_capacity
        self._drift_detector = PreferenceDriftDetector()
        self._profiles: Dict[str, PreferenceProfile] = {}
        self._feedbacks: Dict[str, deque] = defaultdict(lambda: deque(maxlen=history_capacity))
        self._corrections: Dict[str, deque] = defaultdict(lambda: deque(maxlen=history_capacity))
        self._pending_clarifications: Dict[str, ClarificationRequest] = {}
        self._lock = threading.RLock()
        self._action_count: int = 0

        logger.info("PreferenceFeedbackMemory initialized [dim=%d cap=%d]", embedding_dim, history_capacity)

    # ------------------------------------------------------------------
    # Per-User Memory Space
    # ------------------------------------------------------------------

    def get_user_profile(self, user_id: str) -> PreferenceProfile:
        """获取用户偏好画像——per-user 独立空间。"""
        with self._lock:
            if user_id not in self._profiles:
                self._profiles[user_id] = PreferenceProfile(user_id=user_id)
            return self._profiles[user_id]

    # ------------------------------------------------------------------
    # Pre-Action Clarification
    # ------------------------------------------------------------------

    def pre_action_clarification(
        self,
        user_id: str,
        action_description: str,
        scopes: Optional[List[ClarificationScope]] = None,
    ) -> Optional[ClarificationRequest]:
        """行动前询问模糊偏好。

        Parameters
        ----------
        user_id : str
            用户标识。
        action_description : str
            即将执行的操作描述。
        scopes : Optional[List[ClarificationScope]]
            需要澄清的维度。

        Returns
        -------
        Optional[ClarificationRequest]
            澄清请求; None 表示无需澄清。
        """
        with self._lock:
            profile = self.get_user_profile(user_id)

            # 检查是否存在模糊点
            scopes = scopes or [ClarificationScope.GOAL, ClarificationScope.STYLE]
            ambiguous_scopes = []

            for scope in scopes:
                key = scope.name.lower()
                if key not in profile.preferences or profile.preferences[key] is None:
                    ambiguous_scopes.append(scope)

            if not ambiguous_scopes:
                # 检查漂移: 最近有显著变化则重新确认
                drift_status, _ = self.detect_preference_drift(user_id)
                if drift_status in (DriftStatus.SIGNIFICANT_DRIFT, DriftStatus.ABRUPT_CHANGE):
                    ambiguous_scopes = scopes[:1]  # 仅确认最关键维度

            if not ambiguous_scopes:
                return None

            req = ClarificationRequest(
                request_id=f"clar_{user_id}_{int(time.time()*1e6)}",
                user_id=user_id,
                scope=ambiguous_scopes[0],
                question=f"Regarding '{action_description}', please clarify your {ambiguous_scopes[0].name.lower()}",
            )
            self._pending_clarifications[req.request_id] = req
            logger.info("Clarification requested: %s %s", req.request_id, ambiguous_scopes[0].name)
            return req

    def resolve_clarification(self, request_id: str, resolution: str) -> bool:
        """解决澄清请求——用户提供回答。"""
        with self._lock:
            req = self._pending_clarifications.get(request_id)
            if req is None:
                return False

            req.resolved = True
            req.resolution = resolution
            profile = self.get_user_profile(req.user_id)
            profile.preferences[req.scope.name.lower()] = resolution
            profile.preference_history.append({
                "scope": req.scope.name,
                "value": resolution,
                "timestamp": time.time(),
            })
            profile.last_update = time.time()
            del self._pending_clarifications[request_id]
            logger.info("Clarification resolved: %s → %s", request_id, resolution[:40])
            return True

    # ------------------------------------------------------------------
    # Post-Action Correction
    # ------------------------------------------------------------------

    def post_action_correction(
        self,
        user_id: str,
        action_description: str,
        original_behavior: str,
        user_feedback: str,
        reason: str = "",
    ) -> PAHFCorrectionRecord:
        """行动后根据反馈修正记忆。

        Parameters
        ----------
        user_id : str
            用户标识。
        action_description : str
            执行的操作描述。
        original_behavior : str
            原始行为。
        user_feedback : str
            用户的修正反馈。
        reason : str
            用户给出的修正理由。

        Returns
        -------
        PAHFCorrectionRecord
            修正记录。
        """
        with self._lock:
            self._action_count += 1
            correction = PAHFCorrectionRecord(
                correction_id=f"corr_{self._action_count}_{user_id}",
                user_id=user_id,
                action_description=action_description,
                original_behavior=original_behavior,
                corrected_behavior=user_feedback,
                reason=reason,
                applied=True,
            )
            self._corrections[user_id].append(correction)

            # 更新偏好画像
            profile = self.get_user_profile(user_id)
            profile.preferences["last_correction"] = user_feedback
            profile.last_update = time.time()

            # 生成简单嵌入并记录
            embedding = _text_to_embedding(user_feedback, self.embedding_dim)
            self._drift_detector.record_preference(user_id, embedding)

            logger.info("Correction applied: %s → %s", original_behavior[:30], user_feedback[:30])
            return correction

    def record_feedback(
        self,
        user_id: str,
        feedback_type: PAHFFeedbackType,
        context: str,
        user_response: str,
        confidence: float = 0.0,
    ) -> PAHFFeedbackRecord:
        """记录通用反馈。"""
        with self._lock:
            record = PAHFFeedbackRecord(
                record_id=f"fb_{int(time.time()*1e6)}_{user_id}",
                user_id=user_id,
                feedback_type=feedback_type,
                context=context,
                user_response=user_response,
                confidence=confidence,
            )
            self._feedbacks[user_id].append(record)
            return record

    # ------------------------------------------------------------------
    # Preference Drift Detection
    # ------------------------------------------------------------------

    def detect_preference_drift(self, user_id: str) -> Tuple[DriftStatus, float]:
        """检测用户偏好漂移。"""
        return self._drift_detector.detect_preference_drift(user_id)

    # ------------------------------------------------------------------
    # Retrieval
    # ------------------------------------------------------------------

    def get_recent_corrections(self, user_id: str, limit: int = 10) -> List[PAHFCorrectionRecord]:
        """获取用户最近的修正记录。"""
        corrs = list(self._corrections.get(user_id, deque()))
        return sorted(corrs, key=lambda c: c.timestamp, reverse=True)[:limit]

    def get_user_preferences(self, user_id: str) -> Dict[str, Any]:
        """获取用户当前偏好。"""
        return self.get_user_profile(user_id).preferences.copy()

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_feedbacks = sum(len(v) for v in self._feedbacks.values())
            total_corrections = sum(len(v) for v in self._corrections.values())
            pending = len(self._pending_clarifications)

            drift_summary = {}
            for uid in self._profiles:
                status, sim = self.detect_preference_drift(uid)
                drift_summary[uid] = {"status": status.name, "similarity": round(sim, 4)}

            return {
                "users": len(self._profiles),
                "total_feedbacks": total_feedbacks,
                "total_corrections": total_corrections,
                "pending_clarifications": pending,
                "drift_summary": drift_summary,
            }


def _text_to_embedding(text: str, dim: int) -> List[float]:
    """简单哈希嵌入——将文本映射为固定维度向量 (无 torch 依赖)。"""
    h = hashlib.sha256(text.encode()).digest()
    vec = []
    for i in range(dim):
        byte_idx = i % len(h)
        vec.append((h[byte_idx] / 255.0) * 2.0 - 1.0)
    # 归一化
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# required for module-level usage
import math
