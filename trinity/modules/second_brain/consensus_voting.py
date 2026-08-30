# consensus_voting — CB54: Consensus Voting for Conflicting Facts
# 对标 Mnemos / SITS2026 多版本共识裁决机制
#
# 解决 conflict_resolver.py "最新优先"策略在用户反复修正同一事实后
# Agent 仍复述旧陈述的局限性，引入版本化快照 + 加权共识投票。
#
# status: frozen (2026-09 EXECUTION 163)
# 对标论文:
#   - Mnemos: Self-Evolving Memory Consensus for Agentic Systems (arXiv:2607.xxxxx, SITS2026)
#   - Consensus Memory Architecture for Multi-Source Agent Systems (SITS2026 闭门报告)
#
# 设计要点:
#   1. MemorySnapshot — 版本化记忆快照，含 trust 分数与 TTL
#   2. ConsensusVoter — 加权共识投票引擎，3 种裁决模式
#   3. MemoryVersionManager — 版本链管理与时间点查询
#   4. conflict_resolver 集成 — 为 RulingEngine 提供共识裁决替代"最新优先"
#   5. bi_temporal_graph 集成 — 时间点查询委托给 BiTemporalGraphEngine

from __future__ import annotations

import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# =============================================================================
# 配置常量
# =============================================================================

CONSENSUS_THRESHOLD: float = 0.6
"""达成共识所需的最低权重占比（0.0-1.0）。仅 majority 模式使用。"""

CONSENSUS_RECENCY_HALF_LIFE: float = 3600.0
"""recency_decay 半衰期（秒）。默认 1 小时。"""

CONSENSUS_MIN_VERSIONS_FOR_VOTE: int = 2
"""最少版本数才触发投票。低于此数默认返回唯一版本。"""

CONSENSUS_AUTO_RESOLVE: bool = True
"""是否自动标记旧版本为 deprecated。False 时仅返回推荐，不修改状态。"""

CONSENSUS_DEFAULT_TTL: float = 86400.0 * 30
"""默认快照 TTL（秒），默认 30 天。"""

CONSENSUS_CONFIDENCE_DECAY_LAMBDA: float = 1e-7
"""confidence 随时间衰减系数（每秒）。e^{-λ·Δt}。"""


# =============================================================================
# 枚举
# =============================================================================

class SnapshotStatus(str, Enum):
    """快照状态"""
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    PENDING_REVIEW = "pending_review"


class VoteMode(str, Enum):
    """裁决模式"""
    HIGHEST_WEIGHT = "highest_weight"
    """最高权重胜出：权重最高的快照直接当选"""
    MAJORITY_CONSENSUS = "majority_consensus"
    """多数共识：需 ≥ consensus_threshold 的归一化权重"""
    WEIGHTED_AVERAGE = "weighted_average"
    """加权平均：按权重合并 content（实际执行 confidence 加权）"""


class ConsensusRecommendation(str, Enum):
    """共识建议"""
    ACCEPT = "accept"
    """接受胜出快照，标记其余为 deprecated"""
    MERGE = "merge"
    """合并多个高共识快照为一个"""
    DEFER = "defer"
    """权重不足，推迟决策，标记为 pending_review"""
    REJECT = "reject"
    """所有快照低质量，全部标记为 deprecated"""


# =============================================================================
# 数据类
# =============================================================================

@dataclass
class MemorySnapshot:
    """
    版本化记忆快照。

    对标 Mnemos FactSnapshot schema，每个快照自包含、带置信度和 TTL。
    """
    memory_id: str
    """关联的记忆/事实 ID"""
    fact_content: str
    """事实内容"""
    confidence: float = 1.0
    """置信度 0.0-1.0"""
    version: float = 0.0
    """版本号（Unix 时间戳）"""
    source: str = "user"
    """来源标识"""
    ttl: float = 0.0
    """生存时间（Unix 时间戳，过期失效）"""
    status: SnapshotStatus = SnapshotStatus.ACTIVE
    """快照状态"""
    snapshot_id: str = ""
    """快照唯一标识"""

    def __post_init__(self):
        if not self.snapshot_id:
            import uuid
            self.snapshot_id = f"ss_{uuid.uuid4().hex[:12]}"
        if self.version == 0.0:
            self.version = time.time()
        if self.ttl == 0.0:
            self.ttl = time.time() + CONSENSUS_DEFAULT_TTL

    def is_expired(self, now: Optional[float] = None) -> bool:
        """判断快照是否过期。"""
        _now = now or time.time()
        return _now > self.ttl

    @property
    def age_seconds(self, now: Optional[float] = None) -> float:
        """快照年龄（秒）。"""
        _now = now or time.time()
        return max(0.0, _now - self.version)


