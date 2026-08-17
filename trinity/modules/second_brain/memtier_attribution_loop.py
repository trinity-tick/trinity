"""
# status: orphan (2026-08-15 audit, not in runtime path)
P25-2: MEMTIER Attribution Loop — 对标 MEMTIER 2026.05
三元语: Track → Attribute → Adapt → Consolidate
设计要点:
  - RetrievalSignal 为加权检索信号 dataclass，支持来源溯源
  - AttributionLoop 将工具执行结果反向归因到各 signal 并调权
  - PPOWeightAdapter 用简化 PPO 策略 clip 更新权重
  - ConsolidationDaemon 异步后台定期归档衰减旧信号
  - weighted_retrieve 按信号权重加权检索
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RetrievalSignal:
    """MEMTIER 加权检索信号 — 含溯源与置信度。"""

    name: str
    weight: float
    source: str
    last_updated: float = field(default_factory=time.time)
    attribution_count: int = 0
    confidence: float = 1.0


class AttributionLoop:
    """工具结果 → signal 反向归因，调整 signal 权重与置信度。

    每轮 track() 将工具执行结果按成功/失败归因到各 RetrievalSignal，
    signal.confidence 依据连续命中/未命中做指数滑动更新。
    """

    def __init__(self, learning_rate: float = 0.05) -> None:
        self._lr = learning_rate
        self._signals: dict[str, RetrievalSignal] = {}
        self._lock = threading.RLock()
        self._track_count = 0
        self._started_at = time.time()

    def register(self, signal: RetrievalSignal) -> None:
        """注册一条检索信号（若已存在则覆盖权重）。"""
        with self._lock:
            self._signals[signal.name] = signal

    def track(
        self, tool_outcome: dict, retrieval_signals: list[RetrievalSignal]
    ) -> None:
        """将 tool_outcome 反向归因到 retrieval_signals 并更新权重。

        tool_outcome 预期包含 {"success": bool, "score": float}。
        """
        success = tool_outcome.get("success", False)
        score = tool_outcome.get("score", 0.5)

        with self._lock:
            for sig in retrieval_signals:
                stored = self._signals.get(sig.name)
                if stored is None:
                    stored = sig
                    self._signals[sig.name] = stored
                delta = self._lr * (score - 0.5) * (1.0 if success else -0.5)
                stored.weight = max(0.0, min(1.0, stored.weight + delta))
                stored.attribution_count += 1
                stored.last_updated = time.time()
                hit_factor = 0.8 if success else -0.3
                stored.confidence = max(0.1, min(1.0, stored.confidence + self._lr * hit_factor))
            self._track_count += 1

    def get_signals(self) -> list[RetrievalSignal]:
        """返回当前全部已注册信号快照。"""
        with self._lock:
            return list(self._signals.values())

    def statistics(self) -> dict:
        with self._lock:
            return {
                "signal_count": len(self._signals),
                "track_count": self._track_count,
                "uptime_sec": time.time() - self._started_at,
            }


class PPOWeightAdapter:
    """简化 PPO 权重适配器 — clip 策略更新权重避免剧烈震荡。

    adapt() 接收 signals 列表与 reward_delta，逐 signal 做
    ratio-clip (ε=0.2) 更新。
    """

    def __init__(self, clip_epsilon: float = 0.2, lr: float = 0.01) -> None:
        self._clip_eps = clip_epsilon
        self._lr = lr
        self._prev_weights: dict[str, float] = {}
        self._lock = threading.RLock()

    def adapt(
        self, signals: list[RetrievalSignal], reward_delta: float
    ) -> list[RetrievalSignal]:
        """PPO-clip 策略更新各 signal 权重。"""
        with self._lock:
            for sig in signals:
                old_w = self._prev_weights.get(sig.name, sig.weight)
                ratio = sig.weight / max(old_w, 1e-8)
                clipped = max(1.0 - self._clip_eps, min(1.0 + self._clip_eps, ratio))
                update = self._lr * clipped * reward_delta
                sig.weight = max(0.0, min(1.0, sig.weight + update))
                self._prev_weights[sig.name] = sig.weight
            return signals


class ConsolidationDaemon:
    """后台 Consolidation Daemon — 定期归档/衰减旧信号。

    consolidate() 根据 retention_policy 对 signal.weight 做指数衰减，
    低于 min_weight_threshold 的信号标记为已过期可移除。
    """

    def __init__(
        self, decay_rate: float = 0.95, min_weight_threshold: float = 0.05
    ) -> None:
        self._decay = decay_rate
        self._min_weight = min_weight_threshold
        self._lock = threading.RLock()
        self._consolidation_count = 0

    def consolidate(
        self, signals: list[RetrievalSignal], retention_policy: Optional[dict] = None
    ) -> tuple[list[RetrievalSignal], list[RetrievalSignal]]:
        """归档：active 列表与 expired 列表分开返回。

        retention_policy 可含 {"decay_rate": float, "min_weight": float} 覆盖默认值。
        """
        decay = (retention_policy or {}).get("decay_rate", self._decay)
        min_w = (retention_policy or {}).get("min_weight", self._min_weight)

        with self._lock:
            active: list[RetrievalSignal] = []
            expired: list[RetrievalSignal] = []
            for sig in signals:
                sig.weight *= decay
                if sig.weight < min_w:
                    expired.append(sig)
                else:
                    active.append(sig)
            self._consolidation_count += 1
            return active, expired

    def statistics(self) -> dict:
        return {"consolidation_count": self._consolidation_count}


def weighted_retrieve(
    query: str,
    signal_weights: dict[str, float],
    top_k: int = 10,
) -> list[dict]:
    """按 signal_weights 加权检索 — 返回按权重排序的 top_k 结果。

    注：本实现为接口占位，实际检索依赖上层引擎注入。
    """
    sorted_signals = sorted(signal_weights.items(), key=lambda x: x[1], reverse=True)
    results: list[dict] = []
    for name, weight in sorted_signals[:top_k]:
        results.append({"name": name, "weight": weight, "query": query})
    return results
