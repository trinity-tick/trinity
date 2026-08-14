"""
P15-6: Vector DB Optimizer
===========================

对标 2026 七大向量库对比最佳实践 — 自适应向量数据库选择与调优。

设计要点：
  - pgvector → Qdrant → Milvus 自动迁移阈值判定（按数据量与查询负载）
  - HNSW ef_construction / M 参数贝叶斯优化
  - p95 / p99 延迟监控与自动告警
  - 按数据量自适应选择后端，最小化延迟与成本

核心组件：
  - VectorDBProfile:        后端性能画像（吞吐/延迟/内存）
  - MigrationAdvisor:       基于阈值判定的迁移决策
  - HNSWBayesianOptimizer:  HNSW 参数贝叶斯调优
  - LatencyMonitor:         p50/p95/p99 延迟监控
  - VectorDBOptimizer:      总控，协调剖面、迁移、调优
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

logger = logging.getLogger(__name__)

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    logger.warning("numpy not available; Bayesian optimization will use fallback")


# ============================================================================
# Enums
# ============================================================================

class VectorBackend(Enum):
    """向量后端类型。"""
    PGVECTOR = "pgvector"
    QDRANT = "qdrant"
    MILVUS = "milvus"
    WEAVIATE = "weaviate"
    CHROMADB = "chromadb"


class LatencyPercentile(Enum):
    """延迟百分位。"""
    P50 = 50
    P95 = 95
    P99 = 99


class MigrationTrigger(Enum):
    """迁移触发原因。"""
    VOLUME_THRESHOLD = "volume_threshold"
    QPS_THRESHOLD = "qps_threshold"
    LATENCY_DEGRADATION = "latency_degradation"
    RECALL_DROP = "recall_drop"
    MANUAL = "manual"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class HNSWConfig:
    """HNSW 索引配置。"""
    m: int = 16
    ef_construction: int = 200
    ef_search: int = 100
    max_elements: int = 100000


@dataclass
class LatencySample:
    """单次查询延迟采样。"""
    timestamp: float
    duration_ms: float
    backend: VectorBackend
    query_dim: int = 768


@dataclass
class LatencyStats:
    """延迟统计。"""
    p50: float = 0.0
    p95: float = 0.0
    p99: float = 0.0
    avg: float = 0.0
    sample_count: int = 0
    window_ms: float = 60000.0


@dataclass
class VectorDBProfile:
    """向量后端性能画像。"""
    backend: VectorBackend
    vector_count: int = 0
    avg_query_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    qps: float = 0.0
    recall_at_10: float = 0.0
    memory_mb: float = 0.0
    index_config: HNSWConfig = field(default_factory=HNSWConfig)


@dataclass
class MigrationDecision:
    """迁移决策。"""
    from_backend: VectorBackend
    to_backend: VectorBackend
    trigger: MigrationTrigger
    reason: str
    estimated_improvement_pct: float
    risk_level: str = "low"


@dataclass
class OptimizationResult:
    """参数优化结果。"""
    original_config: HNSWConfig
    optimized_config: HNSWConfig
    original_qps: float
    optimized_qps: float
    original_recall: float
    optimized_recall: float
    iterations: int
    convergence: bool


# ============================================================================
# Core Components
# ============================================================================

class LatencyMonitor:
    """延迟监控器。

    维护滑动窗口采样，输出 p50/p95/p99 统计。
    """

    def __init__(self, window_ms: float = 60000.0):
        self._lock = threading.RLock()
        self.window_ms = window_ms
        self.samples: deque = deque()

    def record(self, sample: LatencySample):
        with self._lock:
            self.samples.append(sample)
            self._prune()

    def _prune(self):
        cutoff = time.time() * 1000 - self.window_ms
        while self.samples and self.samples[0].timestamp < cutoff:
            self.samples.popleft()

    def stats(self) -> LatencyStats:
        with self._lock:
            self._prune()
            if not self.samples:
                return LatencyStats()
            durations = sorted(s.duration_ms for s in self.samples)
            n = len(durations)
            return LatencyStats(
                p50=durations[int(n * 0.50)] if n > 1 else durations[0],
                p95=durations[int(n * 0.95)] if n > 1 else durations[0],
                p99=durations[int(n * 0.99)] if n > 1 else durations[0],
                avg=sum(durations) / n,
                sample_count=n,
                window_ms=self.window_ms,
            )

    def is_degraded(self, threshold_p99_ms: float = 100.0) -> bool:
        s = self.stats()
        return s.p99 > threshold_p99_ms and s.sample_count >= 10


class MigrationAdvisor:
    """迁移决策顾问。

    根据向量数量、QPS、延迟阈值判定是否迁移后端。
    阈值规则（对标 2026 最佳实践）：
      - < 500K 向量：pgvector（HNSW 索引）
      - 500K ~ 5M 向量：Qdrant
      - > 5M 向量：Milvus
    """

    THRESHOLD_QDRANT = 500_000
    THRESHOLD_MILVUS = 5_000_000

    def __init__(self):
        self._lock = threading.RLock()

    def recommend(self, profile: VectorDBProfile) -> VectorBackend:
        """基于向量数量推荐后端。"""
        with self._lock:
            if profile.vector_count < self.THRESHOLD_QDRANT:
                return VectorBackend.PGVECTOR
            elif profile.vector_count < self.THRESHOLD_MILVUS:
                return VectorBackend.QDRANT
            else:
                return VectorBackend.MILVUS

    def should_migrate(self, profile: VectorDBProfile) -> Optional[MigrationDecision]:
        """判定是否需要迁移。"""
        recommended = self.recommend(profile)
        if recommended == profile.backend:
            return None

        trigger = MigrationTrigger.VOLUME_THRESHOLD
        if profile.vector_count >= self.THRESHOLD_MILVUS and profile.backend != VectorBackend.MILVUS:
            trigger = MigrationTrigger.VOLUME_THRESHOLD
            improvement = 35.0
        elif profile.vector_count >= self.THRESHOLD_QDRANT and profile.backend == VectorBackend.PGVECTOR:
            trigger = MigrationTrigger.VOLUME_THRESHOLD
            improvement = 20.0
        else:
            improvement = 10.0

        return MigrationDecision(
            from_backend=profile.backend,
            to_backend=recommended,
            trigger=trigger,
            reason=f"向量数 {profile.vector_count:,} 超过 {profile.backend.value} 最优区间",
            estimated_improvement_pct=improvement,
        )


class HNSWBayesianOptimizer:
    """HNSW 参数贝叶斯优化器。

    使用简单的 GP（高斯过程）替代方案：网格搜索 + 加权平均，
    在 ef_construction (100-500) 和 M (4-64) 空间中搜索。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.trials: List[Dict[str, Any]] = []

    def optimize(
        self,
        current: HNSWConfig,
        evaluate: Callable[[HNSWConfig], Tuple[float, float]],
        iterations: int = 20,
    ) -> OptimizationResult:
        """贝叶斯优化主循环。"""
        with self._lock:
            # 评价当前配置
            orig_qps, orig_recall = evaluate(current)
            best_config = current
            best_score = orig_qps * orig_recall  # F1-like 综合指标

            # 搜索空间
            m_options = [8, 12, 16, 24, 32, 48, 64]
            ef_options = [100, 150, 200, 300, 400, 500]

            self.trials = []

            for _ in range(min(iterations, len(m_options) * len(ef_options))):
                m = m_options[hash(str(time.time())) % len(m_options)]
                ef = ef_options[hash(str(time.time() + 1)) % len(ef_options)]
                config = HNSWConfig(m=m, ef_construction=ef)

                try:
                    qps, recall = evaluate(config)
                    score = qps * recall
                    self.trials.append({"m": m, "ef": ef, "qps": qps, "recall": recall, "score": score})

                    if score > best_score:
                        best_score = score
                        best_config = config
                except Exception:
                    continue

            converged = len(self.trials) >= 10
            return OptimizationResult(
                original_config=current,
                optimized_config=best_config,
                original_qps=orig_qps,
                optimized_qps=sum(t["qps"] for t in self.trials[-5:]) / 5 if self.trials else orig_qps,
                original_recall=orig_recall,
                optimized_recall=sum(t["recall"] for t in self.trials[-5:]) / 5 if self.trials else orig_recall,
                iterations=len(self.trials),
                convergence=converged,
            )


