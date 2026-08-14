
"""
P18-3: Distributed Hive Mind — 分布式蜂群记忆

对标论文: Distributed Hive Mind (2026.02)
核心发现: 单 Agent 5000 轮 90.47% → 10 Agent 并行 2.4h (9x 加速)
三元语: 领域分片 → 交叉确证 → 并行学习 → 副本管理 → 检索质量监控 → 蜂群共识

设计要点:
- DomainPartitioner: 领域分片器——按领域（生物/历史/数学/代码等）自动分区知识，最小化跨分片查询
- CrossAgentFactVerifier: 跨 Agent 事实交叉确证——多个 Agent 各自检索后投票确认/矛盾/精炼
- ParallelLearningCoordinator: 并行学习协调器——10 个 Agent 并行学习，2.4h 完成 5000 轮（单 Agent 需 21.6h）
- ShardReplicationManager: 分片副本管理——事实跨 Agent 副本，单点故障自动切换
- RetrievalQualityMonitor: 分片检索质量监控——确保分片后整体检索质量不跌破单 Agent 90.47% 天花板
- HiveConsensusEngine: 蜂群共识引擎——当 Agent 间事实矛盾时，多数投票+置信度加权解决
- 与 P12 multi_agent_topology.py / P13 crdt_collaborative_memory.py 互补——topology 做路由，CRDT 做同步，本模块做水平分片与并行加速
"""

from __future__ import annotations

import hashlib
import logging
import math
import random
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums & Constants
# ============================================================================

class DomainCategory(Enum):
    """知识领域分类"""
    BIOLOGY = "biology"
    HISTORY = "history"
    MATHEMATICS = "mathematics"
    CODE = "code"
    LITERATURE = "literature"
    PHYSICS = "physics"
    CHEMISTRY = "chemistry"
    GEOGRAPHY = "geography"
    PHILOSOPHY = "philosophy"
    GENERAL = "general"


class FactVerdict(Enum):
    """事实判定"""
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    REFINED = "refined"
    INCONCLUSIVE = "inconclusive"
    OUT_OF_SCOPE = "out_of_scope"


class LearningPhase(Enum):
    """并行学习阶段"""
    INITIALIZING = "initializing"
    SHARDING = "sharding"
    PARALLEL_TRAINING = "parallel_training"
    CROSS_VALIDATION = "cross_validation"
    CONSENSUS = "consensus"
    COMPLETED = "completed"


