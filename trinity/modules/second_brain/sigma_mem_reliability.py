"""
SigmaMem — Spectral Reliability Memory for Multi-Agent Systems
===============================================================
arXiv 2607.27958 · P46-4

在线可靠性记忆: 记录每个 peer agent 的历史能力证据。
Weyl 不等式保证每次事件更新的谱变化有界, 实现在线稳定适应。
可靠性加权投票/融合。

设计要点:
  - SigmaReliabilityMemory: 在线可靠性记忆主控
  - PeerCompetenceTracker: 按任务类型跟踪 peer 正确率与可靠性
  - WeylBoundSpectralUpdater: Weyl 有界谱更新
  - ReliabilityWeightedAggregator: 可靠性加权融合
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Sequence, Tuple
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class PeerEvidence:
    """Peer 能力证据——单条观察记录。"""
    peer_id: str
    task_type: str
    outcome: bool          # True = 正确, False = 错误
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReliabilitySpectrum:
    """可靠性谱——某个 peer 在特定任务类型上的可靠性分布。"""
    peer_id: str
    task_type: str
    total_attempts: int = 0
    correct_attempts: int = 0
    reliability_score: float = 0.5   # 0~1
    spectral_norm: float = 0.0       # 谱范数 (Weyl bound 跟踪)
    last_updated: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# WeylBoundSpectralUpdater
# ---------------------------------------------------------------------------

class WeylBoundSpectralUpdater:
    """谱更新器——利用 Weyl 不等式保证每次事件更新的谱变化有界。

    原理: |λ_i(A+ΔA) - λ_i(A)| ≤ ‖ΔA‖_2 (Weyl 不等式)
    通过限制 ΔA 的谱范数 ‖ΔA‖_2 ≤ η, 保证特征值变化有界。
    """

    def __init__(self, eta: float = 0.1, dim: int = 16) -> None:
        self.eta = eta          # 谱范数变化上界
        self.dim = dim
        # 可靠性矩阵 R ∈ ℝ^{dim×dim}, 初始化为单位矩阵
        self._R: np.ndarray = np.eye(dim, dtype=np.float32) * 0.5
        self._lock = threading.RLock()

    def update(self, outcome: bool, confidence: float = 1.0) -> float:
        """基于一次事件更新谱, 返回当前最大特征值 (主可靠性)。

        ΔA = α * vv^T, 其中 α = (±η * confidence) / ‖v‖^2
        """
        with self._lock:
            # 生成事件向量
            np.random.seed(int(time.time() * 1e6) % (2**31))
            v = np.random.randn(self.dim).astype(np.float32)
            v_norm_sq = float(np.dot(v, v))
            if v_norm_sq < 1e-8:
                return float(np.linalg.eigvalsh(self._R).max())

            # 变化幅度
            sign = 1.0 if outcome else -1.0
            alpha = sign * self.eta * confidence / v_norm_sq

            # ΔA = α * v v^T
            delta = alpha * np.outer(v, v)
            self._R += delta

            # 确保对称
            self._R = 0.5 * (self._R + self._R.T)

            # 计算最大特征值作为可靠性分数
            eigvals = np.linalg.eigvalsh(self._R)
            return float(np.clip(eigvals.max(), 0.0, 1.0))

    def get_spectral_norm(self) -> float:
        with self._lock:
            return float(np.linalg.norm(self._R, ord=2))

    def statistics(self) -> Dict[str, Any]:
        return {
            "eta": self.eta,
            "dim": self.dim,
            "spectral_norm": round(self.get_spectral_norm(), 6),
        }


# ---------------------------------------------------------------------------
# PeerCompetenceTracker
# ---------------------------------------------------------------------------

class PeerCompetenceTracker:
    """Peer 能力追踪器——按任务类型跟踪 peer 正确率与可靠性分数。

    每个 (peer_id, task_type) 组合维护一个 WeylBoundSpectralUpdater。
    """

    def __init__(self) -> None:
        self._spectra: Dict[Tuple[str, str], WeylBoundSpectralUpdater] = {}
        self._records: Dict[Tuple[str, str], List[PeerEvidence]] = {}
        self._lock = threading.RLock()

    def record(self, evidence: PeerEvidence) -> ReliabilitySpectrum:
        """记录一条能力证据并更新谱。"""
        with self._lock:
            key = (evidence.peer_id, evidence.task_type)
            if key not in self._spectra:
                self._spectra[key] = WeylBoundSpectralUpdater()
                self._records[key] = []

            self._records[key].append(evidence)

            updater = self._spectra[key]
            score = updater.update(evidence.outcome, evidence.confidence)

            records = self._records[key]
            total = len(records)
            correct = sum(1 for r in records if r.outcome)

            return ReliabilitySpectrum(
                peer_id=evidence.peer_id,
                task_type=evidence.task_type,
                total_attempts=total,
                correct_attempts=correct,
                reliability_score=round(score, 4),
                spectral_norm=updater.get_spectral_norm(),
            )

    def get_reliability(self, peer_id: str, task_type: str) -> Optional[ReliabilitySpectrum]:
        """获取特定 peer+task 的可靠性谱。"""
        key = (peer_id, task_type)
        if key not in self._records or not self._records[key]:
            return None

        updater = self._spectra[key]
        records = self._records[key]
        total = len(records)
        correct = sum(1 for r in records if r.outcome)

        return ReliabilitySpectrum(
            peer_id=peer_id, task_type=task_type,
            total_attempts=total, correct_attempts=correct,
            reliability_score=round(float(np.linalg.eigvalsh(updater._R).max()), 4),
            spectral_norm=updater.get_spectral_norm(),
        )

    def all_scores(self) -> Dict[str, float]:
        """返回所有 peer 的聚合可靠性分数 (所有任务类型平均)。"""
        scores: Dict[str, List[float]] = {}
        for (peer_id, _), updater in self._spectra.items():
            scores.setdefault(peer_id, []).append(
                float(np.clip(np.linalg.eigvalsh(updater._R).max(), 0.0, 1.0)))
        return {pid: round(float(np.mean(s)), 4) for pid, s in scores.items()}

    def statistics(self) -> Dict[str, Any]:
        return {
            "peers_tracked": len(set(k[0] for k in self._spectra)),
            "task_types": len(set(k[1] for k in self._spectra)),
            "total_records": sum(len(r) for r in self._records.values()),
        }


# ---------------------------------------------------------------------------
# ReliabilityWeightedAggregator
# ---------------------------------------------------------------------------

class ReliabilityWeightedAggregator:
    """可靠性加权融合器——高分 peer 意见权重更大。

    支持加权投票 (分类) 和加权平均 (数值)。
    """

    def __init__(self, min_weight: float = 0.01) -> None:
        self.min_weight = min_weight
        self._lock = threading.RLock()

    def weighted_vote(
        self, votes: Dict[str, str], tracker: PeerCompetenceTracker, task_type: str,
    ) -> Tuple[str, float]:
        """可靠性加权投票。

        Parameters
        ----------
        votes : Dict[str, str]
            {peer_id: vote_value}
        tracker : PeerCompetenceTracker
        task_type : str

        Returns
        -------
        Tuple[str, float]
            (胜出选项, 加权置信度)
        """
        with self._lock:
            tally: Dict[str, float] = {}
            for peer_id, vote in votes.items():
                spec = tracker.get_reliability(peer_id, task_type)
                weight = max(self.min_weight, spec.reliability_score if spec else 0.5)
                tally[vote] = tally.get(vote, 0) + weight

            if not tally:
                return ("", 0.0)

            winner = max(tally, key=tally.get)
            confidence = tally[winner] / sum(tally.values()) if sum(tally.values()) > 0 else 0.0
            return winner, round(confidence, 4)

    def weighted_average(
        self, values: Dict[str, float], tracker: PeerCompetenceTracker, task_type: str,
    ) -> float:
        """可靠性加权平均。"""
        with self._lock:
            weighted_sum = 0.0
            weight_sum = 0.0
            for peer_id, val in values.items():
                spec = tracker.get_reliability(peer_id, task_type)
                weight = max(self.min_weight, spec.reliability_score if spec else 0.5)
                weighted_sum += weight * val
                weight_sum += weight
            return round(weighted_sum / weight_sum, 4) if weight_sum > 0 else 0.0

    def statistics(self) -> Dict[str, Any]:
        return {"min_weight": self.min_weight}


# ---------------------------------------------------------------------------
# SigmaReliabilityMemory
# ---------------------------------------------------------------------------

class SigmaReliabilityMemory:
    """Sigma 在线可靠性记忆——记录每个 peer agent 的历史能力证据。

    组合 PeerCompetenceTracker + ReliabilityWeightedAggregator。
    """

    def __init__(self) -> None:
        self.tracker = PeerCompetenceTracker()
        self.aggregator = ReliabilityWeightedAggregator()
        self._lock = threading.RLock()

    def observe(self, peer_id: str, task_type: str, outcome: bool, confidence: float = 1.0) -> ReliabilitySpectrum:
        """观察一次 peer 行为并更新可靠性。"""
        evidence = PeerEvidence(peer_id=peer_id, task_type=task_type,
                                outcome=outcome, confidence=confidence)
        return self.tracker.record(evidence)

    def decide(
        self, votes: Dict[str, str], task_type: str,
    ) -> Tuple[str, float]:
        """可靠性加权投票决策。"""
        return self.aggregator.weighted_vote(votes, self.tracker, task_type)

    def statistics(self) -> Dict[str, Any]:
        return {
            "tracker": self.tracker.statistics(),
            "aggregator": self.aggregator.statistics(),
        }