@dataclass
class ConsensusResult:
    """
    共识投票结果。

    对标 Mnemos ConsensusRecord，包含完整投票分布与建议。
    """
    winner_snapshot: Optional[MemorySnapshot] = None
    """胜出快照"""
    confidence: float = 0.0
    """共识置信度（归一化后胜者的权重占比）"""
    consensus_reached: bool = False
    """是否达成共识"""
    vote_distribution: dict[str, float] = field(default_factory=dict)
    """各快照的归一化权重分布 {snapshot_id: normalized_weight}"""
    deprecated_snapshots: list[MemorySnapshot] = field(default_factory=list)
    """被淘汰的快照列表"""
    recommendation: ConsensusRecommendation = ConsensusRecommendation.DEFER
    """后续行动建议"""
    vote_mode: VoteMode = VoteMode.HIGHEST_WEIGHT
    """使用的裁决模式"""
    tie_broken: bool = False
    """是否触发平局打破"""
    reasoning: str = ""

    def summary(self) -> str:
        return (
            f"mode={self.vote_mode.value} reached={self.consensus_reached} "
            f"confidence={self.confidence:.3f} rec={self.recommendation.value} "
            f"deprecated={len(self.deprecated_snapshots)}"
        )


# =============================================================================
# ConsensusVoter — 加权共识投票引擎
# =============================================================================

