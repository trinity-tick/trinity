"""
# status: orphan (2026-08-15 audit, not in runtime path)
Live-Evo — Online Evolution of Agentic Memory from Continuous Feedback
=======================================================================
arXiv 2602.02369 · P49-1

连续用户反馈在线进化记忆：收集隐式/显式反馈信号，基于反馈梯度
在线微调记忆策略而无需离线重训，配合三维质量评分与版本快照回滚。

设计要点:
  - FeedbackCollector: 隐式/显式用户反馈收集
  - OnlineEvolutionEngine: 反馈梯度在线策略微调
  - MemoryFitnessEvaluator: 三维质量（时效性/准确性/相关性）
  - EvolutionCheckpointManager: 版本快照与回滚
"""
from __future__ import annotations

import logging
import threading
import time
import copy
import json
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import defaultdict, deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums & Data Classes
# ---------------------------------------------------------------------------

class FeedbackType(Enum):
    CLICK = auto()
    ADOPT = auto()
    CORRECT = auto()
    LIKE = auto()
    DISLIKE = auto()
    IGNORE = auto()
    EXPLICIT = auto()


@dataclass
class FeedbackSignal:
    """单条用户反馈信号。"""
    signal_type: FeedbackType
    memory_key: str = ""
    weight: float = 1.0
    context: str = ""
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class EvolutionSnapshot:
    """进化版本快照。"""
    version: int
    policy_weights: Dict[str, float] = field(default_factory=dict)
    fitness_scores: Dict[str, float] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class FitnessReport:
    """记忆质量评估报告。"""
    timeliness: float = 0.0
    accuracy: float = 0.0
    relevance: float = 0.0
    overall: float = 0.0
    details: str = ""


# ---------------------------------------------------------------------------
# FeedbackCollector
# ---------------------------------------------------------------------------

class FeedbackCollector:
    """收集隐式/显式用户反馈信号。

    隐式: 点击、采纳（复制/粘贴）、忽略
    显式: 点赞/踩、纠正、直接评价
    """

    _IMPLICIT_WEIGHTS = {
        FeedbackType.CLICK: 0.3,
        FeedbackType.ADOPT: 0.8,
        FeedbackType.IGNORE: -0.1,
    }
    _EXPLICIT_WEIGHTS = {
        FeedbackType.LIKE: 1.0,
        FeedbackType.DISLIKE: -1.0,
        FeedbackType.CORRECT: 0.6,
        FeedbackType.EXPLICIT: 0.5,
    }

    def __init__(self, max_buffer: int = 1000) -> None:
        self._buffer: deque[FeedbackSignal] = deque(maxlen=max_buffer)
        self._lock = threading.RLock()

    def collect(self, signal: FeedbackSignal) -> None:
        with self._lock:
            if signal.weight == 1.0 and signal.signal_type in self._IMPLICIT_WEIGHTS:
                signal.weight = self._IMPLICIT_WEIGHTS[signal.signal_type]
            elif signal.weight == 1.0 and signal.signal_type in self._EXPLICIT_WEIGHTS:
                signal.weight = self._EXPLICIT_WEIGHTS[signal.signal_type]
            self._buffer.append(signal)

    def get_recent(self, n: int = 50, since: float = 0.0) -> List[FeedbackSignal]:
        with self._lock:
            items = list(self._buffer)
            if since > 0:
                items = [s for s in items if s.timestamp >= since]
            return items[-n:]

    def aggregate(self, window_seconds: float = 3600.0) -> Dict[str, float]:
        """聚合反馈窗口内统计。"""
        with self._lock:
            now = time.time()
            window_signals = [s for s in self._buffer if now - s.timestamp <= window_seconds]
            positive = sum(s.weight for s in window_signals if s.weight > 0)
            negative = sum(abs(s.weight) for s in window_signals if s.weight < 0)
            return {
                "positive": round(positive, 4),
                "negative": round(negative, 4),
                "count": len(window_signals),
                "net": round(positive - negative, 4),
            }

    def statistics(self) -> Dict[str, Any]:
        return {"buffer_size": len(self._buffer), "aggregate_1h": self.aggregate(3600)}


# ---------------------------------------------------------------------------
# MemoryFitnessEvaluator
# ---------------------------------------------------------------------------

