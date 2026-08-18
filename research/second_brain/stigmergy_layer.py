"""
# status: orphan (2026-08-15 audit, not in runtime path)
P7-3: Ant-Colony Stigmergy Federated Knowledge Layer (对标 Stigmem)
=====================================================================

核心设计（基于 Stigmem v1.0, Eidetic-Labs）：
  - 类型化事实存储（Typed Facts）：结构化类型化事实——实体/关系/属性/事件
  - 信心自然衰减（Confidence Decay）：无验证时信心随时间自然衰减
  - 范围约束复制（Scope-Enforced Replication）：知识在受限范围内复制传播
  - 蚁群信息素模型：Agent 留下信息素轨迹，其他 Agent 沿轨迹发现知识

联邦知识层特性：
  - 无需显式同步：通过信息素传递机制实现去中心化知识共享
  - 信息素蒸发：过时未验证的知识信息素强度自然衰减
  - 轨迹强化：重复验证/使用强化信息素轨迹

Reference: Eidetic-Labs, "Stigmem v1.0: Federated Knowledge Fabric
           for AI Agents", GitHub: offbyonce/stigmem, 2026.
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ────────────────────────────────────────────────────────


class FactType(Enum):
    ENTITY = "entity"
    RELATION = "relation"
    ATTRIBUTE = "attribute"
    EVENT = "event"
    RULE = "rule"
    PROCEDURE = "procedure"


class ScopeLevel(Enum):
    LOCAL = "local"
    GROUP = "group"
    DOMAIN = "domain"
    FEDERATION = "federation"
    PUBLIC = "public"


class DecayModel(Enum):
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    SIGMOID = "sigmoid"
    HYBRID = "hybrid"


class TrailStrategy(Enum):
    DEPOSIT_ON_WRITE = "deposit_on_write"
    DEPOSIT_ON_VERIFY = "deposit_on_verify"
    DEPOSIT_ON_ACCESS = "deposit_on_access"
    COMPOUND = "compound"


# ── 数据结构 ──────────────────────────────────────────────────────────


@dataclass
class TypedFact:
    fact_id: str = field(default_factory=lambda: f"tf_{uuid.uuid4().hex[:12]}")
    fact_type: FactType = FactType.ENTITY
    subject: str = ""
    predicate: str = ""
    object: str = ""
    confidence: float = 1.0
    initial_confidence: float = 1.0
    scope: ScopeLevel = ScopeLevel.LOCAL
    source_agent: str = ""
    tags: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_verified_at: Optional[float] = None
    verification_count: int = 0


@dataclass
class PheromoneTrail:
    trail_id: str = field(default_factory=lambda: f"pht_{uuid.uuid4().hex[:12]}")
    fact_ids: List[str] = field(default_factory=list)
    pheromone_level: float = 1.0
    deposit_agent: str = ""
    path_signature: str = ""
    trail_scope: ScopeLevel = ScopeLevel.LOCAL
    created_at: float = field(default_factory=time.time)
    last_reinforced_at: Optional[float] = None
    reinforcement_count: int = 0


@dataclass
class ScopeConstraint:
    constraint_id: str = field(default_factory=lambda: f"scc_{uuid.uuid4().hex[:10]}")
    source_scope: ScopeLevel = ScopeLevel.LOCAL
    target_scopes: List[ScopeLevel] = field(default_factory=list)
    requires_verification: bool = True
    max_replication_depth: int = 3
    replication_count: int = 0


@dataclass
class DecayState:
    fact_id: str = ""
    elapsed_seconds: float = 0.0
    current_confidence: float = 1.0
    decay_rate: float = 0.0
    half_life_seconds: float = 86400.0
    estimated_expiry: Optional[float] = None


@dataclass
class StigmergyStats:
    total_facts: int = 0
    active_facts: int = 0
    expired_facts: int = 0
    total_trails: int = 0
    active_trails: int = 0
    evaporated_trails: int = 0
    total_verifications: int = 0
    total_replications: int = 0
    avg_confidence: float = 0.0
    avg_pheromone: float = 0.0


# ══════════════════════════════════════════════════════════════════════
# ── _PheromoneDecay：信心衰减与信息素蒸发引擎 ──────────────────────
# ══════════════════════════════════════════════════════════════════════


class _PheromoneDecay:
    """信心衰减模型 + 信息素蒸发控制。

    支持四种衰减模型（指数/线性/S形/混合），
    以及基于蒸发率的信息素自然衰减。
    """

    def __init__(
        self,
        model: DecayModel = DecayModel.EXPONENTIAL,
        half_life_seconds: float = 86400.0,
        min_confidence: float = 0.01,
        sigmoid_steepness: float = 0.00005,
        sigmoid_midpoint_seconds: float = 43200.0,
        evaporation_rate: float = 0.01,
    ):
        self.model = model
        self.half_life_seconds = half_life_seconds
        self.min_confidence = min_confidence
        self.sigmoid_steepness = sigmoid_steepness
        self.sigmoid_midpoint_seconds = sigmoid_midpoint_seconds
        self.evaporation_rate = evaporation_rate
        self._lambda = math.log(2) / max(half_life_seconds, 1)

    def compute(
        self, fact: TypedFact, current_time: Optional[float] = None
    ) -> DecayState:
        now = current_time or time.time()
        last_check = fact.last_verified_at or fact.created_at
        elapsed = max(0.0, now - last_check)

        if self.model == DecayModel.EXPONENTIAL:
            confidence = fact.initial_confidence * math.exp(-self._lambda * elapsed)
        elif self.model == DecayModel.LINEAR:
            total_decay_time = self.half_life_seconds * 2
            decay_amount = fact.initial_confidence * elapsed / max(total_decay_time, 1)
            confidence = max(self.min_confidence, fact.initial_confidence - decay_amount)
        elif self.model == DecayModel.SIGMOID:
            exponent = self.sigmoid_steepness * (elapsed - self.sigmoid_midpoint_seconds)
            confidence = fact.initial_confidence / (1.0 + math.exp(exponent))
        else:  # HYBRID
            if elapsed <= self.half_life_seconds:
                decay_amount = (
                    0.5 * fact.initial_confidence * elapsed / max(self.half_life_seconds, 1)
                )
                confidence = fact.initial_confidence - decay_amount
            else:
                base = fact.initial_confidence * 0.5
                extra_elapsed = elapsed - self.half_life_seconds
                confidence = base * math.exp(-self._lambda * extra_elapsed)

        confidence = max(self.min_confidence, min(1.0, confidence))
        estimated_expiry = now + (
            self.half_life_seconds * 3
            if self.model == DecayModel.EXPONENTIAL
            else self.half_life_seconds * 2
        )
        return DecayState(
            fact_id=fact.fact_id,
            elapsed_seconds=round(elapsed, 2),
            current_confidence=round(confidence, 6),
            decay_rate=round(self._lambda, 8),
            half_life_seconds=self.half_life_seconds,
            estimated_expiry=estimated_expiry,
        )

    def batch_decay(
        self, facts: List[TypedFact], current_time: Optional[float] = None
    ) -> List[DecayState]:
        return [self.compute(f, current_time) for f in facts]

    def evaporate_trail(self, trail: PheromoneTrail, now: float) -> float:
        elapsed = now - (trail.last_reinforced_at or trail.created_at)
        return max(0.001, trail.pheromone_level * math.exp(-self.evaporation_rate * elapsed))

    def statistics(self) -> Dict[str, Any]:
        return {
            "model": self.model.value,
            "half_life_seconds": self.half_life_seconds,
            "lambda": round(self._lambda, 8),
            "min_confidence": self.min_confidence,
            "sigmoid_steepness": self.sigmoid_steepness,
            "sigmoid_midpoint_seconds": self.sigmoid_midpoint_seconds,
            "evaporation_rate": self.evaporation_rate,
        }


# ══════════════════════════════════════════════════════════════════════
# ── _TrailAggregator：轨迹索引 / 范围约束 / 复制引擎 ───────────────
# ══════════════════════════════════════════════════════════════════════


class _TrailAggregator:
    """信息素轨迹管理 + 范围约束复制。

    负责事实索引、轨迹沉积/发现/强化、
    范围约束注册与跨域复制。
    """

    DECAY_HALF_LIFE_MAP: Dict[FactType, float] = {
        FactType.ENTITY: 86400 * 7,
        FactType.RELATION: 86400 * 3,
        FactType.ATTRIBUTE: 86400 * 1,
        FactType.EVENT: 86400 * 0.5,
        FactType.RULE: 86400 * 14,
        FactType.PROCEDURE: 86400 * 30,
    }

    def __init__(
        self,
        agent_id: str,
        decay_engine: _PheromoneDecay,
        trail_strategy: TrailStrategy,
        trail_reinforcement_gain: float,
        max_trails: int,
    ):
        self.agent_id = agent_id
        self.decay_engine = decay_engine
        self.trail_strategy = trail_strategy
        self.trail_reinforcement_gain = trail_reinforcement_gain
        self.max_trails = max_trails

        self._lock = threading.RLock()
        self._facts: Dict[str, TypedFact] = {}
        self._trails: Dict[str, PheromoneTrail] = {}
        self._scope_constraints: Dict[str, ScopeConstraint] = {}
        self._fact_type_indices: Dict[FactType, Set[str]] = defaultdict(set)
        self._scope_indices: Dict[ScopeLevel, Set[str]] = defaultdict(set)
        self._tag_indices: Dict[str, Set[str]] = defaultdict(set)

        self._total_verifications: int = 0
        self._total_replications: int = 0

    # ── 事实写入 ────────────────────────────────────────────────

    def deposit_fact(
        self,
        subject: str,
        predicate: str,
        obj: str = "",
        fact_type: FactType = FactType.ENTITY,
        scope: ScopeLevel | None = None,
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
    ) -> TypedFact:
        fact = TypedFact(
            fact_type=fact_type,
            subject=subject,
            predicate=predicate,
            object=obj,
            confidence=confidence,
            initial_confidence=confidence,
            scope=scope or ScopeLevel.LOCAL,
            source_agent=self.agent_id,
            tags=tags or [],
            last_verified_at=time.time(),
            verification_count=1,
        )
        with self._lock:
            self._facts[fact.fact_id] = fact
            self._fact_type_indices[fact.fact_type].add(fact.fact_id)
            self._scope_indices[fact.scope].add(fact.fact_id)
            for tag in fact.tags:
                self._tag_indices[tag].add(fact.fact_id)

        if self.trail_strategy in (TrailStrategy.DEPOSIT_ON_WRITE, TrailStrategy.COMPOUND):
            self._deposit_trail([fact], f"write:{subject}:{predicate}")

        logger.debug(
            "Deposited fact %s (%s) : %s - %s - %s",
            fact.fact_id, fact_type.value, subject, predicate, obj,
        )
        return fact

    # ── 轨迹操作 ────────────────────────────────────────────────

    def _deposit_trail(self, facts: List[TypedFact], path_hint: str = "") -> Optional[PheromoneTrail]:
        if not facts:
            return None
        fact_ids = [f.fact_id for f in facts]
        signature = hashlib.sha256(
            f"{path_hint}:{':'.join(fact_ids)}".encode()
        ).hexdigest()[:16]

        trail = PheromoneTrail(
            fact_ids=fact_ids,
            pheromone_level=1.0,
            deposit_agent=self.agent_id,
            path_signature=signature,
            trail_scope=(
                facts[0].scope
                if len({f.scope for f in facts}) == 1
                else ScopeLevel.FEDERATION
            ),
        )
        with self._lock:
            self._trails[trail.trail_id] = trail
            while len(self._trails) > self.max_trails:
                weakest = min(
                    self._trails.keys(),
                    key=lambda k: self._trails[k].pheromone_level,
                    default=None,
                )
                if weakest:
                    del self._trails[weakest]
        return trail

    def discover_trails(
        self,
        query_signature: Optional[str] = None,
        scope_filter: Optional[ScopeLevel] = None,
        min_pheromone: float = 0.05,
        top_k: int = 20,
    ) -> List[PheromoneTrail]:
        with self._lock:
            trails = list(self._trails.values())
            if scope_filter:
                trails = [t for t in trails if t.trail_scope.value == scope_filter.value]
            if query_signature:
                trails = [t for t in trails if query_signature[:8] in t.path_signature]
            trails = [t for t in trails if t.pheromone_level >= min_pheromone]
            trails.sort(key=lambda t: t.pheromone_level, reverse=True)
            return trails[:top_k]

    def reinforce_trail(self, trail_id: str, gain: Optional[float] = None) -> bool:
        gain = gain or self.trail_reinforcement_gain
        with self._lock:
            trail = self._trails.get(trail_id)
            if trail is None:
                return False
            trail.pheromone_level = min(1.0, trail.pheromone_level + gain)
            trail.last_reinforced_at = time.time()
            trail.reinforcement_count += 1
        return True

    # ── 验证 ────────────────────────────────────────────────────

    def verify_fact(self, fact_id: str, verification_confidence: float = 1.0) -> bool:
        with self._lock:
            fact = self._facts.get(fact_id)
            if fact is None:
                return False
            fact.confidence = max(fact.confidence, fact.initial_confidence * verification_confidence)
            fact.last_verified_at = time.time()
            fact.verification_count += 1
            self._total_verifications += 1
            for trail in self._trails.values():
                if fact_id in trail.fact_ids:
                    self.reinforce_trail(trail.trail_id)
        return True

    def verify_batch(self, fact_ids: List[str], verification_confidence: float = 1.0) -> int:
        return sum(1 for fid in fact_ids if self.verify_fact(fid, verification_confidence))

    # ── 衰减应用 ────────────────────────────────────────────────

    def apply_decay(self, current_time: Optional[float] = None) -> List[DecayState]:
        now = current_time or time.time()
        states: List[DecayState] = []
        with self._lock:
            for fact in list(self._facts.values()):
                state = self.decay_engine.compute(fact, now)
                fact.confidence = state.current_confidence
                states.append(state)
            for trail in list(self._trails.values()):
                trail.pheromone_level = self.decay_engine.evaporate_trail(trail, now)
        expired = sum(1 for s in states if s.current_confidence < 0.05)
        if expired:
            logger.info("Decay applied: %d facts below threshold", expired)
        return states

    def get_expired_facts(self, threshold: float = 0.05) -> List[TypedFact]:
        with self._lock:
            return [f for f in self._facts.values() if f.confidence < threshold]

    def prune_expired(self, threshold: float = 0.01) -> int:
        removed = 0
        with self._lock:
            expired_ids = [fid for fid, f in self._facts.items() if f.confidence < threshold]
            for fid in expired_ids:
                fact = self._facts.pop(fid)
                self._fact_type_indices[fact.fact_type].discard(fid)
                self._scope_indices[fact.scope].discard(fid)
                for tag in fact.tags:
                    self._tag_indices[tag].discard(fid)
                removed += 1
        logger.info("Pruned %d expired facts (threshold=%.3f)", removed, threshold)
        return removed

    # ── 范围约束复制 ────────────────────────────────────────────

    def register_scope_constraint(
        self,
        source_scope: ScopeLevel,
        target_scopes: List[ScopeLevel],
        requires_verification: bool = True,
        max_replication_depth: int = 3,
    ) -> ScopeConstraint:
        constraint = ScopeConstraint(
            source_scope=source_scope,
            target_scopes=target_scopes,
            requires_verification=requires_verification,
            max_replication_depth=max_replication_depth,
        )
        with self._lock:
            self._scope_constraints[constraint.constraint_id] = constraint
        return constraint

    def replicate_fact(self, fact_id: str, target_scope: ScopeLevel) -> Optional[TypedFact]:
        with self._lock:
            fact = self._facts.get(fact_id)
            if fact is None:
                return None
            matching_constraints = [
                c for c in self._scope_constraints.values()
                if c.source_scope == fact.scope and target_scope in c.target_scopes
            ]
            if matching_constraints:
                constraint = matching_constraints[0]
                if constraint.requires_verification and fact.verification_count < 1:
                    return None
                if constraint.replication_count >= constraint.max_replication_depth:
                    return None
                constraint.replication_count += 1
            else:
                scope_rank = {
                    ScopeLevel.LOCAL: 0, ScopeLevel.GROUP: 1,
                    ScopeLevel.DOMAIN: 2, ScopeLevel.FEDERATION: 3,
                    ScopeLevel.PUBLIC: 4,
                }
                if abs(scope_rank.get(target_scope, 0) - scope_rank.get(fact.scope, 0)) > 1:
                    return None

            replicated = TypedFact(
                fact_type=fact.fact_type,
                subject=fact.subject,
                predicate=fact.predicate,
                object=fact.object,
                confidence=fact.confidence * 0.95,
                initial_confidence=fact.initial_confidence,
                scope=target_scope,
                source_agent=self.agent_id,
                tags=list(fact.tags),
            )
            self._facts[replicated.fact_id] = replicated
            self._fact_type_indices[replicated.fact_type].add(replicated.fact_id)
            self._scope_indices[replicated.scope].add(replicated.fact_id)
            for tag in replicated.tags:
                self._tag_indices[tag].add(replicated.fact_id)
            self._total_replications += 1

        logger.debug("Replicated fact %s: %s → %s", fact_id, fact.scope.value, target_scope.value)
        return replicated

    # ── 查询 ────────────────────────────────────────────────────

    def query_facts(
        self,
        fact_type: Optional[FactType] = None,
        scope: Optional[ScopeLevel] = None,
        tags: Optional[List[str]] = None,
        min_confidence: float = 0.0,
        subjects: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[TypedFact]:
        with self._lock:
            candidates: Set[str] = set()
            if fact_type:
                candidates = self._fact_type_indices.get(fact_type, set()).copy()
            elif scope:
                candidates = self._scope_indices.get(scope, set()).copy()
            elif tags:
                for tag in tags:
                    candidates |= self._tag_indices.get(tag, set())
            else:
                candidates = set(self._facts.keys())

            if fact_type:
                candidates &= self._fact_type_indices.get(fact_type, set())
            if scope:
                candidates &= self._scope_indices.get(scope, set())
            if tags:
                for tag in tags:
                    candidates &= self._tag_indices.get(tag, set())

            results = []
            for fid in candidates:
                fact = self._facts.get(fid)
                if fact is None:
                    continue
                if fact.confidence < min_confidence:
                    continue
                if subjects and fact.subject not in subjects:
                    continue
                results.append(fact)
            results.sort(key=lambda f: f.confidence, reverse=True)
            return results[:limit]

    def get_fact(self, fact_id: str) -> Optional[TypedFact]:
        return self._facts.get(fact_id)

    def get_trail(self, trail_id: str) -> Optional[PheromoneTrail]:
        return self._trails.get(trail_id)

    # ── 统计 ────────────────────────────────────────────────────

    def snapshot(self) -> StigmergyStats:
        with self._lock:
            facts = list(self._facts.values())
            trails = list(self._trails.values())
            active_facts = [f for f in facts if f.confidence >= 0.05]
            active_trails = [t for t in trails if t.pheromone_level >= 0.01]
            return StigmergyStats(
                total_facts=len(facts),
                active_facts=len(active_facts),
                expired_facts=len(facts) - len(active_facts),
                total_trails=len(trails),
                active_trails=len(active_trails),
                evaporated_trails=len(trails) - len(active_trails),
                total_verifications=self._total_verifications,
                total_replications=self._total_replications,
                avg_confidence=round(
                    np.mean([f.confidence for f in facts]) if facts else 0.0, 4
                ),
                avg_pheromone=round(
                    np.mean([t.pheromone_level for t in trails]) if trails else 0.0, 4
                ),
            )

    def scope_distribution(self) -> Dict[str, int]:
        return {sl.value: len(self._scope_indices.get(sl, set())) for sl in ScopeLevel}

    def type_distribution(self) -> Dict[str, int]:
        return {ft.value: len(self._fact_type_indices.get(ft, set())) for ft in FactType}

    def constraint_count(self) -> int:
        return len(self._scope_constraints)


# ══════════════════════════════════════════════════════════════════════
# ── Facade：StigmergyLayer ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

    def statistics_dict(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {
            "facts_total": snap.total_facts, "facts_active": snap.active_facts,
            "facts_expired": snap.expired_facts, "trails_total": snap.total_trails,
            "trails_active": snap.active_trails, "trails_evaporated": snap.evaporated_trails,
            "verifications_total": snap.total_verifications,
            "replications_total": snap.total_replications,
            "avg_fact_confidence": snap.avg_confidence,
            "avg_pheromone_level": snap.avg_pheromone,
            "scope_constraints": self.constraint_count(),
            "fact_type_distribution": self.type_distribution(),
            "scope_distribution": self.scope_distribution()}




class StigmergyLayer:
    """蚁群信息素联邦知识层。基于生物 Stigmergy 模型实现去中心化知识共享：
    Agent 创建/验证事实时留下信息素轨迹，未验证知识随时间衰减，重复验证强化轨迹。"""

    def __init__(self, agent_id: str = "", default_scope: ScopeLevel = ScopeLevel.LOCAL,
                 decay_model: DecayModel = DecayModel.EXPONENTIAL,
                 trail_strategy: TrailStrategy = TrailStrategy.COMPOUND,
                 pheromone_evaporation_rate: float = 0.01,
                 trail_reinforcement_gain: float = 0.15, max_trails: int = 500):
        self.agent_id = agent_id or f"agent_{uuid.uuid4().hex[:8]}"
        self.default_scope = default_scope; self.trail_strategy = trail_strategy
        self._decay = _PheromoneDecay(model=decay_model, evaporation_rate=pheromone_evaporation_rate)
        self._aggregator = _TrailAggregator(
            agent_id=self.agent_id, decay_engine=self._decay,
            trail_strategy=trail_strategy, trail_reinforcement_gain=trail_reinforcement_gain,
            max_trails=max_trails)
        logger.info("StigmergyLayer initialized (agent=%s, scope=%s, decay=%s)",
                    self.agent_id, default_scope.value, decay_model.value)

    # ── 事实 ──
    def deposit_fact(self, subject: str, predicate: str, obj: str = "",
                     fact_type: FactType = FactType.ENTITY, scope: Optional[ScopeLevel] = None,
                     confidence: float = 1.0, tags: Optional[List[str]] = None) -> TypedFact:
        return self._aggregator.deposit_fact(subject, predicate, obj, fact_type,
                                              scope or self.default_scope, confidence, tags)

    # ── 轨迹 ──
    def discover_trails(self, query_signature: Optional[str] = None,
                        scope_filter: Optional[ScopeLevel] = None,
                        min_pheromone: float = 0.05, top_k: int = 20) -> List[PheromoneTrail]:
        return self._aggregator.discover_trails(query_signature, scope_filter, min_pheromone, top_k)
    def reinforce_trail(self, trail_id: str, gain: Optional[float] = None) -> bool:
        return self._aggregator.reinforce_trail(trail_id, gain)

    # ── 验证 ──
    def verify_fact(self, fact_id: str, verification_confidence: float = 1.0) -> bool:
        return self._aggregator.verify_fact(fact_id, verification_confidence)
    def verify_batch(self, fact_ids: List[str], verification_confidence: float = 1.0) -> int:
        return self._aggregator.verify_batch(fact_ids, verification_confidence)

    # ── 衰减 ──
    def apply_decay(self, current_time: Optional[float] = None) -> List[DecayState]:
        return self._aggregator.apply_decay(current_time)
    def get_expired_facts(self, threshold: float = 0.05) -> List[TypedFact]:
        return self._aggregator.get_expired_facts(threshold)
    def prune_expired(self, threshold: float = 0.01) -> int:
        return self._aggregator.prune_expired(threshold)

    # ── 复制 ──
    def register_scope_constraint(self, source_scope: ScopeLevel, target_scopes: List[ScopeLevel],
                                   requires_verification: bool = True,
                                   max_replication_depth: int = 3) -> ScopeConstraint:
        return self._aggregator.register_scope_constraint(
            source_scope, target_scopes, requires_verification, max_replication_depth)
    def replicate_fact(self, fact_id: str, target_scope: ScopeLevel) -> Optional[TypedFact]:
        return self._aggregator.replicate_fact(fact_id, target_scope)

    # ── 查询 ──
    def query_facts(self, fact_type: Optional[FactType] = None, scope: Optional[ScopeLevel] = None,
                    tags: Optional[List[str]] = None, min_confidence: float = 0.0,
                    subjects: Optional[List[str]] = None, limit: int = 50) -> List[TypedFact]:
        return self._aggregator.query_facts(fact_type, scope, tags, min_confidence, subjects, limit)
    def get_fact(self, fact_id: str) -> Optional[TypedFact]:
        return self._aggregator.get_fact(fact_id)
    def get_trail(self, trail_id: str) -> Optional[PheromoneTrail]:
        return self._aggregator.get_trail(trail_id)

    # ── 统计 / 快照 / 重置 ──
    def snapshot(self) -> StigmergyStats:
        return self._aggregator.snapshot()
    def statistics(self) -> Dict[str, Any]:
        d = self._aggregator.statistics_dict()
        d.update({"agent_id": self.agent_id, "default_scope": self.default_scope.value,
                  "trail_strategy": self.trail_strategy.value, "decay": self._decay.statistics()})
        return d
    def reset(self) -> None:
        self._aggregator = _TrailAggregator(
            agent_id=self.agent_id, decay_engine=self._decay,
            trail_strategy=self.trail_strategy,
            trail_reinforcement_gain=self._aggregator.trail_reinforcement_gain,
            max_trails=self._aggregator.max_trails)
        logger.info("StigmergyLayer reset")


# ══════════════════════════════════════════════════════════════════════
# ── _PheromoneDecay：信心衰减与信息素蒸发引擎 ──────────────────────
# ══════════════════════════════════════════════════════════════════════


class _PheromoneDecay:
    """信心衰减模型 + 信息素蒸发控制。

    支持四种衰减模型（指数/线性/S形/混合），
    以及基于蒸发率的信息素自然衰减。
    """

    def __init__(
        self,
        model: DecayModel = DecayModel.EXPONENTIAL,
        half_life_seconds: float = 86400.0,
        min_confidence: float = 0.01,
        sigmoid_steepness: float = 0.00005,
        sigmoid_midpoint_seconds: float = 43200.0,
        evaporation_rate: float = 0.01,
    ):
        self.model = model
        self.half_life_seconds = half_life_seconds
        self.min_confidence = min_confidence
        self.sigmoid_steepness = sigmoid_steepness
        self.sigmoid_midpoint_seconds = sigmoid_midpoint_seconds
        self.evaporation_rate = evaporation_rate
        self._lambda = math.log(2) / max(half_life_seconds, 1)

    def compute(
        self, fact: TypedFact, current_time: Optional[float] = None
    ) -> DecayState:
        now = current_time or time.time()
        last_check = fact.last_verified_at or fact.created_at
        elapsed = max(0.0, now - last_check)

        if self.model == DecayModel.EXPONENTIAL:
            confidence = fact.initial_confidence * math.exp(-self._lambda * elapsed)
        elif self.model == DecayModel.LINEAR:
            total_decay_time = self.half_life_seconds * 2
            decay_amount = fact.initial_confidence * elapsed / max(total_decay_time, 1)
            confidence = max(self.min_confidence, fact.initial_confidence - decay_amount)
        elif self.model == DecayModel.SIGMOID:
            exponent = self.sigmoid_steepness * (elapsed - self.sigmoid_midpoint_seconds)
            confidence = fact.initial_confidence / (1.0 + math.exp(exponent))
        else:  # HYBRID
            if elapsed <= self.half_life_seconds:
                decay_amount = (
                    0.5 * fact.initial_confidence * elapsed / max(self.half_life_seconds, 1)
                )
                confidence = fact.initial_confidence - decay_amount
            else:
                base = fact.initial_confidence * 0.5
                extra_elapsed = elapsed - self.half_life_seconds
                confidence = base * math.exp(-self._lambda * extra_elapsed)

        confidence = max(self.min_confidence, min(1.0, confidence))
        estimated_expiry = now + (
            self.half_life_seconds * 3
            if self.model == DecayModel.EXPONENTIAL
            else self.half_life_seconds * 2
        )
        return DecayState(
            fact_id=fact.fact_id,
            elapsed_seconds=round(elapsed, 2),
            current_confidence=round(confidence, 6),
            decay_rate=round(self._lambda, 8),
            half_life_seconds=self.half_life_seconds,
            estimated_expiry=estimated_expiry,
        )

    def batch_decay(
        self, facts: List[TypedFact], current_time: Optional[float] = None
    ) -> List[DecayState]:
        return [self.compute(f, current_time) for f in facts]

    def evaporate_trail(self, trail: PheromoneTrail, now: float) -> float:
        elapsed = now - (trail.last_reinforced_at or trail.created_at)
        return max(0.001, trail.pheromone_level * math.exp(-self.evaporation_rate * elapsed))

    def statistics(self) -> Dict[str, Any]:
        return {
            "model": self.model.value,
            "half_life_seconds": self.half_life_seconds,
            "lambda": round(self._lambda, 8),
            "min_confidence": self.min_confidence,
            "sigmoid_steepness": self.sigmoid_steepness,
            "sigmoid_midpoint_seconds": self.sigmoid_midpoint_seconds,
            "evaporation_rate": self.evaporation_rate,
        }


# ══════════════════════════════════════════════════════════════════════
# ── _TrailAggregator：轨迹索引 / 范围约束 / 复制引擎 ───────────────
# ══════════════════════════════════════════════════════════════════════


class _TrailAggregator:
    """信息素轨迹管理 + 范围约束复制。

    负责事实索引、轨迹沉积/发现/强化、
    范围约束注册与跨域复制。
    """

    DECAY_HALF_LIFE_MAP: Dict[FactType, float] = {
        FactType.ENTITY: 86400 * 7,
        FactType.RELATION: 86400 * 3,
        FactType.ATTRIBUTE: 86400 * 1,
        FactType.EVENT: 86400 * 0.5,
        FactType.RULE: 86400 * 14,
        FactType.PROCEDURE: 86400 * 30,
    }

    def __init__(
        self,
        agent_id: str,
        decay_engine: _PheromoneDecay,
        trail_strategy: TrailStrategy,
        trail_reinforcement_gain: float,
        max_trails: int,
    ):
        self.agent_id = agent_id
        self.decay_engine = decay_engine
        self.trail_strategy = trail_strategy
        self.trail_reinforcement_gain = trail_reinforcement_gain
        self.max_trails = max_trails

        self._lock = threading.RLock()
        self._facts: Dict[str, TypedFact] = {}
        self._trails: Dict[str, PheromoneTrail] = {}
        self._scope_constraints: Dict[str, ScopeConstraint] = {}
        self._fact_type_indices: Dict[FactType, Set[str]] = defaultdict(set)
        self._scope_indices: Dict[ScopeLevel, Set[str]] = defaultdict(set)
        self._tag_indices: Dict[str, Set[str]] = defaultdict(set)

        self._total_verifications: int = 0
        self._total_replications: int = 0

    # ── 事实写入 ────────────────────────────────────────────────

    def deposit_fact(
        self,
        subject: str,
        predicate: str,
        obj: str = "",
        fact_type: FactType = FactType.ENTITY,
        scope: ScopeLevel | None = None,
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
    ) -> TypedFact:
        fact = TypedFact(
            fact_type=fact_type,
            subject=subject,
            predicate=predicate,
            object=obj,
            confidence=confidence,
            initial_confidence=confidence,
            scope=scope or ScopeLevel.LOCAL,
            source_agent=self.agent_id,
            tags=tags or [],
            last_verified_at=time.time(),
            verification_count=1,
        )
        with self._lock:
            self._facts[fact.fact_id] = fact
            self._fact_type_indices[fact.fact_type].add(fact.fact_id)
            self._scope_indices[fact.scope].add(fact.fact_id)
            for tag in fact.tags:
                self._tag_indices[tag].add(fact.fact_id)

        if self.trail_strategy in (TrailStrategy.DEPOSIT_ON_WRITE, TrailStrategy.COMPOUND):
            self._deposit_trail([fact], f"write:{subject}:{predicate}")

        logger.debug(
            "Deposited fact %s (%s) : %s - %s - %s",
            fact.fact_id, fact_type.value, subject, predicate, obj,
        )
        return fact

    # ── 轨迹操作 ────────────────────────────────────────────────

    def _deposit_trail(self, facts: List[TypedFact], path_hint: str = "") -> Optional[PheromoneTrail]:
        if not facts:
            return None
        fact_ids = [f.fact_id for f in facts]
        signature = hashlib.sha256(
            f"{path_hint}:{':'.join(fact_ids)}".encode()
        ).hexdigest()[:16]

        trail = PheromoneTrail(
            fact_ids=fact_ids,
            pheromone_level=1.0,
            deposit_agent=self.agent_id,
            path_signature=signature,
            trail_scope=(
                facts[0].scope
                if len({f.scope for f in facts}) == 1
                else ScopeLevel.FEDERATION
            ),
        )
        with self._lock:
            self._trails[trail.trail_id] = trail
            while len(self._trails) > self.max_trails:
                weakest = min(
                    self._trails.keys(),
                    key=lambda k: self._trails[k].pheromone_level,
                    default=None,
                )
                if weakest:
                    del self._trails[weakest]
        return trail

    def discover_trails(
        self,
        query_signature: Optional[str] = None,
        scope_filter: Optional[ScopeLevel] = None,
        min_pheromone: float = 0.05,
        top_k: int = 20,
    ) -> List[PheromoneTrail]:
        with self._lock:
            trails = list(self._trails.values())
            if scope_filter:
                trails = [t for t in trails if t.trail_scope.value == scope_filter.value]
            if query_signature:
                trails = [t for t in trails if query_signature[:8] in t.path_signature]
            trails = [t for t in trails if t.pheromone_level >= min_pheromone]
            trails.sort(key=lambda t: t.pheromone_level, reverse=True)
            return trails[:top_k]

    def reinforce_trail(self, trail_id: str, gain: Optional[float] = None) -> bool:
        gain = gain or self.trail_reinforcement_gain
        with self._lock:
            trail = self._trails.get(trail_id)
            if trail is None:
                return False
            trail.pheromone_level = min(1.0, trail.pheromone_level + gain)
            trail.last_reinforced_at = time.time()
            trail.reinforcement_count += 1
        return True

    # ── 验证 ────────────────────────────────────────────────────

    def verify_fact(self, fact_id: str, verification_confidence: float = 1.0) -> bool:
        with self._lock:
            fact = self._facts.get(fact_id)
            if fact is None:
                return False
            fact.confidence = max(fact.confidence, fact.initial_confidence * verification_confidence)
            fact.last_verified_at = time.time()
            fact.verification_count += 1
            self._total_verifications += 1
            for trail in self._trails.values():
                if fact_id in trail.fact_ids:
                    self.reinforce_trail(trail.trail_id)
        return True

    def verify_batch(self, fact_ids: List[str], verification_confidence: float = 1.0) -> int:
        return sum(1 for fid in fact_ids if self.verify_fact(fid, verification_confidence))

    # ── 衰减应用 ────────────────────────────────────────────────

    def apply_decay(self, current_time: Optional[float] = None) -> List[DecayState]:
        now = current_time or time.time()
        states: List[DecayState] = []
        with self._lock:
            for fact in list(self._facts.values()):
                state = self.decay_engine.compute(fact, now)
                fact.confidence = state.current_confidence
                states.append(state)
            for trail in list(self._trails.values()):
                trail.pheromone_level = self.decay_engine.evaporate_trail(trail, now)
        expired = sum(1 for s in states if s.current_confidence < 0.05)
        if expired:
            logger.info("Decay applied: %d facts below threshold", expired)
        return states

    def get_expired_facts(self, threshold: float = 0.05) -> List[TypedFact]:
        with self._lock:
            return [f for f in self._facts.values() if f.confidence < threshold]

    def prune_expired(self, threshold: float = 0.01) -> int:
        removed = 0
        with self._lock:
            expired_ids = [fid for fid, f in self._facts.items() if f.confidence < threshold]
            for fid in expired_ids:
                fact = self._facts.pop(fid)
                self._fact_type_indices[fact.fact_type].discard(fid)
                self._scope_indices[fact.scope].discard(fid)
                for tag in fact.tags:
                    self._tag_indices[tag].discard(fid)
                removed += 1
        logger.info("Pruned %d expired facts (threshold=%.3f)", removed, threshold)
        return removed

    # ── 范围约束复制 ────────────────────────────────────────────

    def register_scope_constraint(
        self,
        source_scope: ScopeLevel,
        target_scopes: List[ScopeLevel],
        requires_verification: bool = True,
        max_replication_depth: int = 3,
    ) -> ScopeConstraint:
        constraint = ScopeConstraint(
            source_scope=source_scope,
            target_scopes=target_scopes,
            requires_verification=requires_verification,
            max_replication_depth=max_replication_depth,
        )
        with self._lock:
            self._scope_constraints[constraint.constraint_id] = constraint
        return constraint

    def replicate_fact(self, fact_id: str, target_scope: ScopeLevel) -> Optional[TypedFact]:
        with self._lock:
            fact = self._facts.get(fact_id)
            if fact is None:
                return None
            matching_constraints = [
                c for c in self._scope_constraints.values()
                if c.source_scope == fact.scope and target_scope in c.target_scopes
            ]
            if matching_constraints:
                constraint = matching_constraints[0]
                if constraint.requires_verification and fact.verification_count < 1:
                    return None
                if constraint.replication_count >= constraint.max_replication_depth:
                    return None
                constraint.replication_count += 1
            else:
                scope_rank = {
                    ScopeLevel.LOCAL: 0, ScopeLevel.GROUP: 1,
                    ScopeLevel.DOMAIN: 2, ScopeLevel.FEDERATION: 3,
                    ScopeLevel.PUBLIC: 4,
                }
                if abs(scope_rank.get(target_scope, 0) - scope_rank.get(fact.scope, 0)) > 1:
                    return None

            replicated = TypedFact(
                fact_type=fact.fact_type,
                subject=fact.subject,
                predicate=fact.predicate,
                object=fact.object,
                confidence=fact.confidence * 0.95,
                initial_confidence=fact.initial_confidence,
                scope=target_scope,
                source_agent=self.agent_id,
                tags=list(fact.tags),
            )
            self._facts[replicated.fact_id] = replicated
            self._fact_type_indices[replicated.fact_type].add(replicated.fact_id)
            self._scope_indices[replicated.scope].add(replicated.fact_id)
            for tag in replicated.tags:
                self._tag_indices[tag].add(replicated.fact_id)
            self._total_replications += 1

        logger.debug("Replicated fact %s: %s → %s", fact_id, fact.scope.value, target_scope.value)
        return replicated

    # ── 查询 ────────────────────────────────────────────────────

    def query_facts(
        self,
        fact_type: Optional[FactType] = None,
        scope: Optional[ScopeLevel] = None,
        tags: Optional[List[str]] = None,
        min_confidence: float = 0.0,
        subjects: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[TypedFact]:
        with self._lock:
            candidates: Set[str] = set()
            if fact_type:
                candidates = self._fact_type_indices.get(fact_type, set()).copy()
            elif scope:
                candidates = self._scope_indices.get(scope, set()).copy()
            elif tags:
                for tag in tags:
                    candidates |= self._tag_indices.get(tag, set())
            else:
                candidates = set(self._facts.keys())

            if fact_type:
                candidates &= self._fact_type_indices.get(fact_type, set())
            if scope:
                candidates &= self._scope_indices.get(scope, set())
            if tags:
                for tag in tags:
                    candidates &= self._tag_indices.get(tag, set())

            results = []
            for fid in candidates:
                fact = self._facts.get(fid)
                if fact is None:
                    continue
                if fact.confidence < min_confidence:
                    continue
                if subjects and fact.subject not in subjects:
                    continue
                results.append(fact)
            results.sort(key=lambda f: f.confidence, reverse=True)
            return results[:limit]

    def get_fact(self, fact_id: str) -> Optional[TypedFact]:
        return self._facts.get(fact_id)

    def get_trail(self, trail_id: str) -> Optional[PheromoneTrail]:
        return self._trails.get(trail_id)

    # ── 统计 ────────────────────────────────────────────────────

    def snapshot(self) -> StigmergyStats:
        with self._lock:
            facts = list(self._facts.values())
            trails = list(self._trails.values())
            active_facts = [f for f in facts if f.confidence >= 0.05]
            active_trails = [t for t in trails if t.pheromone_level >= 0.01]
            return StigmergyStats(
                total_facts=len(facts),
                active_facts=len(active_facts),
                expired_facts=len(facts) - len(active_facts),
                total_trails=len(trails),
                active_trails=len(active_trails),
                evaporated_trails=len(trails) - len(active_trails),
                total_verifications=self._total_verifications,
                total_replications=self._total_replications,
                avg_confidence=round(
                    np.mean([f.confidence for f in facts]) if facts else 0.0, 4
                ),
                avg_pheromone=round(
                    np.mean([t.pheromone_level for t in trails]) if trails else 0.0, 4
                ),
            )

    def scope_distribution(self) -> Dict[str, int]:
        return {sl.value: len(self._scope_indices.get(sl, set())) for sl in ScopeLevel}

    def type_distribution(self) -> Dict[str, int]:
        return {ft.value: len(self._fact_type_indices.get(ft, set())) for ft in FactType}

    def constraint_count(self) -> int:
        return len(self._scope_constraints)


# ══════════════════════════════════════════════════════════════════════
# ── Facade：StigmergyLayer ──────────────────────────────────────────
# ══════════════════════════════════════════════════════════════════════

    def statistics_dict(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {
            "facts_total": snap.total_facts, "facts_active": snap.active_facts,
            "facts_expired": snap.expired_facts, "trails_total": snap.total_trails,
            "trails_active": snap.active_trails, "trails_evaporated": snap.evaporated_trails,
            "verifications_total": snap.total_verifications,
            "replications_total": snap.total_replications,
            "avg_fact_confidence": snap.avg_confidence,
            "avg_pheromone_level": snap.avg_pheromone,
            "scope_constraints": self.constraint_count(),
            "fact_type_distribution": self.type_distribution(),
            "scope_distribution": self.scope_distribution()}




class StigmergyLayer:
    """蚁群信息素联邦知识层。

    基于生物 Stigmergy 模型实现去中心化知识共享：
      - Agent 创建/验证事实时留下信息素轨迹
      - 其他 Agent 沿轨迹发现知识
      - 未验证的知识随时间自然衰减（蒸发）
      - 重复验证强化轨迹
      - 知识复制受作用域限制
    """

    def __init__(
        self,
        agent_id: str = "",
        default_scope: ScopeLevel = ScopeLevel.LOCAL,
        decay_model: DecayModel = DecayModel.EXPONENTIAL,
        trail_strategy: TrailStrategy = TrailStrategy.COMPOUND,
        pheromone_evaporation_rate: float = 0.01,
        trail_reinforcement_gain: float = 0.15,
        max_trails: int = 500,
    ):
        self.agent_id = agent_id or f"agent_{uuid.uuid4().hex[:8]}"
        self.default_scope = default_scope
        self.trail_strategy = trail_strategy

        self._decay = _PheromoneDecay(
            model=decay_model,
            evaporation_rate=pheromone_evaporation_rate,
        )
        self._aggregator = _TrailAggregator(
            agent_id=self.agent_id,
            decay_engine=self._decay,
            trail_strategy=trail_strategy,
            trail_reinforcement_gain=trail_reinforcement_gain,
            max_trails=max_trails,
        )

        logger.info(
            "StigmergyLayer initialized (agent=%s, scope=%s, decay=%s)",
            self.agent_id, default_scope.value, decay_model.value,
        )

    # ── 事实写入 ──────────────────────────────────────────────────

    def deposit_fact(
        self,
        subject: str,
        predicate: str,
        obj: str = "",
        fact_type: FactType = FactType.ENTITY,
        scope: Optional[ScopeLevel] = None,
        confidence: float = 1.0,
        tags: Optional[List[str]] = None,
    ) -> TypedFact:
        return self._aggregator.deposit_fact(
            subject, predicate, obj, fact_type,
            scope or self.default_scope, confidence, tags,
        )

    # ── 轨迹操作 ──────────────────────────────────────────────────

    def discover_trails(
        self,
        query_signature: Optional[str] = None,
        scope_filter: Optional[ScopeLevel] = None,
        min_pheromone: float = 0.05,
        top_k: int = 20,
    ) -> List[PheromoneTrail]:
        return self._aggregator.discover_trails(query_signature, scope_filter, min_pheromone, top_k)

    def reinforce_trail(self, trail_id: str, gain: Optional[float] = None) -> bool:
        return self._aggregator.reinforce_trail(trail_id, gain)

    # ── 验证 ──────────────────────────────────────────────────────

    def verify_fact(self, fact_id: str, verification_confidence: float = 1.0) -> bool:
        return self._aggregator.verify_fact(fact_id, verification_confidence)

    def verify_batch(self, fact_ids: List[str], verification_confidence: float = 1.0) -> int:
        return self._aggregator.verify_batch(fact_ids, verification_confidence)

    # ── 衰减 ──────────────────────────────────────────────────────

    def apply_decay(self, current_time: Optional[float] = None) -> List[DecayState]:
        return self._aggregator.apply_decay(current_time)

    def get_expired_facts(self, threshold: float = 0.05) -> List[TypedFact]:
        return self._aggregator.get_expired_facts(threshold)

    def prune_expired(self, threshold: float = 0.01) -> int:
        return self._aggregator.prune_expired(threshold)

    # ── 范围约束复制 ─────────────────────────────────────────────

    def register_scope_constraint(
        self,
        source_scope: ScopeLevel,
        target_scopes: List[ScopeLevel],
        requires_verification: bool = True,
        max_replication_depth: int = 3,
    ) -> ScopeConstraint:
        return self._aggregator.register_scope_constraint(
            source_scope, target_scopes, requires_verification, max_replication_depth,
        )

    def replicate_fact(self, fact_id: str, target_scope: ScopeLevel) -> Optional[TypedFact]:
        return self._aggregator.replicate_fact(fact_id, target_scope)

    # ── 查询 ──────────────────────────────────────────────────────

    def query_facts(
        self,
        fact_type: Optional[FactType] = None,
        scope: Optional[ScopeLevel] = None,
        tags: Optional[List[str]] = None,
        min_confidence: float = 0.0,
        subjects: Optional[List[str]] = None,
        limit: int = 50,
    ) -> List[TypedFact]:
        return self._aggregator.query_facts(fact_type, scope, tags, min_confidence, subjects, limit)

    def get_fact(self, fact_id: str) -> Optional[TypedFact]:
        return self._aggregator.get_fact(fact_id)

    def get_trail(self, trail_id: str) -> Optional[PheromoneTrail]:
        return self._aggregator.get_trail(trail_id)

    # ── 统计 ──────────────────────────────────────────────────────

    def snapshot(self) -> StigmergyStats:
        return self._aggregator.snapshot()

    def statistics(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {
            "facts_total": snap.total_facts,
            "facts_active": snap.active_facts,
            "facts_expired": snap.expired_facts,
            "trails_total": snap.total_trails,
            "trails_active": snap.active_trails,
            "trails_evaporated": snap.evaporated_trails,
            "verifications_total": snap.total_verifications,
            "replications_total": snap.total_replications,
            "avg_fact_confidence": snap.avg_confidence,
            "avg_pheromone_level": snap.avg_pheromone,
            "agent_id": self.agent_id,
            "default_scope": self.default_scope.value,
            "trail_strategy": self.trail_strategy.value,
            "scope_constraints": self._aggregator.constraint_count(),
            "fact_type_distribution": self._aggregator.type_distribution(),
            "scope_distribution": self._aggregator.scope_distribution(),
            "decay": self._decay.statistics(),
        }

    def reset(self) -> None:
        self._aggregator = _TrailAggregator(
            agent_id=self.agent_id,
            decay_engine=self._decay,
            trail_strategy=self.trail_strategy,
            trail_reinforcement_gain=self._aggregator.trail_reinforcement_gain,
            max_trails=self._aggregator.max_trails,
        )
        logger.info("StigmergyLayer reset")


# ══════════════════════════════════════════════════════════════════════
# ── 向后兼容：ConfidenceDecay（独立衰减工具类）─────────────────────
# ══════════════════════════════════════════════════════════════════════


class ConfidenceDecay:
    """自然信心衰减模型（独立工具类，向后兼容）。

    直接包装 _PheromoneDecay，保持原 API 不变。
    """

    def __init__(
        self,
        model: DecayModel = DecayModel.EXPONENTIAL,
        half_life_seconds: float = 86400.0,
        min_confidence: float = 0.01,
        sigmoid_steepness: float = 0.00005,
        sigmoid_midpoint_seconds: float = 43200.0,
    ):
        self._engine = _PheromoneDecay(
            model=model,
            half_life_seconds=half_life_seconds,
            min_confidence=min_confidence,
            sigmoid_steepness=sigmoid_steepness,
            sigmoid_midpoint_seconds=sigmoid_midpoint_seconds,
        )

    def compute(
        self, fact: TypedFact, current_time: Optional[float] = None
    ) -> DecayState:
        return self._engine.compute(fact, current_time)

    def batch_decay(
        self, facts: List[TypedFact], current_time: Optional[float] = None
    ) -> List[DecayState]:
        return self._engine.batch_decay(facts, current_time)

    def statistics(self) -> Dict[str, Any]:
        return self._engine.statistics()