class ConsensusVoter:
    """
    CB54: ConsensusVoter — 多版本共识裁决引擎。

    三种裁决模式:
      - highest_weight: 权重最高的快照直接胜出
      - majority_consensus: 需要胜者归一化权重 ≥ consensus_threshold
      - weighted_average: 多快照按权重加权合并 content（confidence 加权）

    权重公式:
      effective_weight = confidence × recency_decay
      recency_decay = 2^{-age / half_life}
    """

    def __init__(self,
                 mode: VoteMode = VoteMode.HIGHEST_WEIGHT,
                 threshold: float = CONSENSUS_THRESHOLD,
                 half_life: float = CONSENSUS_RECENCY_HALF_LIFE,
                 min_versions: int = CONSENSUS_MIN_VERSIONS_FOR_VOTE,
                 auto_resolve: bool = CONSENSUS_AUTO_RESOLVE):
        self.mode = mode
        self.threshold = threshold
        self.half_life = half_life
        self.min_versions = min_versions
        self.auto_resolve = auto_resolve
        self._lock = threading.RLock()

        # 集成引用
        self.cr_ref: Any = None          # ConflictResolver
        self.bt_graph_ref: Any = None    # BiTemporalGraphEngine

        # 统计
        self.vote_count: int = 0
        self.tie_count: int = 0

    # ------------------------------------------------------------------
    # vote — 核心投票方法
    # ------------------------------------------------------------------

    def vote(self, snapshots: list[MemorySnapshot],
             now: Optional[float] = None) -> ConsensusResult:
        """
        对同一事实的多个版本进行共识投票。

        Args:
            snapshots: 待投票的快照列表
            now: 当前时间戳

        Returns:
            ConsensusResult 包含胜者与建议
        """
        _now = now or time.time()

        with self._lock:
            self.vote_count += 1

            # 过滤过期快照
            active = [s for s in snapshots if not s.is_expired(_now)]

            # 不足最小版本数
            if len(active) < self.min_versions:
                return self._trivial_result(active, _now)

            # 计算有效权重
            weights: list[tuple[MemorySnapshot, float]] = []
            for s in active:
                w = self._compute_weight(s, _now)
                weights.append((s, w))

            # 按权重降序
            weights.sort(key=lambda x: -x[1])

            # 根据模式裁决
            if self.mode == VoteMode.HIGHEST_WEIGHT:
                result = self._vote_highest_weight(weights, _now)
            elif self.mode == VoteMode.MAJORITY_CONSENSUS:
                result = self._vote_majority(weights, _now)
            elif self.mode == VoteMode.WEIGHTED_AVERAGE:
                result = self._vote_weighted_average(weights, _now)
            else:
                result = self._vote_highest_weight(weights, _now)

            return result

    # ------------------------------------------------------------------
    # 权重计算
    # ------------------------------------------------------------------

    def _compute_weight(self, snapshot: MemorySnapshot,
                        now: float) -> float:
        """
        计算快照的有效投票权重。

        公式: effective_weight = confidence × recency_decay × trust_bonus

        recency_decay = 2^{-age / half_life}
        trust_bonus = 1.0 + 0.1 × (source == "user")  — 用户直接输入享有 10% 加成
        """
        confidence = max(0.0, min(1.0, snapshot.confidence))
        age = max(0.0, now - snapshot.version)

        # recency decay: 指数衰减，半衰期为 half_life
        if self.half_life > 0 and age > 0:
            recency = 2.0 ** (-age / self.half_life)
        else:
            recency = 1.0

        # trust bonus: 用户直接输入有轻微加成
        trust_bonus = 1.0
        if snapshot.source and snapshot.source.lower() in ("user", "human", "direct"):
            trust_bonus = 1.10

        weight = confidence * recency * trust_bonus
        return max(0.0, weight)

    # ------------------------------------------------------------------
    # 裁决模式实现
    # ------------------------------------------------------------------

    def _vote_highest_weight(self,
                              weights: list[tuple[MemorySnapshot, float]],
                              now: float) -> ConsensusResult:
        """最高权重胜出模式。"""
        if not weights:
            return self._empty_result(now)

        top, top_weight = weights[0]
        total_weight = sum(w for _, w in weights)
        normalized = top_weight / total_weight if total_weight > 0 else 0.0

        # 平局检测: 最高权重与次高权重相差 < 5%
        tie_broken = False
        if len(weights) > 1:
            second_weight = weights[1][1]
            if second_weight > 0 and abs(top_weight - second_weight) / max(top_weight, second_weight) < 0.05:
                tie_broken = True
                self.tie_count += 1
                # 平局打破: 优先最近版本
                if weights[1][0].version > top.version:
                    top, top_weight = weights[1]
                    normalized = top_weight / total_weight

        distribution = {s.snapshot_id: w / total_weight for s, w in weights} if total_weight > 0 else {}
        deprecated = [s for s, _ in weights if s.snapshot_id != top.snapshot_id]

        if tie_broken:
            recommendation = ConsensusRecommendation.MERGE
            reasoning = "Tie broken by recency; consider merging both versions."
        else:
            recommendation = ConsensusRecommendation.ACCEPT
            reasoning = f"Winner by highest weight ({top_weight:.4f} / {total_weight:.4f})"

        return ConsensusResult(
            winner_snapshot=top,
            confidence=normalized,
            consensus_reached=normalized >= self.threshold,
            vote_distribution=distribution,
            deprecated_snapshots=deprecated,
            recommendation=recommendation,
            vote_mode=VoteMode.HIGHEST_WEIGHT,
            tie_broken=tie_broken,
            reasoning=reasoning,
        )

    def _vote_majority(self,
                       weights: list[tuple[MemorySnapshot, float]],
                       now: float) -> ConsensusResult:
        """多数共识模式: 胜者权重占比必须 ≥ threshold。"""
        if not weights:
            return self._empty_result(now)

        total_weight = sum(w for _, w in weights)
        if total_weight == 0:
            return self._empty_result(now)

        top, top_weight = weights[0]
        normalized = top_weight / total_weight

        distribution = {s.snapshot_id: w / total_weight for s, w in weights}

        if normalized >= self.threshold:
            recommendation = ConsensusRecommendation.ACCEPT
            consensus_reached = True
            deprecated = [s for s, _ in weights if s.snapshot_id != top.snapshot_id]
            reasoning = f"Majority consensus reached: {normalized:.3f} >= {self.threshold}"
        else:
            # 未达阈值: 检查是否可以合并多个高权重快照
            cumulative = 0.0
            merge_candidates = []
            for s, w in weights:
                cumulative += w / total_weight
                merge_candidates.append(s)
                if cumulative >= self.threshold:
                    break

            if len(merge_candidates) > 1:
                recommendation = ConsensusRecommendation.MERGE
                reasoning = f"No single majority ({normalized:.3f} < {self.threshold}); merge top {len(merge_candidates)}"
            else:
                recommendation = ConsensusRecommendation.DEFER
                reasoning = f"No consensus ({normalized:.3f} < {self.threshold}); postpone."

            consensus_reached = False
            deprecated = []

        return ConsensusResult(
            winner_snapshot=top,
            confidence=normalized,
            consensus_reached=consensus_reached,
            vote_distribution=distribution,
            deprecated_snapshots=deprecated,
            recommendation=recommendation,
            vote_mode=VoteMode.MAJORITY_CONSENSUS,
            reasoning=reasoning,
        )

    def _vote_weighted_average(self,
                                weights: list[tuple[MemorySnapshot, float]],
                                now: float) -> ConsensusResult:
        """加权平均模式: 按权重合并多快照的置信度与内容。"""
        if not weights:
            return self._empty_result(now)

        total_weight = sum(w for _, w in weights)
        if total_weight == 0:
            return self._empty_result(now)

        # 加权平均 confidence
        avg_confidence = sum(s.confidence * w for s, w in weights) / total_weight

        # 构造合并后的 synthetic snapshot
        top = weights[0][0]
        merged_content = self._merge_snapshot_contents(weights)
        synthetic = MemorySnapshot(
            memory_id=top.memory_id,
            fact_content=merged_content,
            confidence=avg_confidence,
            version=now,
            source=f"consensus_weighted_avg_{len(weights)}",
            status=SnapshotStatus.ACTIVE,
        )

        distribution = {s.snapshot_id: w / total_weight for s, w in weights}
        all_snaps = [s for s, _ in weights]

        return ConsensusResult(
            winner_snapshot=synthetic,
            confidence=avg_confidence,
            consensus_reached=True,
            vote_distribution=distribution,
            deprecated_snapshots=all_snaps,
            recommendation=ConsensusRecommendation.MERGE,
            vote_mode=VoteMode.WEIGHTED_AVERAGE,
            reasoning=f"Weighted average of {len(weights)} snapshots, avg confidence={avg_confidence:.3f}",
        )

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _trivial_result(self, snapshots: list[MemorySnapshot],
                        now: float) -> ConsensusResult:
        """版本不足时的平凡结果。"""
        if not snapshots:
            return self._empty_result(now)
        only = snapshots[0]
        return ConsensusResult(
            winner_snapshot=only,
            confidence=only.confidence,
            consensus_reached=True,
            vote_distribution={only.snapshot_id: 1.0},
            deprecated_snapshots=[],
            recommendation=ConsensusRecommendation.ACCEPT,
            vote_mode=self.mode,
            reasoning=f"Only {len(snapshots)} valid snapshot(s), below min_versions={self.min_versions}",
        )

    def _empty_result(self, now: float) -> ConsensusResult:
        return ConsensusResult(
            winner_snapshot=None,
            confidence=0.0,
            consensus_reached=False,
            recommendation=ConsensusRecommendation.REJECT,
            vote_mode=self.mode,
            reasoning="No valid snapshots available",
        )

    @staticmethod
    def _merge_snapshot_contents(
        weights: list[tuple[MemorySnapshot, float]]) -> str:
        """
        合并多个快照的内容。按权重降序排列，去冗余句子。
        """
        seen_sentences: set[str] = set()
        merged_parts: list[str] = []

        # 按权重降序
        sorted_snaps = sorted(weights, key=lambda x: -x[1])
        for snap, _ in sorted_snaps:
            # 简单按句号分句
            parts = [s.strip() for s in snap.fact_content.replace('\n', '. ').split('.')]
            for p in parts:
                if not p:
                    continue
                key = p.lower()
                if key not in seen_sentences:
                    seen_sentences.add(key)
                    merged_parts.append(p)

        return '. '.join(merged_parts) + '.'

    def statistics(self) -> dict:
        return {
            "vote_count": self.vote_count,
            "tie_count": self.tie_count,
            "mode": self.mode.value,
            "threshold": self.threshold,
            "half_life": self.half_life,
        }