class MemoryFitnessEvaluator:
    """记忆质量三维评分器——时效性、准确性、相关性。"""

    def __init__(self) -> None:
        self._history: List[FitnessReport] = []
        self._lock = threading.RLock()

    def evaluate(
        self, memory_content: str, feedback: List[FeedbackSignal],
        query: str = "", created_at: float = 0.0,
    ) -> FitnessReport:
        """综合三维评分。"""
        with self._lock:
            # 时效性
            age_hours = (time.time() - created_at) / 3600.0 if created_at > 0 else 0.0
            timeliness = max(0.0, 1.0 - age_hours / 720.0)  # 30天线性衰减

            # 准确性: 正向反馈占比
            if feedback:
                pos = sum(1 for f in feedback if f.weight > 0)
                accuracy = pos / len(feedback)
            else:
                accuracy = 0.5

            # 相关性: 基于 query 关键词匹配
            if query and memory_content:
                q_words = set(query.lower().split())
                m_words = set(memory_content.lower().split())
                overlap = len(q_words & m_words)
                relevance = min(1.0, overlap / max(len(q_words), 1))
            else:
                relevance = 0.5

            overall = 0.3 * timeliness + 0.35 * accuracy + 0.35 * relevance
            report = FitnessReport(
                timeliness=round(timeliness, 4),
                accuracy=round(accuracy, 4),
                relevance=round(relevance, 4),
                overall=round(overall, 4),
                details=f"age={age_hours:.1f}h feedback_n={len(feedback)}",
            )
            self._history.append(report)
            return report

    def statistics(self) -> Dict[str, Any]:
        return {"evaluations": len(self._history)}


# ---------------------------------------------------------------------------
# OnlineEvolutionEngine
# ---------------------------------------------------------------------------

class OnlineEvolutionEngine:
    """基于反馈梯度的在线记忆策略微调——无需离线重训。

    维护策略权重向量（键→权重），每条反馈信号作为一个梯度步骤。
    """

    def __init__(self, learning_rate: float = 0.05) -> None:
        self.learning_rate = learning_rate
        self._weights: Dict[str, float] = defaultdict(lambda: 0.5)
        self._update_count: int = 0
        self._lock = threading.RLock()

    def apply_feedback(self, signal: FeedbackSignal) -> Dict[str, Any]:
        """将一条反馈信号作为梯度更新策略权重。"""
        with self._lock:
            key = signal.memory_key or "default"
            old_weight = self._weights[key]
            # 权重更新: w ← w + lr * signal_weight * (1 - w)
            delta = self.learning_rate * signal.weight * (1.0 - old_weight)
            self._weights[key] = max(0.0, min(1.0, old_weight + delta))
            self._update_count += 1

            return {
                "key": key,
                "old_weight": round(old_weight, 4),
                "new_weight": round(self._weights[key], 4),
                "delta": round(delta, 4),
            }

    def batch_update(self, signals: List[FeedbackSignal]) -> Dict[str, Any]:
        """批量反馈更新。"""
        with self._lock:
            aggregated: Dict[str, float] = defaultdict(float)
            counts: Dict[str, int] = defaultdict(int)
            for s in signals:
                key = s.memory_key or "default"
                aggregated[key] += s.weight
                counts[key] += 1

            results = {}
            for key, total in aggregated.items():
                avg = total / counts[key]
                old = self._weights[key]
                delta = self.learning_rate * avg * (1.0 - old)
                self._weights[key] = max(0.0, min(1.0, old + delta))
                results[key] = {"old": round(old, 4), "new": round(self._weights[key], 4)}
                self._update_count += 1

            return {"results": results, "total_signals": len(signals)}

    def get_weights(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._weights)

    def statistics(self) -> Dict[str, Any]:
        return {"updates": self._update_count, "tracked_keys": len(self._weights)}


# ---------------------------------------------------------------------------
# EvolutionCheckpointManager
# ---------------------------------------------------------------------------

class EvolutionCheckpointManager:
    """进化版本快照与回滚管理——保留最近 N 个版本。"""

    def __init__(self, max_checkpoints: int = 10) -> None:
        self.max_checkpoints = max_checkpoints
        self._checkpoints: List[EvolutionSnapshot] = []
        self._current_version: int = 0
        self._lock = threading.RLock()

    def save(self, engine: OnlineEvolutionEngine, evaluator: Optional[MemoryFitnessEvaluator] = None) -> int:
        """保存当前策略版本快照。"""
        with self._lock:
            self._current_version += 1
            snapshot = EvolutionSnapshot(
                version=self._current_version,
                policy_weights=engine.get_weights(),
                fitness_scores={"evals": evaluator.statistics()["evaluations"]} if evaluator else {},
            )
            self._checkpoints.append(snapshot)
            if len(self._checkpoints) > self.max_checkpoints:
                self._checkpoints.pop(0)
            return self._current_version

    def rollback(self, engine: OnlineEvolutionEngine, version: Optional[int] = None) -> bool:
        """回滚到指定版本（默认上一个）。"""
        with self._lock:
            if not self._checkpoints:
                return False

            target = self._checkpoints[-1]
            if version is not None:
                for cp in self._checkpoints:
                    if cp.version == version:
                        target = cp
                        break
                else:
                    return False

            with engine._lock:
                engine._weights.clear()
                engine._weights.update(target.policy_weights)
            return True

    def list_versions(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{"version": c.version, "keys": len(c.policy_weights), "ts": c.timestamp} for c in self._checkpoints]

    def statistics(self) -> Dict[str, Any]:
        return {"versions": len(self._checkpoints), "current": self._current_version}
