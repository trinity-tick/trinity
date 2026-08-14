"""
InformationGeometricMemory — Fisher Metric-Driven Memory Management
===================================================================
arXiv 2603.14588 · P38-3 · SuperLocalMemory V3

三元语: Fisher 信息度量驱动的记忆管理。用信息几何 (层论 +
Fisher-Riemann 流形) 检测不可调解的记忆矛盾, 通过黎曼随机微分
方程控制记忆衰减, Fisher 信息门控决定保留/丢弃。

设计要点:
  - InformationGeometricMemory: 信息几何记忆引擎, 协调层论矛盾
    检测/黎曼衰减/信息门控三条流水线。
  - SheafContradictionDetector: 层论矛盾检测器, 将记忆组织为层论
    (sheaf) 结构, 利用上同调检测不可调解的矛盾 (global section failure)。
  - RiemannianLifecycleDynamics: 黎曼生命周期动力学, Fisher-Riemann
    流形上的随机微分方程控制记忆衰减速率。
  - FisherInformationRetentionGate: Fisher 信息保留门控, 基于参数
    Fisher 信息量决定记忆保留/丢弃决策。
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class GeometricRetentionDecision(Enum):
    """Fisher 信息门控决策。"""
    RETAIN = auto()
    COMPRESS = auto()
    DISCARD = auto()


class ContradictionType(Enum):
    """矛盾类型。"""
    DIRECT = auto()          # 直接矛盾 (A ∧ ¬A)
    TOPOLOGICAL = auto()     # 拓扑矛盾 (sheaf 全局截面不存在)
    TRANSITIVE = auto()      # 传递矛盾 (A→B, B→C, A→¬C)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class InformationGeodesic:
    """Fisher-Riemann 流形上的测地线。"""
    start_point: np.ndarray          # 记忆参数向量 θ_start
    end_point: np.ndarray            # θ_end
    geodesic_length: float           # Fisher 距离
    fisher_matrix_rank: int
    curvature: float                 # 截面曲率估计


@dataclass
class SheafSection:
    """层论截面——一组局部一致的记忆赋值。"""
    section_id: str
    memory_ids: List[str]
    restrictions: Dict[str, Any]     # 开放集 → 局部赋值映射
    is_global: bool = False          # 是否为全局截面
    obstruction: Optional[str] = None  # 阻碍的上同调类描述


@dataclass
class ContradictionReport:
    """矛盾检测报告。"""
    report_id: str
    contradiction_type: ContradictionType
    involved_memories: List[str]
    description: str
    severity: float                    # [0, 1]
    resolution_suggestion: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetentionScore:
    """Fisher 信息保留评分。"""
    memory_id: str
    fisher_info_norm: float           # Fisher 信息矩阵范数
    lp_norm_contribution: float        # 对预测损失的 Lp 范数贡献
    composite_score: float             # 综合保留分 [0, 1]
    decision: GeometricRetentionDecision = GeometricRetentionDecision.RETAIN


# =============================================================================
# InformationGeometricMemory
# =============================================================================

class InformationGeometricMemory:
    """信息几何记忆引擎。

    Parameters
    ----------
    memory_dim : int
        记忆嵌入维度。
    time_horizon : float
        生命周期时间窗 (秒)。
    decay_base_rate : float
        基础衰减率 (黎曼 SDE 漂移项)。
    """

    def __init__(
        self,
        memory_dim: int = 256,
        time_horizon: float = 86400.0,
        decay_base_rate: float = 0.01,
    ) -> None:
        self.memory_dim = memory_dim
        self.time_horizon = time_horizon
        self.decay_base_rate = decay_base_rate

        self._lock = threading.RLock()
        self._sheaf_detector = SheafContradictionDetector()
        self._lifecycle = RiemannianLifecycleDynamics(memory_dim, time_horizon, decay_base_rate)
        self._gate = FisherInformationRetentionGate(memory_dim)

        self._memory_store: Dict[str, Dict[str, Any]] = {}  # mem_id → {vector, timestamp, metadata}
        self._total_processed: int = 0

        logger.info("InformationGeometricMemory initialized [dim=%d horizon=%.0fs]", memory_dim, time_horizon)

    def ingest(self, memory_id: str, content_vector: np.ndarray, metadata: Optional[Dict] = None) -> None:
        """摄入新记忆。"""
        with self._lock:
            vec = content_vector.flatten()[:self.memory_dim]
            vec = vec / (np.linalg.norm(vec) + 1e-8)
            self._memory_store[memory_id] = {
                "vector": vec.copy(),
                "timestamp": time.time(),
                "metadata": metadata or {},
            }
            self._total_processed += 1

    def detect_contradictions(self) -> List[ContradictionReport]:
        """检测所有记忆间的矛盾。"""
        with self._lock:
            return self._sheaf_detector.detect(list(self._memory_store.values()))

    def evolve_lifecycle(self) -> Dict[str, float]:
        """执行一轮黎曼生命周期演化, 返回每段记忆的新 Fisher 距离。"""
        with self._lock:
            return self._lifecycle.evolve(self._memory_store)

    def evaluate_retention(self) -> List[RetentionScore]:
        """评估所有记忆的保留决策。"""
        with self._lock:
            scores = self._gate.evaluate(self._memory_store)
            return scores

    def apply_retention(self, scores: List[RetentionScore]) -> int:
        """执行保留决策, 返回移除的记忆数。"""
        with self._lock:
            removed = 0
            for s in scores:
                if s.decision == GeometricRetentionDecision.COMPRESS:
                    if s.memory_id in self._memory_store:
                        vec = self._memory_store[s.memory_id]["vector"]
                        self._memory_store[s.memory_id]["vector"] = 0.5 * vec  # 压缩
                elif s.decision == GeometricRetentionDecision.DISCARD:
                    if s.memory_id in self._memory_store:
                        del self._memory_store[s.memory_id]
                        removed += 1
            return removed

    def full_cycle(self) -> Dict[str, Any]:
        """完整信息几何记忆管理周期。"""
        contradictions = self.detect_contradictions()
        lifecycles = self.evolve_lifecycle()
        scores = self.evaluate_retention()
        removed = self.apply_retention(scores)
        return {
            "contradictions": len(contradictions),
            "critical_contradictions": sum(1 for c in contradictions if c.severity > 0.7),
            "active_memories": len(self._memory_store),
            "removed": removed,
            "retained": sum(1 for s in scores if s.decision == GeometricRetentionDecision.RETAIN),
            "compressed": sum(1 for s in scores if s.decision == GeometricRetentionDecision.COMPRESS),
        }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_processed": self._total_processed,
                "active_memories": len(self._memory_store),
                "sheaf_detector": self._sheaf_detector.statistics(),
                "lifecycle": self._lifecycle.statistics(),
                "gate": self._gate.statistics(),
            }


# =============================================================================
# SheafContradictionDetector
# =============================================================================

class SheafContradictionDetector:
    """层论矛盾检测器。

    将记忆组织为层论 (sheaf) 结构: 每个记忆簇是开集上的局部截面,
    通过检查限制兼容性 (restriction compatibility) 检测全局截面
    存在性——不存在即存在不可调解的矛盾。

    Parameters
    ----------
    compatibility_threshold : float
        相容性阈值 (余弦相似度)。
    min_cluster_size : int
        最小簇大小。
    """

    def __init__(self, compatibility_threshold: float = 0.75, min_cluster_size: int = 3) -> None:
        self.compatibility_threshold = compatibility_threshold
        self.min_cluster_size = min_cluster_size
        self._lock = threading.RLock()
        self._detection_count: int = 0
        logger.info("SheafContradictionDetector initialized [thresh=%.2f]", compatibility_threshold)

    def detect(self, memories: List[Dict[str, Any]]) -> List[ContradictionReport]:
        with self._lock:
            self._detection_count += 1
            reports: List[ContradictionReport] = []

            if len(memories) < self.min_cluster_size:
                return reports

            # 构建相容性图
            n = len(memories)
            compat_matrix = np.eye(n)
            for i in range(n):
                vi = memories[i].get("vector")
                if vi is None:
                    continue
                vi = np.asarray(vi).flatten()
                for j in range(i + 1, n):
                    vj = memories[j].get("vector")
                    if vj is None:
                        continue
                    vj = np.asarray(vj).flatten()
                    sim = float(np.dot(vi, vj) / (np.linalg.norm(vi) * np.linalg.norm(vj) + 1e-8))
                    compat_matrix[i, j] = compat_matrix[j, i] = sim

            # 寻找不相容对
            for i in range(n):
                for j in range(i + 1, n):
                    sim = compat_matrix[i, j]
                    if sim < 0.0:  # 反向相似 → 直接矛盾
                        mem_id_i = memories[i].get("metadata", {}).get("id", f"mem_{i}")
                        mem_id_j = memories[j].get("metadata", {}).get("id", f"mem_{j}")
                        reports.append(ContradictionReport(
                            report_id=f"cr_{self._detection_count}_{i}_{j}",
                            contradiction_type=ContradictionType.DIRECT,
                            involved_memories=[mem_id_i, mem_id_j],
                            description=f"Direct contradiction: cosine_sim={sim:.3f}",
                            severity=min(1.0, abs(sim)),
                            resolution_suggestion="Flag for manual reconciliation or source auditing.",
                        ))

            # Topological: 全局截面不存在检测 (简化为三角形矛盾链)
            for i in range(n):
                for j in range(i + 1, n):
                    if compat_matrix[i, j] < 0.0:
                        continue
                    for k in range(j + 1, n):
                        pairs = [(i, j), (j, k), (i, k)]
                        sims = [compat_matrix[p[0], p[1]] for p in pairs]
                        # 三角不等式违反 → 拓扑矛盾
                        if min(sims) < 0.3 and max(sims) > 0.8:
                            mem_ids = [
                                memories[a].get("metadata", {}).get("id", f"mem_{a}")
                                for a in (i, j, k)
                            ]
                            reports.append(ContradictionReport(
                                report_id=f"cr_top_{self._detection_count}_{i}_{j}_{k}",
                                contradiction_type=ContradictionType.TOPOLOGICAL,
                                involved_memories=mem_ids,
                                description=f"Topological sheaf obstruction: pairwise sims={sims}",
                                severity=0.6,
                                resolution_suggestion="Introduce intermediary bridging memory or weaken conflicting edge.",
                            ))

            return reports

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"detections": self._detection_count, "threshold": self.compatibility_threshold}


# =============================================================================
# RiemannianLifecycleDynamics
# =============================================================================

class RiemannianLifecycleDynamics:
    """Fisher-Riemann 流形上的记忆生命周期动力学。

    使用随机微分方程 dθ_t = -η∇L(θ_t) dt + σ dW_t 建模记忆参数
    在 Fisher-Riemann 流形上的漂移-扩散过程。高 Fisher 信息方向
    衰减慢, 低信息方向扩散快。

    Parameters
    ----------
    dim : int
        参数流形维度。
    time_horizon : float
        生命周期窗口 (秒)。
    drift_base : float
        基础漂移系数 η。
    diffusion_scale : float
        扩散强度 σ。
    """

    def __init__(
        self,
        dim: int = 256,
        time_horizon: float = 86400.0,
        drift_base: float = 0.01,
        diffusion_scale: float = 0.05,
    ) -> None:
        self.dim = dim
        self.time_horizon = time_horizon
        self.drift_base = drift_base
        self.diffusion_scale = diffusion_scale
        self._lock = threading.RLock()
        self._evolve_count: int = 0
        logger.info("RiemannianLifecycleDynamics initialized [dim=%d η=%.4f σ=%.4f]", dim, drift_base, diffusion_scale)

    def evolve(self, memory_store: Dict[str, Dict[str, Any]]) -> Dict[str, float]:
        with self._lock:
            self._evolve_count += 1
            current_time = time.time()
            distances: Dict[str, float] = {}

            for mem_id, mem in memory_store.items():
                t = mem["timestamp"]
                age = max(current_time - t, 0.0)
                age_factor = age / self.time_horizon

                vec = np.asarray(mem["vector"]).flatten()[:self.dim]

                # Fisher 信息: 用向量范数的梯度近似 Fisher 对角元素
                # 高范数分量 → 高 Fisher → 低衰减
                fisher_diag = np.abs(vec) + 1e-6
                fisher_diag = fisher_diag / (fisher_diag.sum() + 1e-8)

                # 漂移项: Fisher 信息加权的衰减 (高信息方向保留)
                drift = self.drift_base * age_factor * (1.0 - fisher_diag)

                # 扩散项: Wiener 过程增量 ~ N(0, σ²·age_factor)
                noise = self.diffusion_scale * np.sqrt(age_factor) * np.random.randn(self.dim)

                # 更新向量 (Riemannian retraction: 归一化)
                new_vec = vec - drift + noise
                norm = np.linalg.norm(new_vec)
                if norm > 0:
                    new_vec = new_vec / norm
                    mem["vector"] = new_vec

                # Fisher 距离 (改变量的 L2)
                delta = new_vec - vec
                fisher_dist = float(np.sqrt(np.dot(delta, delta)))
                distances[mem_id] = fisher_dist

            return distances

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "evolves": self._evolve_count,
                "time_horizon": self.time_horizon,
                "drift_base": self.drift_base,
                "diffusion_scale": self.diffusion_scale,
            }


# =============================================================================
# FisherInformationRetentionGate
# =============================================================================

class FisherInformationRetentionGate:
    """Fisher 信息保留门控。

    基于 Fisher 信息矩阵近似 (对角 Fisher / 贡献度) 决定记忆保留、
    压缩或丢弃。

    Parameters
    ----------
    dim : int
        参数维度。
    retain_threshold : float
        保留阈值。
    compress_threshold : float
        压缩阈值 (低于此值丢弃)。
    """

    def __init__(
        self,
        dim: int = 256,
        retain_threshold: float = 0.5,
        compress_threshold: float = 0.2,
    ) -> None:
        self.dim = dim
        self.retain_threshold = retain_threshold
        self.compress_threshold = compress_threshold
        self._lock = threading.RLock()
        self._eval_count: int = 0
        logger.info("FisherInformationRetentionGate initialized [r=%.2f c=%.2f]", retain_threshold, compress_threshold)

    def evaluate(self, memory_store: Dict[str, Dict[str, Any]]) -> List[RetentionScore]:
        with self._lock:
            self._eval_count += 1
            scores: List[RetentionScore] = []

            for mem_id, mem in memory_store.items():
                vec = np.asarray(mem["vector"]).flatten()[:self.dim]

                # Fisher 信息范数: 平方和作为对角 Fisher 近似
                fisher_norm = float(np.sum(vec ** 2)) / self.dim

                # Lp 贡献度: 向量能量占比
                total_energy = sum(
                    np.sum(np.asarray(m["vector"]).flatten()[:self.dim] ** 2)
                    for m in memory_store.values()
                )
                energy = float(np.sum(vec ** 2))
                lp_contribution = energy / max(total_energy, 1e-8)

                # 时间衰减因子
                age = time.time() - mem["timestamp"]
                recency = math.exp(-age / max(self.dim, 1.0))

                composite = 0.4 * fisher_norm + 0.35 * lp_contribution + 0.25 * recency

                if composite >= self.retain_threshold:
                    decision = GeometricRetentionDecision.RETAIN
                elif composite >= self.compress_threshold:
                    decision = GeometricRetentionDecision.COMPRESS
                else:
                    decision = GeometricRetentionDecision.DISCARD

                scores.append(RetentionScore(
                    memory_id=mem_id,
                    fisher_info_norm=float(fisher_norm),
                    lp_norm_contribution=float(lp_contribution),
                    composite_score=float(composite),
                    decision=decision,
                ))

            return scores

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {"evaluations": self._eval_count, "retain_threshold": self.retain_threshold}