# =============================================================================
# MemoryVersionManager — 版本链管理
# =============================================================================

class MemoryVersionManager:
    """
    CB54: MemoryVersionManager — 记忆版本链管理者。

    管理同一 memory_id 的版本化快照链，提供:
      - 版本追加 (add_version)
      - 共识触发 (resolve)
      - 时间点查询 (point_in_time_query)
      - conflict_resolver 集成 (get_current_fact 委托)
      - bi_temporal_graph 集成 (时间点查询委托)
    """

    def __init__(self,
                 voter: Optional[ConsensusVoter] = None,
                 half_life: float = CONSENSUS_RECENCY_HALF_LIFE,
                 auto_resolve: bool = CONSENSUS_AUTO_RESOLVE):
        self.voter = voter or ConsensusVoter(half_life=half_life)
        self.auto_resolve = auto_resolve

        # 版本链: memory_id → [MemorySnapshot]
        self.versions: dict[str, list[MemorySnapshot]] = defaultdict(list)

        # 集成引用
        self.cr_ref: Any = None          # ConflictResolver
        self.bt_graph_ref: Any = None     # BiTemporalGraphEngine

        # 线程安全
        self._lock = threading.RLock()

        # 统计
        self.total_versions: int = 0
        self.total_resolutions: int = 0

    # ------------------------------------------------------------------
    # 版本管理
    # ------------------------------------------------------------------

    def add_version(self, memory_id: str, fact_content: str,
                    confidence: float = 1.0, source: str = "user",
                    ttl: Optional[float] = None) -> MemorySnapshot:
        """
        写入新版本快照。

        Args:
            memory_id: 记忆 ID
            fact_content: 事实内容
            confidence: 置信度
            source: 来源
            ttl: 过期时间

        Returns:
            新创建的 MemorySnapshot
        """
        with self._lock:
            # 为旧版本做 confidence decay
            if memory_id in self.versions:
                now = time.time()
                for old in self.versions[memory_id]:
                    if old.status == SnapshotStatus.ACTIVE:
                        age = max(0.0, now - old.version)
                        old.confidence = old.confidence * math.exp(
                            -CONSENSUS_CONFIDENCE_DECAY_LAMBDA * age
                        )

            snapshot = MemorySnapshot(
                memory_id=memory_id,
                fact_content=fact_content,
                confidence=confidence,
                source=source,
                ttl=ttl or (time.time() + CONSENSUS_DEFAULT_TTL),
            )
            self.versions[memory_id].append(snapshot)
            self.total_versions += 1

            # 自动触发 resolve
            if self.auto_resolve and len(self.versions[memory_id]) >= CONSENSUS_MIN_VERSIONS_FOR_VOTE:
                self._auto_resolve(memory_id)

            return snapshot

    def resolve(self, memory_id: str) -> ConsensusResult:
        """
        触发共识投票，标记旧快照为 deprecated。

        Args:
            memory_id: 记忆 ID

        Returns:
            ConsensusResult
        """
        with self._lock:
            snapshots = self.versions.get(memory_id, [])
            if not snapshots:
                return self.voter._empty_result(time.time())

            result = self.voter.vote(snapshots)
            self.total_resolutions += 1

            # 标记 deprecated
            if result.recommendation == ConsensusRecommendation.ACCEPT:
                for s in result.deprecated_snapshots:
                    s.status = SnapshotStatus.DEPRECATED
            elif result.recommendation == ConsensusRecommendation.REJECT:
                for s in snapshots:
                    s.status = SnapshotStatus.DEPRECATED
            elif result.recommendation == ConsensusRecommendation.DEFER:
                for s in snapshots:
                    s.status = SnapshotStatus.PENDING_REVIEW
            elif result.recommendation == ConsensusRecommendation.MERGE:
                for s in result.deprecated_snapshots:
                    s.status = SnapshotStatus.DEPRECATED

            # 集成: 更新 bi_temporal_graph
            if self.bt_graph_ref and result.winner_snapshot:
                try:
                    self.bt_graph_ref.add_entity(
                        entity_id=memory_id,
                        entity_type="memory_snapshot",
                        properties={
                            "content": result.winner_snapshot.fact_content,
                            "confidence": result.confidence,
                            "consensus_reached": result.consensus_reached,
                        },
                        ingested_by="consensus_voting",
                    )
                except Exception:
                    logger.debug("bt_graph add_entity skipped for %s", memory_id)

            return result

    def point_in_time_query(self, memory_id: str,
                            timestamp: float) -> Optional[MemorySnapshot]:
        """
        时间点查询: 返回该时间点有效的快照。

        按 version 排序，返回 version ≤ timestamp 的最新快照。
        如集成 bi_temporal_graph，委托给其 point_in_time_query。
        """
        with self._lock:
            # 优先委托 bi_temporal_graph
            if self.bt_graph_ref:
                try:
                    pit_result = self.bt_graph_ref.point_in_time_query(
                        entity_id=memory_id, timestamp=timestamp
                    )
                    if pit_result:
                        return self._bt_to_snapshot(pit_result, memory_id)
                except Exception:
                    logger.debug("bt_graph point_in_time_query fallback for %s", memory_id)

            # 兜底: 从版本链查找
            snapshots = self.versions.get(memory_id, [])
            candidates = [s for s in snapshots if s.version <= timestamp]
            if not candidates:
                return None
            candidates.sort(key=lambda s: s.version)
            return candidates[-1]

    def get_active_snapshots(self, memory_id: str) -> list[MemorySnapshot]:
        """返回所有活跃快照。"""
        with self._lock:
            return [s for s in self.versions.get(memory_id, [])
                    if s.status == SnapshotStatus.ACTIVE and not s.is_expired()]

    def get_version_history(self, memory_id: str) -> list[MemorySnapshot]:
        """返回完整版本历史（按 version 升序）。"""
        with self._lock:
            return sorted(self.versions.get(memory_id, []), key=lambda s: s.version)

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _auto_resolve(self, memory_id: str) -> None:
        """自动触发共识投票并应用结果。"""
        active = self.get_active_snapshots(memory_id)
        if len(active) < CONSENSUS_MIN_VERSIONS_FOR_VOTE:
            return
        self.resolve(memory_id)

    @staticmethod
    def _bt_to_snapshot(entity: Any, memory_id: str) -> Optional[MemorySnapshot]:
        """将 BiTemporalEntity 转换为 MemorySnapshot。"""
        if entity is None:
            return None
        props = getattr(entity, 'properties', {}) or {}
        return MemorySnapshot(
            memory_id=memory_id,
            fact_content=props.get('content', str(props)),
            confidence=props.get('confidence', 1.0),
            version=getattr(entity, 'event_time', time.time()),
            source=getattr(entity, 'ingested_by', 'bi_temporal'),
            status=SnapshotStatus.ACTIVE,
        )

    # ------------------------------------------------------------------
    # conflict_resolver 集成
    # ------------------------------------------------------------------

    def resolve_with_conflict_resolver(self, memory_id: str) -> dict:
        """
        双通道裁决: 先走共识投票，再将结果反馈给 conflict_resolver。

        Returns:
            {"consensus": ConsensusResult, "cr_action": str}
        """
        consensus = self.resolve(memory_id)
        cr_action = "none"

        if self.cr_ref and consensus.winner_snapshot:
            try:
                ws = consensus.winner_snapshot
                # 将共识胜者注入 conflict_resolver 为权威事实
                self.cr_ref.add_fact(
                    content=ws.fact_content,
                    entity_type="consensus",
                    entity_id=memory_id,
                    source=ws.source,
                    authority=ws.confidence,
                    scope="global",
                    specificity=1.0,
                    metadata={"via": "consensus_voting", "snapshot_id": ws.snapshot_id},
                )
                cr_action = "injected_to_cr"
            except Exception:
                logger.debug("cr add_fact failed for %s", memory_id)

        return {"consensus": consensus, "cr_action": cr_action}

    def statistics(self) -> dict:
        return {
            "total_versions": self.total_versions,
            "total_resolutions": self.total_resolutions,
            "tracked_memories": len(self.versions),
            "voter_stats": self.voter.statistics(),
        }


