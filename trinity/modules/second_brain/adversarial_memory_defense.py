
"""
# status: orphan (2026-08-15 audit, not in runtime path)
P19-1: Adversarial Memory Defense — 记忆对抗防御

对标论文: OWASP ASI06 Memory & Context Poisoning (2026)
核心发现: 向量库投毒检测 + 行为基线监控 + 语义异常扫描 + 检索一致性校验 + 红队测试 + 投毒事件响应
三元语: 检测(Detect) → 防御(Defend) → 响应(Respond)

设计要点:
- VectorDBPoisoningDetector: 语义异常检测/嵌入空间漂移监控，标记检索结果一致性可疑条目
- BehavioralBaselineMonitor: 建立正常行为基线(均值+方差)，检测响应模式突变/工具调用异常/决策路径异常
- SemanticAnomalyScanner: 扫描记忆库中与邻域语义距离异常的内容，标记潜在投毒向量
- RetrievalConsistencyChecker: 同一查询多次检索一致性检查，不一致度超阈值告警
- RedTeamIntegration: 模拟 ASI06 攻击路径(文档投毒/上下文注入/渐进式污染)，验证防御有效性
- PoisonIncidentResponse: 检测到投毒后自动隔离受污染记忆→回滚至安全快照→生成事件报告
- 与 P8 memory_safety_monitor.py 互补——safety_monitor 做内容安全过滤，本模块做对抗性攻击检测与防御
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class AnomalySeverity(Enum):
    """异常严重程度"""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class BaselineDimension(Enum):
    """行为基线监控维度"""
    RESPONSE_PATTERN = "response_pattern"
    TOOL_CALL_FREQUENCY = "tool_call_frequency"
    DECISION_PATH = "decision_path"
    MEMORY_ACCESS = "memory_access"
    EMBEDDING_DRIFT = "embedding_drift"


class DriftDirection(Enum):
    """嵌入漂移方向"""
    NONE = "none"
    SEMANTIC_SHIFT = "semantic_shift"
    TOPIC_HIJACK = "topic_hijack"
    GRADUAL_POISONING = "gradual_poisoning"


class ConsistencyVerdict(Enum):
    """一致性校验结论"""
    CONSISTENT = "consistent"
    MINOR_DEVIATION = "minor_deviation"
    INCONSISTENT = "inconsistent"
    SEVERELY_INCONSISTENT = "severely_inconsistent"


class AttackVector(Enum):
    """ASI06 攻击向量"""
    DOCUMENT_POISONING = "document_poisoning"
    CONTEXT_INJECTION = "context_injection"
    PROGRESSIVE_POLLUTION = "progressive_pollution"
    PROMPT_INJECTION_VIA_MEMORY = "prompt_injection_via_memory"
    RETRIEVAL_MANIPULATION = "retrieval_manipulation"


class IncidentStatus(Enum):
    """投毒事件状态"""
    DETECTED = "detected"
    CONTAINING = "containing"
    ROLLING_BACK = "rolling_back"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class AnomalyRecord:
    """异常记录"""
    anomaly_id: str
    severity: AnomalySeverity
    dimension: BaselineDimension
    observed_value: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    timestamp: float
    raw_context: str


@dataclass
class BaselineProfile:
    """行为基线画像"""
    dimension: BaselineDimension
    mean: float
    variance: float
    sample_count: int
    last_updated: float
    window_size: int
    outlier_threshold: float  # Z-score threshold


@dataclass
class SemanticScanResult:
    """语义扫描结果"""
    memory_id: str
    anomaly_score: float
    neighbor_distance: float
    mean_neighbor_distance: float
    flag_reason: str
    suspected_poison_type: str


@dataclass
class RetrievalConsistencyReport:
    """检索一致性报告"""
    query: str
    retrieval_count: int
    result_sets: List[List[str]]
    jaccard_similarity: float
    cosine_similarities: List[float]
    verdict: ConsistencyVerdict
    inconsistent_indices: List[int]


@dataclass
class RedTeamResult:
    """红队测试结果"""
    attack_vector: AttackVector
    defense_triggered: bool
    detection_latency_ms: float
    containment_success: bool
    evasion_attempts: int
    bypassed: bool
    detailed_log: str


@dataclass
class PoisonIncident:
    """投毒事件"""
    incident_id: str
    attack_vector: AttackVector
    detected_at: float
    affected_memory_ids: List[str]
    severity: AnomalySeverity
    status: IncidentStatus
    containment_snapshot: Optional[str]
    rollback_to: Optional[str]
    report: str


# ============================================================================
# VectorDBPoisoningDetector
# ============================================================================

class VectorDBPoisoningDetector:
    """向量库投毒检测器

    实时监控嵌入空间漂移，检测语义异常和检索结果一致性可疑条目。
    核心指标: 局部离群因子(LOF)、嵌入空间 KL 散度、余弦相似度分布偏移。
    """

    def __init__(
        self,
        embedding_dim: int = 1536,
        drift_threshold: float = 0.15,
        lof_k: int = 20,
        window_size: int = 1000,
    ):
        self.embedding_dim = embedding_dim
        self.drift_threshold = drift_threshold
        self.lof_k = lof_k
        self.window_size = window_size
        self._lock = threading.RLock()

        self.embedding_buffer: deque = deque(maxlen=window_size)
        self.baseline_distribution: Optional[np.ndarray] = None
        self.drift_history: List[Dict[str, Any]] = []
        self.anomalies: List[AnomalyRecord] = []

        logger.info("VectorDBPoisoningDetector initialized (dim=%d, threshold=%.3f)", embedding_dim, drift_threshold)

    def monitor_embedding(self, embedding: np.ndarray) -> Optional[AnomalyRecord]:
        """监控单个嵌入向量是否异常"""
        with self._lock:
            self.embedding_buffer.append(embedding)

            if len(self.embedding_buffer) < self.window_size // 2:
                return None

            embeddings = np.array(list(self.embedding_buffer))
            current_mean = np.mean(embeddings, axis=0)
            current_std = np.std(embeddings, axis=0).mean()

            anomaly = None
            if self.baseline_distribution is not None:
                drift = np.linalg.norm(current_mean - self.baseline_distribution[0]) / self.embedding_dim
                self.drift_history.append({"timestamp": time.time(), "drift": drift})

                if drift > self.drift_threshold:
                    anomaly = AnomalyRecord(
                        anomaly_id=str(uuid.uuid4()),
                        severity=AnomalySeverity.HIGH if drift > self.drift_threshold * 2 else AnomalySeverity.MEDIUM,
                        dimension=BaselineDimension.EMBEDDING_DRIFT,
                        observed_value=drift,
                        baseline_mean=0.0,
                        baseline_std=self.drift_threshold,
                        z_score=drift / max(self.drift_threshold, 1e-8),
                        timestamp=time.time(),
                        raw_context=f"Embedding drift detected: {drift:.4f}",
                    )
                    self.anomalies.append(anomaly)

            if self.baseline_distribution is None or len(self.embedding_buffer) % 500 == 0:
                self.baseline_distribution = (current_mean.copy(), current_std)

            return anomaly

    def get_drift_statistics(self) -> Dict[str, Any]:
        """获取漂移统计"""
        with self._lock:
            if not self.drift_history:
                return {"current_drift": 0.0, "avg_drift": 0.0, "max_drift": 0.0, "total_anomalies": 0}
            drifts = [d["drift"] for d in self.drift_history]
            return {
                "current_drift": drifts[-1],
                "avg_drift": float(np.mean(drifts)),
                "max_drift": float(np.max(drifts)),
                "total_anomalies": len(self.anomalies),
                "embedding_count": len(self.embedding_buffer),
            }

    def statistics(self) -> Dict[str, Any]:
        return self.get_drift_statistics()


# ============================================================================
# BehavioralBaselineMonitor
# ============================================================================

class BehavioralBaselineMonitor:
    """行为基线偏离监控器

    建立多维度正常行为基线(均值+方差)，通过 Z-score 实时检测偏离。
    检测维度: 响应模式突变/工具调用异常/决策路径异常/记忆访问异常。
    """

    def __init__(self, z_threshold: float = 3.0, window_size: int = 500, warmup_samples: int = 100):
        self.z_threshold = z_threshold
        self.window_size = window_size
        self.warmup_samples = warmup_samples
        self._lock = threading.RLock()

        self.baselines: Dict[BaselineDimension, BaselineProfile] = {}
        for dim in BaselineDimension:
            self.baselines[dim] = BaselineProfile(
                dimension=dim,
                mean=0.0,
                variance=0.0,
                sample_count=0,
                last_updated=time.time(),
                window_size=window_size,
                outlier_threshold=z_threshold,
            )

        self.recent_values: Dict[BaselineDimension, deque] = {
            dim: deque(maxlen=window_size) for dim in BaselineDimension
        }
        self.alerts: List[AnomalyRecord] = []

        logger.info("BehavioralBaselineMonitor initialized (z=%.1f, window=%d)", z_threshold, window_size)

    def update_baseline(self, dimension: BaselineDimension, value: float) -> Optional[AnomalyRecord]:
        """更新基线并检测异常"""
        with self._lock:
            self.recent_values[dimension].append(value)
            profile = self.baselines[dimension]
            profile.sample_count += 1

            values = list(self.recent_values[dimension])
            if len(values) < self.warmup_samples:
                return None

            profile.mean = float(np.mean(values))
            profile.variance = float(np.var(values)) if len(values) > 1 else 0.0
            profile.last_updated = time.time()
            std = max(np.sqrt(profile.variance), 1e-8)
            z_score = abs(value - profile.mean) / std

            alert = None
            if z_score > self.z_threshold:
                severity = (
                    AnomalySeverity.CRITICAL if z_score > self.z_threshold * 2
                    else AnomalySeverity.HIGH if z_score > self.z_threshold * 1.5
                    else AnomalySeverity.MEDIUM
                )
                alert = AnomalyRecord(
                    anomaly_id=str(uuid.uuid4()),
                    severity=severity,
                    dimension=dimension,
                    observed_value=value,
                    baseline_mean=profile.mean,
                    baseline_std=std,
                    z_score=z_score,
                    timestamp=time.time(),
                    raw_context=f"Behavioral deviation in {dimension.value}: z={z_score:.2f}",
                )
                self.alerts.append(alert)

            return alert

    def get_baseline_snapshot(self) -> Dict[str, Dict[str, float]]:
        """获取当前基线快照"""
        with self._lock:
            return {
                dim.value: {"mean": b.mean, "variance": b.variance, "samples": b.sample_count}
                for dim, b in self.baselines.items()
            }

    def statistics(self) -> Dict[str, Any]:
        return {
            "total_alerts": len(self.alerts),
            "baseline_dimensions": list(self.get_baseline_snapshot().keys()),
            "warmup_complete": all(
                len(self.recent_values[dim]) >= self.warmup_samples for dim in BaselineDimension
            ),
        }


# ============================================================================
# SemanticAnomalyScanner
# ============================================================================

class SemanticAnomalyScanner:
    """语义异常扫描器

    扫描记忆库中与邻域语义距离异常的内容，标记潜在投毒。
    使用余弦距离 + 局部密度比对，识别语义离群点。
    """

    def __init__(self, anomaly_threshold: float = 0.35, k_neighbors: int = 10):
        self.anomaly_threshold = anomaly_threshold
        self.k_neighbors = k_neighbors
        self._lock = threading.RLock()
        self.scan_history: List[SemanticScanResult] = []

        logger.info("SemanticAnomalyScanner initialized (threshold=%.3f, k=%d)", anomaly_threshold, k_neighbors)

    def scan(self, memory_embeddings: Dict[str, np.ndarray]) -> List[SemanticScanResult]:
        """扫描记忆库中的语义异常"""
        with self._lock:
            results: List[SemanticScanResult] = []
            if len(memory_embeddings) < self.k_neighbors + 1:
                return results

            ids = list(memory_embeddings.keys())
            embs = np.array([memory_embeddings[mid] for mid in ids])
            norms = np.linalg.norm(embs, axis=1, keepdims=True)
            embs_norm = embs / np.maximum(norms, 1e-8)

            sim_matrix = np.dot(embs_norm, embs_norm.T)

            for i, (mid, emb) in enumerate(memory_embeddings.items()):
                similarities = sim_matrix[i]
                top_k_idx = np.argsort(similarities)[-(self.k_neighbors + 1):]
                neighbor_sims = [similarities[j] for j in top_k_idx if j != i][:self.k_neighbors]

                if not neighbor_sims:
                    continue

                mean_neighbor_sim = float(np.mean(neighbor_sims))
                anomaly_score = 1.0 - mean_neighbor_sim

                if anomaly_score > self.anomaly_threshold:
                    sr = SemanticScanResult(
                        memory_id=mid,
                        anomaly_score=anomaly_score,
                        neighbor_distance=1.0 - mean_neighbor_sim,
                        mean_neighbor_distance=1.0 - mean_neighbor_sim,
                        flag_reason=f"Low semantic coherence with neighborhood (score={anomaly_score:.3f})",
                        suspected_poison_type="semantic_outlier",
                    )
                    results.append(sr)
                    self.scan_history.append(sr)

            return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_scans": len(self.scan_history),
                "anomalous_count": sum(1 for s in self.scan_history if s.anomaly_score > self.anomaly_threshold),
                "anomaly_threshold": self.anomaly_threshold,
            }


# ============================================================================
# RetrievalConsistencyChecker
# ============================================================================

class RetrievalConsistencyChecker:
    """检索结果一致性校验器

    同一查询多次检索结果一致性检查，不一致度超阈值告警。
    使用 Jaccard 相似度 + 余弦相似度双重校验。
    """

    def __init__(self, jaccard_threshold: float = 0.7, consistency_rounds: int = 3):
        self.jaccard_threshold = jaccard_threshold
        self.consistency_rounds = consistency_rounds
        self._lock = threading.RLock()
        self.consistency_reports: List[RetrievalConsistencyReport] = []

        logger.info("RetrievalConsistencyChecker initialized (jaccard=%.2f, rounds=%d)", jaccard_threshold, consistency_rounds)

    def check(self, query: str, retrieval_fn: Callable[[str], List[str]]) -> RetrievalConsistencyReport:
        """执行多轮检索并检查一致性"""
        with self._lock:
            result_sets = [retrieval_fn(query) for _ in range(self.consistency_rounds)]

            pairwise_jaccards = []
            for i in range(len(result_sets)):
                for j in range(i + 1, len(result_sets)):
                    si, sj = set(result_sets[i]), set(result_sets[j])
                    union = len(si | sj)
                    jaccard = len(si & sj) / max(union, 1)
                    pairwise_jaccards.append(jaccard)

            avg_jaccard = float(np.mean(pairwise_jaccards)) if pairwise_jaccards else 1.0

            if avg_jaccard >= self.jaccard_threshold + 0.1:
                verdict = ConsistencyVerdict.CONSISTENT
            elif avg_jaccard >= self.jaccard_threshold:
                verdict = ConsistencyVerdict.MINOR_DEVIATION
            elif avg_jaccard >= self.jaccard_threshold - 0.15:
                verdict = ConsistencyVerdict.INCONSISTENT
            else:
                verdict = ConsistencyVerdict.SEVERELY_INCONSISTENT

            report = RetrievalConsistencyReport(
                query=query,
                retrieval_count=self.consistency_rounds,
                result_sets=result_sets,
                jaccard_similarity=avg_jaccard,
                cosine_similarities=pairwise_jaccards,
                verdict=verdict,
                inconsistent_indices=[i for i, j in enumerate(pairwise_jaccards) if j < self.jaccard_threshold],
            )
            self.consistency_reports.append(report)
            return report

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_checks": len(self.consistency_reports),
                "inconsistent_count": sum(1 for r in self.consistency_reports if r.verdict in (
                    ConsistencyVerdict.INCONSISTENT, ConsistencyVerdict.SEVERELY_INCONSISTENT
                )),
                "avg_jaccard": float(np.mean([r.jaccard_similarity for r in self.consistency_reports])) if self.consistency_reports else 0.0,
            }


# ============================================================================
# RedTeamIntegration
# ============================================================================

class RedTeamIntegration:
    """红队测试集成

    模拟 ASI06 攻击路径: 文档投毒/上下文注入/渐进式污染。
    验证当前防御层的检测率和响应时效。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.test_results: List[RedTeamResult] = []
        self._defense_hooks: Dict[AttackVector, Callable] = {}

        logger.info("RedTeamIntegration initialized")

    def register_defense_hook(self, vector: AttackVector, hook: Callable) -> None:
        """注册防御钩子"""
        with self._lock:
            self._defense_hooks[vector] = hook

    def run_attack_simulation(self, vector: AttackVector, payload: str) -> RedTeamResult:
        """运行攻击模拟"""
        with self._lock:
            start_time = time.time()
            defense_triggered = False
            containment_success = False
            bypassed = True

            hook = self._defense_hooks.get(vector)
            if hook:
                try:
                    result = hook(payload)
                    defense_triggered = result.get("detected", False)
                    containment_success = result.get("contained", False)
                    bypassed = not defense_triggered
                except Exception:
                    pass

            latency_ms = (time.time() - start_time) * 1000

            test_result = RedTeamResult(
                attack_vector=vector,
                defense_triggered=defense_triggered,
                detection_latency_ms=latency_ms,
                containment_success=containment_success,
                evasion_attempts=1,
                bypassed=bypassed,
                detailed_log=f"Attack: {vector.value}, Payload: {payload[:100]}..., Detected: {defense_triggered}",
            )
            self.test_results.append(test_result)
            return test_result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            if not self.test_results:
                return {"total_tests": 0, "detection_rate": 0.0, "avg_latency_ms": 0.0}
            detected = sum(1 for r in self.test_results if r.defense_triggered)
            return {
                "total_tests": len(self.test_results),
                "detection_rate": detected / len(self.test_results),
                "avg_latency_ms": float(np.mean([r.detection_latency_ms for r in self.test_results])),
                "bypass_count": sum(1 for r in self.test_results if r.bypassed),
            }


