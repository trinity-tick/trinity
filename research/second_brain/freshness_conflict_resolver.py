"""
# status: orphan (2026-08-15 audit, not in runtime path)
Freshness Conflict Resolver — Deterministic Memory Conflict Resolution
======================================================================
arXiv 2606.13115 · P46-1

检测记忆写入中的新鲜度冲突（同一 subject 的新旧版本竞争），
基于时间戳+来源权威性+语义完整性三元组打分做确定性消解。

设计要点:
  - FreshnessConflictDetector: subject 维度冲突检测
  - FreshnessScoreCalculator: 衰减曲线 + 三元组打分
  - ConflictResolutionPolicy: latest-wins / authority-first / merge / quarantine
  - DeterministicConflictResolver: 路由策略并执行消解
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class ResolutionStrategy(Enum):
    LATEST_WINS = auto()
    AUTHORITY_FIRST = auto()
    MERGE = auto()
    QUARANTINE = auto()


class ConflictSeverity(Enum):
    MINOR = auto()    # 同源, 微小差异
    MODERATE = auto() # 不同源, 可合并
    CRITICAL = auto() # 不同源, 矛盾不可调和


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class FreshnessConflictRecord:
    """冲突记录——记录一次 subject 维度的新鲜度冲突详情。"""
    conflict_id: str
    subject: str
    incumbent: Dict[str, Any] = field(default_factory=dict)
    challenger: Dict[str, Any] = field(default_factory=dict)
    severity: ConflictSeverity = ConflictSeverity.MINOR
    resolution: Optional[ResolutionStrategy] = None
    resolved_at: float = 0.0
    winner: str = ""  # "incumbent" | "challenger" | "merged"
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# FreshnessScoreCalculator
# ---------------------------------------------------------------------------

class FreshnessScoreCalculator:
    """新鲜度分数计算器——基于时间戳+来源权威性+语义完整性的三元组打分。

    Parameters
    ----------
    half_life_seconds : float
        衰减半衰期, 默认 3600 (1小时)。
    """

    def __init__(self, half_life_seconds: float = 3600.0) -> None:
        self.half_life = half_life_seconds
        self._authority_registry: Dict[str, float] = {}
        self._lock = threading.RLock()

    def set_authority(self, source: str, score: float) -> None:
        """设置来源权威性分数 (0~1)。"""
        self._authority_registry[source] = max(0.0, min(1.0, score))

    def compute(
        self, timestamp: float, source: str, content_length: int,
        max_length: int = 1000,
    ) -> float:
        """计算新鲜度分数 (0~1)。

        score = w_t * time_decay + w_a * authority + w_s * semantic_completeness
        """
        with self._lock:
            now = time.time()
            age = max(0.0, now - timestamp)
            # 指数衰减
            time_score = 2.0 ** (-age / self.half_life)

            authority = self._authority_registry.get(source, 0.5)
            completeness = min(1.0, content_length / max_length)

            # 权重: 时间 0.4, 权威 0.35, 完整性 0.25
            score = 0.4 * time_score + 0.35 * authority + 0.25 * completeness
            return round(score, 4)

    def statistics(self) -> Dict[str, Any]:
        return {
            "half_life_seconds": self.half_life,
            "registered_sources": len(self._authority_registry),
        }


# ---------------------------------------------------------------------------
# FreshnessConflictDetector
# ---------------------------------------------------------------------------

class FreshnessConflictDetector:
    """新鲜度冲突检测器——检测同一 subject 的新旧版本竞争。

    依赖 FreshnessScoreCalculator 做双边打分后判断冲突级别。
    """

    def __init__(self, score_calculator: Optional[FreshnessScoreCalculator] = None) -> None:
        self.scorer = score_calculator or FreshnessScoreCalculator()
        self._subject_index: Dict[str, List[Dict[str, Any]]] = {}
        self._history: List[FreshnessConflictRecord] = []
        self._count: int = 0
        self._lock = threading.RLock()

    def detect(
        self, subject: str, content: Any, source: str, timestamp: Optional[float] = None,
    ) -> Optional[FreshnessConflictRecord]:
        """检测并记录冲突——如果有同 subject 的已有记录则视为冲突。"""
        with self._lock:
            ts = timestamp or time.time()
            self._count += 1

            existing = self._subject_index.get(subject, [])
            if not existing:
                self._subject_index.setdefault(subject, []).append({
                    "content": content, "source": source, "timestamp": ts,
                    "content_len": len(str(content)),
                })
                return None

            # 取最新的已有记录作为 incumbent
            incumbent = max(existing, key=lambda e: e["timestamp"])
            challenger = {"content": content, "source": source, "timestamp": ts}

            inc_score = self.scorer.compute(
                incumbent["timestamp"], incumbent["source"], incumbent["content_len"])
            chal_score = self.scorer.compute(
                ts, source, len(str(content)))

            # 判断严重度
            score_diff = abs(inc_score - chal_score)
            if incumbent["source"] == source and score_diff < 0.2:
                severity = ConflictSeverity.MINOR
            elif score_diff < 0.4:
                severity = ConflictSeverity.MODERATE
            else:
                severity = ConflictSeverity.CRITICAL

            record = FreshnessConflictRecord(
                conflict_id=f"fc_{self._count}_{int(time.time()*1e6)}",
                subject=subject,
                incumbent=incumbent,
                challenger=challenger,
                severity=severity,
            )
            self._history.append(record)
            return record

    def statistics(self) -> Dict[str, Any]:
        return {
            "subjects_tracked": len(self._subject_index),
            "conflicts_detected": len(self._history),
        }


# ---------------------------------------------------------------------------
# ConflictResolutionPolicy
# ---------------------------------------------------------------------------

class ConflictResolutionPolicy:
    """可配置的消解策略路由——按严重度/策略映射表选择策略。"""

    def __init__(self) -> None:
        self._policy_map: Dict[ConflictSeverity, ResolutionStrategy] = {
            ConflictSeverity.MINOR: ResolutionStrategy.LATEST_WINS,
            ConflictSeverity.MODERATE: ResolutionStrategy.MERGE,
            ConflictSeverity.CRITICAL: ResolutionStrategy.QUARANTINE,
        }
        self._override: Dict[str, ResolutionStrategy] = {}
        self._lock = threading.RLock()

    def set_policy(self, severity: ConflictSeverity, strategy: ResolutionStrategy) -> None:
        with self._lock:
            self._policy_map[severity] = strategy

    def set_authority_override(self, source: str, strategy: ResolutionStrategy) -> None:
        with self._lock:
            self._override[source] = strategy

    def resolve_strategy(self, record: FreshnessConflictRecord) -> ResolutionStrategy:
        """返回应使用的消解策略。"""
        with self._lock:
            # 来源级别 override 优先
            inc_src = record.incumbent.get("source", "")
            chal_src = record.challenger.get("source", "")
            for src in [inc_src, chal_src]:
                if src in self._override:
                    return self._override[src]
            return self._policy_map.get(record.severity, ResolutionStrategy.LATEST_WINS)

    def statistics(self) -> Dict[str, Any]:
        return {
            "policies": {s.name: v.name for s, v in self._policy_map.items()},
            "overrides": self._override,
        }


# ---------------------------------------------------------------------------
# DeterministicConflictResolver
# ---------------------------------------------------------------------------

class DeterministicConflictResolver:
    """确定性冲突消解器——执行三元组打分+策略路由+消解。

    四种策略:
      - LATEST_WINS: 取时间戳更近的
      - AUTHORITY_FIRST: 取来源权威性更高的
      - MERGE: 合并内容 (字符串拼接 / 结构化 merge)
      - QUARANTINE: 隔离到待审区
    """

    def __init__(self) -> None:
        self.policy = ConflictResolutionPolicy()
        self._quarantine: List[FreshnessConflictRecord] = []
        self._resolved: List[FreshnessConflictRecord] = []
        self._lock = threading.RLock()

    def resolve(self, record: FreshnessConflictRecord) -> FreshnessConflictRecord:
        """执行消解并返回更新后的记录。"""
        with self._lock:
            strategy = self.policy.resolve_strategy(record)

            if strategy == ResolutionStrategy.LATEST_WINS:
                record.winner = (
                    "challenger" if record.challenger["timestamp"] >= record.incumbent["timestamp"]
                    else "incumbent"
                )

            elif strategy == ResolutionStrategy.AUTHORITY_FIRST:
                inc_auth = record.incumbent.get("authority", 0.5)
                chal_auth = record.challenger.get("authority", 0.5)
                record.winner = "challenger" if chal_auth >= inc_auth else "incumbent"

            elif strategy == ResolutionStrategy.MERGE:
                merged = str(record.incumbent.get("content", "")) + "\n---\n" + str(record.challenger.get("content", ""))
                record.metadata["merged_content"] = merged
                record.winner = "merged"

            elif strategy == ResolutionStrategy.QUARANTINE:
                self._quarantine.append(record)
                record.winner = ""
                record.metadata["quarantined"] = True

            record.resolution = strategy
            record.resolved_at = time.time()
            self._resolved.append(record)
            return record

    def get_quarantine(self) -> List[FreshnessConflictRecord]:
        return list(self._quarantine)

    def statistics(self) -> Dict[str, Any]:
        return {
            "resolved_total": len(self._resolved),
            "quarantined": len(self._quarantine),
            "policy": self.policy.statistics(),
        }