# =============================================================================
# 工厂函数
# =============================================================================

def create_consensus_voter(
    mode: str = "highest_weight",
    threshold: float = CONSENSUS_THRESHOLD,
    half_life: float = CONSENSUS_RECENCY_HALF_LIFE,
    cr_instance: Any = None,
    bt_graph_instance: Any = None,
) -> ConsensusVoter:
    """创建 ConsensusVoter 实例的工厂函数。"""
    mode_map = {
        "highest_weight": VoteMode.HIGHEST_WEIGHT,
        "majority_consensus": VoteMode.MAJORITY_CONSENSUS,
        "weighted_average": VoteMode.WEIGHTED_AVERAGE,
    }
    voter = ConsensusVoter(
        mode=mode_map.get(mode, VoteMode.HIGHEST_WEIGHT),
        threshold=threshold,
        half_life=half_life,
    )
    voter.cr_ref = cr_instance
    voter.bt_graph_ref = bt_graph_instance
    return voter


def create_version_manager(
    voter: Optional[ConsensusVoter] = None,
    cr_instance: Any = None,
    bt_graph_instance: Any = None,
) -> MemoryVersionManager:
    """创建 MemoryVersionManager 实例的工厂函数。"""
    mgr = MemoryVersionManager(voter=voter)
    mgr.cr_ref = cr_instance
    mgr.bt_graph_ref = bt_graph_instance
    return mgr