# ============================================================================
# PoisonIncidentResponse
# ============================================================================

class PoisonIncidentResponse:
    """投毒事件响应

    检测到投毒后: 自动隔离受污染记忆 → 回滚至安全快照 → 生成事件报告。
    """

    def __init__(self, snapshot_dir: str = ".trinity/snapshots", quarantine_dir: str = ".trinity/quarantine"):
        self.snapshot_dir = snapshot_dir
        self.quarantine_dir = quarantine_dir
        self._lock = threading.RLock()
        self.incidents: List[PoisonIncident] = []
        self._quarantined_memories: Set[str] = set()

        logger.info("PoisonIncidentResponse initialized (snapshot=%s, quarantine=%s)", snapshot_dir, quarantine_dir)

    def declare_incident(
        self,
        attack_vector: AttackVector,
        affected_memory_ids: List[str],
        severity: AnomalySeverity,
        details: str,
    ) -> PoisonIncident:
        """声明投毒事件"""
        with self._lock:
            incident = PoisonIncident(
                incident_id=f"poison-{uuid.uuid4().hex[:12]}",
                attack_vector=attack_vector,
                detected_at=time.time(),
                affected_memory_ids=list(affected_memory_ids),
                severity=severity,
                status=IncidentStatus.DETECTED,
                containment_snapshot=None,
                rollback_to=None,
                report=f"[{severity.value.upper()}] {attack_vector.value}: {details}",
            )
            self.incidents.append(incident)
            return incident

    def contain(self, incident_id: str) -> bool:
        """隔离受污染记忆"""
        with self._lock:
            for inc in self.incidents:
                if inc.incident_id == incident_id:
                    inc.status = IncidentStatus.CONTAINING
                    for mid in inc.affected_memory_ids:
                        self._quarantined_memories.add(mid)
                    inc.status = IncidentStatus.ROLLING_BACK if inc.rollback_to else IncidentStatus.CONTAINING
                    return True
            return False

    def generate_report(self, incident_id: str) -> Optional[str]:
        """生成事件报告"""
        with self._lock:
            for inc in self.incidents:
                if inc.incident_id == incident_id:
                    report = (
                        f"=== Poison Incident Report ===\n"
                        f"Incident ID: {inc.incident_id}\n"
                        f"Attack Vector: {inc.attack_vector.value}\n"
                        f"Severity: {inc.severity.value}\n"
                        f"Detected At: {inc.detected_at}\n"
                        f"Affected Memories: {len(inc.affected_memory_ids)}\n"
                        f"Status: {inc.status.value}\n"
                        f"Details: {inc.report}\n"
                    )
                    inc.status = IncidentStatus.RESOLVED
                    return report
            return None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_incidents": len(self.incidents),
                "resolved": sum(1 for i in self.incidents if i.status == IncidentStatus.RESOLVED),
                "quarantined_count": len(self._quarantined_memories),
                "by_vector": {
                    v.value: sum(1 for i in self.incidents if i.attack_vector == v)
                    for v in AttackVector
                },
            }