class ShardHealth(Enum):
    """分片健康状态"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILING = "failing"
    RECOVERING = "recovering"


class ConsensusMode(Enum):
    """共识模式"""
    MAJORITY_VOTE = "majority_vote"
    CONFIDENCE_WEIGHTED = "confidence_weighted"
    UNANIMOUS = "unanimous"
    SUPERMAJORITY = "supermajority"       # 至少 2/3


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class DomainShard:
    """领域分片"""
    shard_id: str
    domain: DomainCategory
    knowledge_keys: List[str] = field(default_factory=list)  # 该分片的知识 key 列表
    agent_assigned: Optional[str] = None
    estimated_size: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class FactCheckResult:
    """Agent 事实检查结果"""
    agent_id: str
    fact_key: str
    verdict: FactVerdict
    confidence: float                    # [0,1]
    supporting_evidence: List[str] = field(default_factory=list)
    contradictory_evidence: List[str] = field(default_factory=list)
    refined_fact: Optional[str] = None
    timestamp: float = field(default_factory=time.time)
    retrieval_latency_ms: float = 0.0


@dataclass
class LearningProgress:
    """并行学习进度"""
    phase: LearningPhase = LearningPhase.INITIALIZING
    total_rounds: int = 0
    completed_rounds: int = 0
    active_agents: int = 0
    elapsed_seconds: float = 0.0
    estimated_remaining_seconds: float = 0.0
    speedup_ratio: float = 1.0
    started_at: float = field(default_factory=time.time)


@dataclass
class ShardReplica:
    """分片副本"""
    replica_id: str
    shard_id: str
    agent_host: str
    health: ShardHealth = ShardHealth.HEALTHY
    last_sync: float = field(default_factory=time.time)
    data_size: int = 0
    is_primary: bool = False


@dataclass
class QualitySnapshot:
    """检索质量快照"""
    timestamp: float = field(default_factory=time.time)
    overall_accuracy: float = 0.9047      # 目标 ≥ 90.47%
    per_shard_accuracy: Dict[str, float] = field(default_factory=dict)
    cross_shard_query_ratio: float = 0.0  # 跨分片查询比例
    latency_p50_ms: float = 0.0
    latency_p95_ms: float = 0.0


@dataclass
class ConsensusResult:
    """蜂群共识结果"""
    question_key: str
    mode: ConsensusMode
    verdict: FactVerdict
    final_confidence: float
    agent_votes: Dict[str, FactVerdict] = field(default_factory=dict)
    agent_confidences: Dict[str, float] = field(default_factory=dict)
    agreement_ratio: float = 0.0
    resolved_at: float = field(default_factory=time.time)


# ============================================================================
# P18-3-1: DomainPartitioner — 领域分片器
# ============================================================================

class DomainPartitioner:
    """按领域自动分区知识，最小化跨分片查询"""

    DOMAIN_KEYWORDS = {
        DomainCategory.BIOLOGY: ["cell", "gene", "protein", "evolution", "species", "DNA", "RNA"],
        DomainCategory.HISTORY: ["war", "empire", "century", "king", "revolution", "ancient"],
        DomainCategory.MATHEMATICS: ["theorem", "equation", "calculus", "algebra", "geometry"],
        DomainCategory.CODE: ["function", "class", "API", "algorithm", "compiler", "runtime"],
        DomainCategory.PHYSICS: ["force", "energy", "quantum", "gravity", "particle", "wave"],
        DomainCategory.CHEMISTRY: ["element", "reaction", "molecule", "bond", "catalyst"],
        DomainCategory.LITERATURE: ["novel", "poem", "author", "plot", "character", "metaphor"],
        DomainCategory.GEOGRAPHY: ["mountain", "river", "climate", "continent", "latitude"],
        DomainCategory.PHILOSOPHY: ["ethics", "metaphysics", "logic", "epistemology", "ontology"],
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._shards: Dict[str, DomainShard] = {}
        self._key_to_shard: Dict[str, str] = {}  # knowledge_key → shard_id

    def classify(self, knowledge_key: str, content: str = "") -> DomainCategory:
        """自动分类知识到领域"""
        content_lower = (knowledge_key + " " + content).lower()
        scores = {}
        for domain, keywords in self.DOMAIN_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in content_lower)
            scores[domain] = score
        if max(scores.values()) == 0:
            return DomainCategory.GENERAL
        return max(scores, key=scores.get)

    def partition(
        self,
        knowledge_keys: List[str],
        content_map: Optional[Dict[str, str]] = None,
    ) -> List[DomainShard]:
        """将一批知识分区到领域分片"""
        content_map = content_map or {}
        domain_buckets: Dict[DomainCategory, List[str]] = defaultdict(list)

        for key in knowledge_keys:
            content = content_map.get(key, "")
            domain = self.classify(key, content)
            domain_buckets[domain].append(key)

        shards: List[DomainShard] = []
        with self._lock:
            for domain, keys in domain_buckets.items():
                shard_id = f"shard:{domain.value}:{uuid.uuid4().hex[:8]}"
                shard = DomainShard(
                    shard_id=shard_id,
                    domain=domain,
                    knowledge_keys=keys,
                    estimated_size=len(keys),
                )
                self._shards[shard_id] = shard
                for key in keys:
                    self._key_to_shard[key] = shard_id
                shards.append(shard)

        logger.info(f"Partitioned {len(knowledge_keys)} keys → {len(shards)} shards")
        return shards

    def get_shard_for_key(self, key: str) -> Optional[DomainShard]:
        with self._lock:
            shard_id = self._key_to_shard.get(key)
            if shard_id:
                return self._shards.get(shard_id)
        return None

    def cross_shard_needed(self, query_domains: List[DomainCategory]) -> bool:
        """判断查询是否需要跨分片"""
        return len(set(query_domains)) > 1

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_shards": len(self._shards),
                "by_domain": {
                    d.value: sum(1 for s in self._shards.values() if s.domain == d)
                    for d in DomainCategory
                },
                "total_keys_mapped": len(self._key_to_shard),
                "avg_shard_size": (
                    sum(s.estimated_size for s in self._shards.values()) / len(self._shards)
                    if self._shards else 0
                ),
            }


# ============================================================================
# P18-3-2: CrossAgentFactVerifier — 跨 Agent 事实交叉确证
# ============================================================================

class CrossAgentFactVerifier:
    """多个 Agent 各自检索后投票确认/矛盾/精炼"""

    MIN_CONFIDENCE = 0.6

    def __init__(self, agent_ids: Optional[List[str]] = None):
        self._lock = threading.RLock()
        self._agent_ids = agent_ids or [f"agent-{i}" for i in range(1, 11)]
        self._results: Dict[str, Dict[str, FactCheckResult]] = defaultdict(dict)  # fact_key → {agent_id → result}
        self._verification_log: List[Tuple[str, str, FactVerdict, float]] = []

    def submit_check(self, agent_id: str, result: FactCheckResult):
        """Agent 提交事实检查结果"""
        with self._lock:
            self._results[result.fact_key][agent_id] = result
            self._verification_log.append((result.fact_key, agent_id, result.verdict, result.confidence))

    def cross_verify(self, fact_key: str) -> FactVerdict:
        """交叉确证某个事实"""
        with self._lock:
            agents_results = self._results.get(fact_key, {})
            if not agents_results:
                return FactVerdict.OUT_OF_SCOPE

            verdicts = [r.verdict for r in agents_results.values()]
            confidences = [r.confidence for r in agents_results.values()]

            # 统计各判定
            confirmed = sum(1 for v in verdicts if v == FactVerdict.CONFIRMED)
            contradicted = sum(1 for v in verdicts if v == FactVerdict.CONTRADICTED)
            refined = sum(1 for v in verdicts if v == FactVerdict.REFINED)
            total = len(verdicts)

            avg_conf = sum(confidences) / len(confidences) if confidences else 0.0

            if contradicted / total >= 0.5:
                return FactVerdict.CONTRADICTED
            if refined / total >= 0.5:
                return FactVerdict.REFINED
            if confirmed / total >= 0.5 and avg_conf >= self.MIN_CONFIDENCE:
                return FactVerdict.CONFIRMED
            return FactVerdict.INCONCLUSIVE

    def get_agent_result(self, fact_key: str, agent_id: str) -> Optional[FactCheckResult]:
        with self._lock:
            return self._results.get(fact_key, {}).get(agent_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            verdict_counts = defaultdict(int)
            for _, _, verdict, _ in self._verification_log:
                verdict_counts[verdict.value] += 1
            return {
                "total_facts_verified": len(self._results),
                "total_verifications": len(self._verification_log),
                "verdict_distribution": dict(verdict_counts),
                "agents_participated": len(set(a for _, a, _, _ in self._verification_log)),
            }


# ============================================================================
# P18-3-3: ParallelLearningCoordinator — 并行学习协调器
# ============================================================================

class ParallelLearningCoordinator:
    """10 个 Agent 并行学习，2.4h 完成 5000 轮"""

    NUM_AGENTS = 10
    TOTAL_ROUNDS = 5000
    SINGLE_AGENT_HOURS = 21.6

    def __init__(self):
        self._lock = threading.RLock()
        self._progress = LearningProgress()
        self._agent_rounds: Dict[str, int] = defaultdict(int)  # agent → rounds completed
        self._agent_active: Dict[str, bool] = defaultdict(lambda: True)
        self._round_results: deque = deque(maxlen=1000)

    def start(self, rounds: int = TOTAL_ROUNDS):
        """启动并行学习"""
        with self._lock:
            self._progress = LearningProgress(
                phase=LearningPhase.SHARDING,
                total_rounds=rounds,
                active_agents=self.NUM_AGENTS,
            )
            for i in range(1, self.NUM_AGENTS + 1):
                self._agent_rounds[f"agent-{i}"] = 0
                self._agent_active[f"agent-{i}"] = True
            self._progress.phase = LearningPhase.PARALLEL_TRAINING
            logger.info(f"Parallel learning started: {rounds} rounds / {self.NUM_AGENTS} agents")

    def report_agent_progress(self, agent_id: str, rounds_completed: int, elapsed_seconds: float):
        """Agent 汇报学习进度"""
        with self._lock:
            self._agent_rounds[agent_id] = rounds_completed
            self._round_results.append((agent_id, rounds_completed, elapsed_seconds))

            # 汇总进度
            total_completed = sum(self._agent_rounds.values())
            self._progress.completed_rounds = min(total_completed, self._progress.total_rounds)
            self._progress.elapsed_seconds = max(
                self._progress.elapsed_seconds, elapsed_seconds
            )
            self._progress.active_agents = sum(1 for a in self._agent_active.values())

            # 估算剩余时间
            if self._progress.completed_rounds > 0:
                rate = self._progress.completed_rounds / max(self._progress.elapsed_seconds, 1e-6)
                remaining = self._progress.total_rounds - self._progress.completed_rounds
                self._progress.estimated_remaining_seconds = remaining / rate if rate > 0 else 0
                self._progress.speedup_ratio = (
                    self._progress.total_rounds / (rate * self.SINGLE_AGENT_HOURS * 3600)
                ) if rate > 0 else 1.0

            if self._progress.completed_rounds >= self._progress.total_rounds:
                self._progress.phase = LearningPhase.COMPLETED
                logger.info(f"Parallel learning completed: {self._progress.elapsed_seconds:.1f}s")

    def get_progress(self) -> LearningProgress:
        with self._lock:
            return self._progress

    def is_complete(self) -> bool:
        with self._lock:
            return self._progress.phase == LearningPhase.COMPLETED

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "phase": self._progress.phase.value,
                "total_rounds": self._progress.total_rounds,
                "completed_rounds": self._progress.completed_rounds,
                "active_agents": self._progress.active_agents,
                "elapsed_hours": self._progress.elapsed_seconds / 3600,
                "speedup": f"{self._progress.speedup_ratio:.1f}x",
                "per_agent_rounds": dict(self._agent_rounds),
                "target_hours": 2.4,
                "single_agent_hours": self.SINGLE_AGENT_HOURS,
            }


# ============================================================================
# P18-3-4: ShardReplicationManager — 分片副本管理
# ============================================================================

class ShardReplicationManager:
    """事实跨 Agent 副本，单点故障自动切换"""

    REPLICATION_FACTOR = 3               # 每个分片至少 3 个副本

    def __init__(self):
        self._lock = threading.RLock()
        self._replicas: Dict[str, ShardReplica] = {}          # replica_id → replica
        self._shard_replicas: Dict[str, List[str]] = defaultdict(list)  # shard_id → [replica_id]
        self._failover_log: List[Tuple[str, str, str, float]] = []  # (shard_id, from_agent, to_agent, timestamp)

    def create_replicas(self, shard_id: str, agent_hosts: List[str]) -> List[ShardReplica]:
        """为分片创建副本"""
        with self._lock:
            replicas = []
            for i, host in enumerate(agent_hosts[:self.REPLICATION_FACTOR]):
                replica_id = f"replica:{shard_id}:{host}:{uuid.uuid4().hex[:6]}"
                replica = ShardReplica(
                    replica_id=replica_id,
                    shard_id=shard_id,
                    agent_host=host,
                    is_primary=(i == 0),
                )
                self._replicas[replica_id] = replica
                self._shard_replicas[shard_id].append(replica_id)
                replicas.append(replica)
            logger.info(f"Created {len(replicas)} replicas for shard {shard_id}")
            return replicas

    def detect_failure(self, shard_id: str) -> Optional[ShardReplica]:
        """检测故障并返回可切换的副本"""
        with self._lock:
            replica_ids = self._shard_replicas.get(shard_id, [])
            for rid in replica_ids:
                replica = self._replicas.get(rid)
                if replica is None:
                    continue
                if replica.health == ShardHealth.HEALTHY and not replica.is_primary:
                    # 切换：将当前 primary 降级，选出新 primary
                    old_primary = next(
                        (self._replicas[r] for r in replica_ids
                         if r in self._replicas and self._replicas[r].is_primary),
                        None,
                    )
                    if old_primary and old_primary.health != ShardHealth.HEALTHY:
                        old_primary.is_primary = False
                        replica.is_primary = True
                        self._failover_log.append(
                            (shard_id, old_primary.agent_host, replica.agent_host, time.time())
                        )
                        logger.warning(
                            f"Failover: shard {shard_id} {old_primary.agent_host} → {replica.agent_host}"
                        )
                        return replica
            return None

    def mark_unhealthy(self, replica_id: str):
        with self._lock:
            replica = self._replicas.get(replica_id)
            if replica:
                replica.health = ShardHealth.FAILING

    def get_primary(self, shard_id: str) -> Optional[ShardReplica]:
        with self._lock:
            for rid in self._shard_replicas.get(shard_id, []):
                replica = self._replicas.get(rid)
                if replica and replica.is_primary and replica.health == ShardHealth.HEALTHY:
                    return replica
        return None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_replicas": len(self._replicas),
                "total_shards_with_replicas": len(self._shard_replicas),
                "replication_factor": self.REPLICATION_FACTOR,
                "healthy": sum(1 for r in self._replicas.values() if r.health == ShardHealth.HEALTHY),
                "failing": sum(1 for r in self._replicas.values() if r.health == ShardHealth.FAILING),
                "failover_count": len(self._failover_log),
            }


# ============================================================================
# P18-3-5: RetrievalQualityMonitor — 检索质量监控
# ============================================================================

class RetrievalQualityMonitor:
    """确保分片后整体检索质量不跌破单 Agent 90.47% 天花板"""

    BASELINE_ACCURACY = 0.9047            # 单 Agent 天花板
    ALERT_THRESHOLD = 0.85                # 低于此值触发告警

    def __init__(self):
        self._lock = threading.RLock()
        self._snapshots: List[QualitySnapshot] = []
        self._alerts: List[str] = []

    def snapshot(
        self,
        overall_accuracy: float,
        per_shard_accuracy: Dict[str, float],
        cross_shard_query_ratio: float = 0.0,
        latency_p50_ms: float = 0.0,
        latency_p95_ms: float = 0.0,
    ) -> QualitySnapshot:
        """记录检索质量快照"""
        snap = QualitySnapshot(
            overall_accuracy=overall_accuracy,
            per_shard_accuracy=per_shard_accuracy,
            cross_shard_query_ratio=cross_shard_query_ratio,
            latency_p50_ms=latency_p50_ms,
            latency_p95_ms=latency_p95_ms,
        )
        with self._lock:
            self._snapshots.append(snap)
            if overall_accuracy < self.ALERT_THRESHOLD:
                alert = f"Quality alert: accuracy {overall_accuracy:.2%} < threshold {self.ALERT_THRESHOLD:.2%}"
                self._alerts.append(alert)
                logger.warning(alert)
        return snap

    def is_below_baseline(self) -> bool:
        """检查是否跌破基线"""
        with self._lock:
            if not self._snapshots:
                return False
            latest = self._snapshots[-1]
            return latest.overall_accuracy < self.BASELINE_ACCURACY

    def trend(self) -> List[float]:
        """返回准确率趋势"""
        with self._lock:
            return [s.overall_accuracy for s in self._snapshots[-20:]]

    def get_alerts(self) -> List[str]:
        with self._lock:
            return list(self._alerts)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            accuracies = [s.overall_accuracy for s in self._snapshots] if self._snapshots else [self.BASELINE_ACCURACY]
            return {
                "snapshots": len(self._snapshots),
                "current_accuracy": accuracies[-1] if accuracies else self.BASELINE_ACCURACY,
                "baseline": self.BASELINE_ACCURACY,
                "min_accuracy": min(accuracies),
                "max_accuracy": max(accuracies),
                "avg_accuracy": sum(accuracies) / len(accuracies),
                "alerts": len(self._alerts),
                "below_baseline": self.is_below_baseline(),
            }


# ============================================================================
# P18-3-6: HiveConsensusEngine — 蜂群共识引擎
# ============================================================================

class HiveConsensusEngine:
    """当 Agent 间事实矛盾时，多数投票+置信度加权解决"""

    def __init__(self, default_mode: ConsensusMode = ConsensusMode.CONFIDENCE_WEIGHTED):
        self._lock = threading.RLock()
        self._default_mode = default_mode
        self._consensus_log: List[ConsensusResult] = []

    def reach_consensus(
        self,
        question_key: str,
        agent_votes: Dict[str, FactVerdict],
        agent_confidences: Dict[str, float],
        mode: Optional[ConsensusMode] = None,
    ) -> ConsensusResult:
        """达成蜂群共识"""
        mode = mode or self._default_mode

        with self._lock:
            if mode == ConsensusMode.MAJORITY_VOTE:
                verdict, confidence, agreement = self._majority_vote(agent_votes)
            elif mode == ConsensusMode.CONFIDENCE_WEIGHTED:
                verdict, confidence, agreement = self._confidence_weighted(agent_votes, agent_confidences)
            elif mode == ConsensusMode.UNANIMOUS:
                verdict, confidence, agreement = self._unanimous(agent_votes, agent_confidences)
            else:  # SUPERMAJORITY
                verdict, confidence, agreement = self._supermajority(agent_votes, agent_confidences)

            result = ConsensusResult(
                question_key=question_key,
                mode=mode,
                verdict=verdict,
                final_confidence=confidence,
                agent_votes=agent_votes,
                agent_confidences=agent_confidences,
                agreement_ratio=agreement,
            )
            self._consensus_log.append(result)
            return result

    def _majority_vote(self, votes: Dict[str, FactVerdict]) -> Tuple[FactVerdict, float, float]:
        tally: Dict[FactVerdict, int] = defaultdict(int)
        for v in votes.values():
            tally[v] += 1
        total = len(votes)
        if total == 0:
            return FactVerdict.INCONCLUSIVE, 0.0, 0.0
        top = max(tally, key=tally.get)  # type: ignore
        agreement = tally[top] / total
        confidence = agreement
        return top, confidence, agreement

    def _confidence_weighted(
        self, votes: Dict[str, FactVerdict], confidences: Dict[str, float]
    ) -> Tuple[FactVerdict, float, float]:
        scores: Dict[FactVerdict, float] = defaultdict(float)
        for agent, verdict in votes.items():
            scores[verdict] += confidences.get(agent, 0.5)
        total_weight = sum(scores.values())
        if total_weight == 0:
            return FactVerdict.INCONCLUSIVE, 0.0, 0.0
        top = max(scores, key=scores.get)  # type: ignore
        final_conf = scores[top] / total_weight
        # agreement 用简单多数辅助
        tally: Dict[FactVerdict, int] = defaultdict(int)
        for v in votes.values():
            tally[v] += 1
        agreement = tally[top] / len(votes) if votes else 0.0
        return top, final_conf, agreement

    def _unanimous(
        self, votes: Dict[str, FactVerdict], confidences: Dict[str, float]
    ) -> Tuple[FactVerdict, float, float]:
        unique = set(votes.values())
        if len(unique) == 1:
            verdict = list(unique)[0]
            avg_conf = sum(confidences.values()) / len(confidences) if confidences else 0.0
            return verdict, avg_conf, 1.0
        return FactVerdict.INCONCLUSIVE, 0.0, len(unique) / len(votes) if votes else 0.0

    def _supermajority(
        self, votes: Dict[str, FactVerdict], confidences: Dict[str, float]
    ) -> Tuple[FactVerdict, float, float]:
        tally: Dict[FactVerdict, int] = defaultdict(int)
        for v in votes.values():
            tally[v] += 1
        total = len(votes)
        if total == 0:
            return FactVerdict.INCONCLUSIVE, 0.0, 0.0
        top = max(tally, key=tally.get)  # type: ignore
        agreement = tally[top] / total
        if agreement >= 2 / 3:
            avg_conf = sum(confidences.get(a, 0.5) for a, v in votes.items() if v == top) / tally[top]
            return top, avg_conf, agreement
        return FactVerdict.INCONCLUSIVE, 0.0, agreement

    def get_consensus_history(self) -> List[ConsensusResult]:
        with self._lock:
            return list(self._consensus_log)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            resolved = sum(1 for c in self._consensus_log if c.verdict != FactVerdict.INCONCLUSIVE)
            total = len(self._consensus_log)
            return {
                "total_consensus_events": total,
                "resolved_count": resolved,
                "resolution_rate": resolved / total if total > 0 else 0.0,
                "avg_agreement": sum(c.agreement_ratio for c in self._consensus_log) / total if total > 0 else 1.0,
                "default_mode": self._default_mode.value,
            }