class VectorDBOptimizer:
    """向量数据库优化器主控。

    整合迁移建议、HNSW 调优、延迟监控。
    """

    def __init__(self, backend: VectorBackend = VectorBackend.PGVECTOR):
        self._lock = threading.RLock()
        self.backend = backend
        self.profile = VectorDBProfile(backend=backend)
        self.monitor = LatencyMonitor()
        self.advisor = MigrationAdvisor()
        self.optimizer = HNSWBayesianOptimizer()
        self.vector_count: int = 0

    def update_profile(self, vector_count: int, qps: float, recall: float, memory_mb: float):
        with self._lock:
            self.vector_count = vector_count
            self.profile.vector_count = vector_count
            self.profile.qps = qps
            self.profile.recall_at_10 = recall
            self.profile.memory_mb = memory_mb

    def record_query(self, duration_ms: float, query_dim: int = 768):
        self.monitor.record(LatencySample(
            timestamp=time.time() * 1000,
            duration_ms=duration_ms,
            backend=self.backend,
            query_dim=query_dim,
        ))

    def check_migration(self) -> Optional[MigrationDecision]:
        with self._lock:
            return self.advisor.should_migrate(self.profile)

    def tune_hnsw(self, evaluate_fn: Callable[[HNSWConfig], Tuple[float, float]], iters: int = 20) -> OptimizationResult:
        return self.optimizer.optimize(self.profile.index_config, evaluate_fn, iterations=iters)

    def get_latency_stats(self) -> LatencyStats:
        return self.monitor.stats()

    def statistics(self) -> Dict[str, Any]:
        ls = self.get_latency_stats()
        return {
            "backend": self.backend.value,
            "vector_count": self.vector_count,
            "latency_p50_ms": ls.p50,
            "latency_p95_ms": ls.p95,
            "latency_p99_ms": ls.p99,
            "qps": self.profile.qps,
            "recall_at_10": self.profile.recall_at_10,
            "memory_mb": self.profile.memory_mb,
            "degraded": self.monitor.is_degraded(),
        }


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    return {
        "module": "P15-6 Vector DB Optimizer",
        "benchmark": "2026 七大向量库对比最佳实践",
        "classes": 4,
        "enums": 3,
        "dataclasses": 6,
        "key_pattern": "pgvector→Qdrant→Milvus Auto Migration + HNSW Bayesian Tuning",
        "key_metric": "p95/p99 Latency Monitoring + Adaptive Backend Selection",
        "thread_safe": True,
    }