# =============================================================================
# Self-Test
# =============================================================================

def self_test() -> bool:
    """自测：验证 ConsensusVoter + MemoryVersionManager 全流程。"""
    import time as _time

    try:
        # === 1. ConsensusVoter: highest_weight ===
        voter = ConsensusVoter(mode=VoteMode.HIGHEST_WEIGHT)

        s1 = MemorySnapshot(
            memory_id="fact_001",
            fact_content="The capital of France is Paris",
            confidence=0.9,
            version=_time.time() - 100,
            source="user",
        )
        s2 = MemorySnapshot(
            memory_id="fact_001",
            fact_content="The capital of France is Lyon",
            confidence=0.3,
            version=_time.time() - 10,
            source="web",
        )
        s3 = MemorySnapshot(
            memory_id="fact_001",
            fact_content="The capital of France is Paris",
            confidence=0.8,
            version=_time.time(),
            source="user",
        )

        result = voter.vote([s1, s2, s3])
        assert result.winner_snapshot is not None, "No winner"
        assert "Paris" in result.winner_snapshot.fact_content, "Wrong winner"
        assert result.recommendation == ConsensusRecommendation.ACCEPT
        assert len(result.deprecated_snapshots) == 2
        print("TEST-1 PASS: highest_weight mode")

        # === 2. ConsensusVoter: majority_consensus ===
        voter2 = ConsensusVoter(mode=VoteMode.MAJORITY_CONSENSUS, threshold=0.5)

        s4 = MemorySnapshot(memory_id="fact_002", fact_content="X=True",
                            confidence=0.9, version=_time.time(), source="user")
        s5 = MemorySnapshot(memory_id="fact_002", fact_content="X=False",
                            confidence=0.1, version=_time.time() - 100, source="web")

        result2 = voter2.vote([s4, s5])
        assert result2.consensus_reached, "Majority should be reached"
        assert "True" in result2.winner_snapshot.fact_content
        print("TEST-2 PASS: majority_consensus mode")

        # === 3. MemoryVersionManager: add + resolve ===
        mgr = MemoryVersionManager(voter=ConsensusVoter(mode=VoteMode.HIGHEST_WEIGHT))

        snap1 = mgr.add_version("mem_A", "User likes Python", confidence=0.6, source="user")
        _time.sleep(0.01)
        snap2 = mgr.add_version("mem_A", "User likes Rust", confidence=0.9, source="user")
        _time.sleep(0.01)
        snap3 = mgr.add_version("mem_A", "User likes Rust for systems, Python for AI",
                                confidence=0.95, source="user")

        assert len(mgr.get_version_history("mem_A")) == 3
        result3 = mgr.resolve("mem_A")
        assert result3.winner_snapshot is not None
        assert "Rust" in result3.winner_snapshot.fact_content
        print("TEST-3 PASS: MemoryVersionManager add + resolve")

        # === 4. point_in_time_query ===
        mid_t = snap2.version
        pit_snap = mgr.point_in_time_query("mem_A", mid_t)
        assert pit_snap is not None, "Should find snapshot at midpoint"
        assert "Rust" in pit_snap.fact_content
        print("TEST-4 PASS: point_in_time_query")

        # === 5. weighted_average mode ===
        voter3 = ConsensusVoter(mode=VoteMode.WEIGHTED_AVERAGE)
        s6 = MemorySnapshot(memory_id="fact_003", fact_content="AI is transformative",
                            confidence=0.8, version=_time.time(), source="user")
        s7 = MemorySnapshot(memory_id="fact_003",
                            fact_content="AI requires careful governance",
                            confidence=0.7, version=_time.time() - 10, source="user")
        result4 = voter3.vote([s6, s7])
        assert result4.winner_snapshot is not None
        assert "transformative" in result4.winner_snapshot.fact_content
        assert "governance" in result4.winner_snapshot.fact_content
        print("TEST-5 PASS: weighted_average mode")

        # === 6. recency decay ===
        voter4 = ConsensusVoter(half_life=1.0)  # 1 second half-life
        old = MemorySnapshot(memory_id="fact_004", fact_content="old",
                             confidence=1.0, version=_time.time() - 10, source="user")
        new = MemorySnapshot(memory_id="fact_004", fact_content="new",
                             confidence=0.5, version=_time.time(), source="user")
        result5 = voter4.vote([old, new])
        # new should win due to recency despite lower confidence
        assert "new" in result5.winner_snapshot.fact_content, \
            f"Recency decay should favor newer snapshot, got: {result5.winner_snapshot.fact_content}"
        print("TEST-6 PASS: recency decay")

        # === 7. expired snapshot filtering ===
        expired = MemorySnapshot(memory_id="fact_005", fact_content="expired",
                                 confidence=1.0, version=_time.time() - 1000,
                                 ttl=_time.time() - 1, source="user")
        valid = MemorySnapshot(memory_id="fact_005", fact_content="valid",
                               confidence=0.5, version=_time.time(), source="user")
        result6 = voter.vote([expired, valid])
        assert result6.winner_snapshot is not None
        assert "valid" in result6.winner_snapshot.fact_content
        print("TEST-7 PASS: expired snapshot filtering")

        # === 8. configuration constants ===
        assert CONSENSUS_THRESHOLD == 0.6
        assert CONSENSUS_RECENCY_HALF_LIFE == 3600.0
        assert CONSENSUS_MIN_VERSIONS_FOR_VOTE == 2
        assert CONSENSUS_AUTO_RESOLVE is True

        # === 9. factory functions ===
        v5 = create_consensus_voter(mode="majority_consensus")
        assert v5.mode == VoteMode.MAJORITY_CONSENSUS
        mgr2 = create_version_manager()
        assert mgr2.auto_resolve is True

        # === 10. statistics ===
        stats = voter.statistics()
        assert stats["vote_count"] >= 2
        assert mgr.statistics()["total_versions"] == 3

        logger.info("self_test: ALL 10 ASSERTIONS PASSED")
        return True
    except Exception as e:
        logger.error("self_test: FAILED — %s", e)
        import traceback
        traceback.print_exc()
        raise


print("[P126] ConsensusVoter (CB54) initialized — Mnemos / SITS2026 aligned")
