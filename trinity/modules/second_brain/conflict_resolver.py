"""
# status: orphan (2026-08-15 audit, not in runtime path)
P8-3: Typed Conflict Resolution Engine (对标 Mnemos + MindMemOS)
=================================================================

核心设计（基于 Mnemos 开源记忆引擎 + MindMemOS Dreaming）：
  - 六种冲突类型识别：事实冲突 / 偏好冲突 / 时效冲突 / 来源冲突 /
    范围冲突 / 语义等价
  - supersedes 链管理：新版归档旧版并建立替代关系，保证检索上下文干净
  - 自动裁决规则引擎：依据时效/来源权威性/特异性优先级自动裁决
  - 冲突消解审计日志：完整记录所有冲突和裁决过程

Mnemos 关键指标：
  - MemoryAgentBench 冲突消解子集：Mnemos 12% vs Mem0 7%
  - Typed conflict resolution 优于无类型冲突检测
  - 冲突判断从每次查询的临场推理前移为持久记忆状态

MindMemOS Dreaming 设计：
  - 识别重复、冲突和演化关系
  - 归档被新事实取代的旧记忆，保留 supersedes 关系
  - 整理结果变为持久记忆状态，不把判断压力留给每次回答

Reference: Mnemos (dev.to, 2026) / MindMemOS (Huawei Noah's Ark, 2026)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# ── 枚举与常量 ───────────────────────────────────────────────────────


class ConflictType(Enum):
    """六种冲突类型。"""
    FACTUAL = "factual"               # 事实冲突：客观事实矛盾
    PREFERENCE = "preference"         # 偏好冲突：用户偏好变化
    TEMPORAL = "temporal"             # 时效冲突：新版信息覆盖旧版
    SOURCE = "source"                 # 来源冲突：不同来源给出矛盾信息
    SCOPE = "scope"                   # 范围冲突：局部 vs 全局的不一致
    SEMANTIC_EQUIVALENCE = "semantic_equivalence"  # 语义等价：同一事实的不同表述


class ResolutionStrategy(Enum):
    """裁决策略。"""
    MOST_RECENT = "most_recent"               # 最新优先
    HIGHEST_AUTHORITY = "highest_authority"    # 最高权威
    MOST_SPECIFIC = "most_specific"            # 最具体（范围最小）
    MAJORITY_CONSENSUS = "majority_consensus"  # 多数共识
    USER_OVERRIDE = "user_override"            # 用户显式覆盖
    MANUAL_REVIEW = "manual_review"            # 需要人工审核
    MERGE = "merge"                           # 合并（语义等价）
    KEEP_BOTH = "keep_both"                    # 保留两者（互不冲突）


class ConflictSeverity(Enum):
    """冲突严重程度。"""
    TRIVIAL = "trivial"         # 无实际影响
    MINOR = "minor"             # 轻微不一致
    MODERATE = "moderate"       # 需要裁决
    MAJOR = "major"             # 影响决策
    CRITICAL = "critical"       # 必须立即解决


class AuditAction(Enum):
    """审计日志动作类型。"""
    CONFLICT_DETECTED = "conflict_detected"         # 检测到冲突
    RESOLUTION_APPLIED = "resolution_applied"       # 裁决已应用
    SUPERSEDES_CREATED = "supersedes_created"       # supersedes关系已建立
    ARCHIVED = "archived"                           # 已归档
    RESTORED = "restored"                           # 已恢复
    MERGED = "merged"                               # 已合并


class MemoryRecordState(Enum):
    """记忆记录状态。"""
    ACTIVE = "active"             # 活跃
    ARCHIVED = "archived"         # 已归档（被取代但可追溯）
    SUPERSEDED = "superseded"     # 已被取代
    MERGED = "merged"             # 已合并到其他记录
    CONFLICTING = "conflicting"   # 存在冲突待解决
    DEPRECATED = "deprecated"     # 已废弃


# ── 数据结构 ─────────────────────────────────────────────────────────


@dataclass
class MemoryFact:
    """记忆事实。

    Args:
        fact_id: 事实ID
        content: 事实内容
        entity_type: 关联实体类型
        entity_id: 关联实体ID
        timestamp: 记录时间
        source: 来源标识
        authority: 来源权威性 (0-1)
        scope: 适用范围 ('global', 'session', 'task', 'utterance')
        specificity: 特异性 (0-1, 越高越具体)
        metadata: 附加元数据
    """
    fact_id: str
    content: str
    entity_type: str = "general"
    entity_id: str = ""
    timestamp: float = field(default_factory=time.time)
    source: str = "unknown"
    authority: float = 0.5
    scope: str = "global"
    specificity: float = 0.5
    metadata: Dict[str, Any] = field(default_factory=dict)
    state: MemoryRecordState = MemoryRecordState.ACTIVE


@dataclass
class Conflict:
    """冲突记录。

    Args:
        conflict_id: 冲突ID
        conflict_type: 冲突类型
        fact_a: 事实A
        fact_b: 事实B
        description: 冲突描述
        severity: 严重程度
        resolved: 是否已解决
        resolution_strategy: 裁决策略
        winner_fact_id: 胜出事实ID
        supersedes_chain: supersedes链
        detected_at: 检测时间
        resolved_at: 解决时间
    """
    conflict_id: str
    conflict_type: ConflictType
    fact_a: MemoryFact
    fact_b: MemoryFact
    description: str
    severity: ConflictSeverity = ConflictSeverity.MODERATE
    resolved: bool = False
    resolution_strategy: Optional[ResolutionStrategy] = None
    winner_fact_id: str = ""
    supersedes_chain: List[str] = field(default_factory=list)
    detected_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None


@dataclass
class SupersedesLink:
    """supersedes 替代关系链节点。

    Args:
        link_id: 链接ID
        older_fact_id: 被取代的旧事实ID
        newer_fact_id: 取代的新事实ID
        reason: 取代原因
        conflict_id: 关联的冲突ID
        created_at: 创建时间
    """
    link_id: str
    older_fact_id: str
    newer_fact_id: str
    reason: str
    conflict_id: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class AuditEntry:
    """审计日志条目。

    Args:
        entry_id: 条目ID
        action: 动作类型
        entity_ids: 涉及的实体/事实ID
        details: 详细信息
        operator: 操作者（'auto' 表示自动裁决）
        timestamp: 时间戳
    """
    entry_id: str
    action: AuditAction
    entity_ids: List[str] = field(default_factory=list)
    details: str = ""
    operator: str = "auto"
    timestamp: float = field(default_factory=time.time)


# ── 冲突类型识别器 ──────────────────────────────────────────────────


class ConflictDetector:
    """六种冲突类型的检测器。"""

    # 事实冲突关键词
    FACTUAL_CONTRADICTIONS = [
        ("是", "不是"), ("有", "没有"), ("可以", "不能"),
        ("true", "false"), ("yes", "no"),
    ]

    # 偏好变化信号词
    PREFERENCE_SIGNALS = [
        "不再", "换了", "改了", "现在喜欢", "以前",
        "prefer", "switched", "changed", "now",
    ]

    # 时效性指标词
    TEMPORAL_MARKERS = [
        "最新", "更新", "截止", "目前", "现在",
        "latest", "updated", "current", "as of",
    ]

    def detect(self, fact_a: MemoryFact, fact_b: MemoryFact) -> Optional[Conflict]:
        """检测两个事实之间是否存在冲突。

        Args:
            fact_a: 事实A
            fact_b: 事实B

        Returns:
            Conflict 对象，无冲突返回 None
        """
        conflict_type = self._classify_conflict(fact_a, fact_b)
        if conflict_type is None:
            return None

        severity = self._assess_severity(conflict_type, fact_a, fact_b)
        description = self._describe_conflict(conflict_type, fact_a, fact_b)

        conflict_id = f"CFL-{uuid.uuid4().hex[:8]}"

        return Conflict(
            conflict_id=conflict_id,
            conflict_type=conflict_type,
            fact_a=fact_a,
            fact_b=fact_b,
            description=description,
            severity=severity,
        )

    def _classify_conflict(self, fact_a: MemoryFact, fact_b: MemoryFact) -> Optional[ConflictType]:
        """分类冲突类型。"""
        content_a = fact_a.content.lower()
        content_b = fact_b.content.lower()

        # 1. 语义等价检测（最高优先级——可能不是真正的冲突）
        if self._is_semantic_equivalent(content_a, content_b, fact_a, fact_b):
            return ConflictType.SEMANTIC_EQUIVALENCE

        # 如果没有实质矛盾，不视为冲突
        if not self._has_substantive_disagreement(content_a, content_b):
            return None

        # 2. 时效冲突：相同实体/来源，但时间戳差异明显
        if fact_a.entity_id == fact_b.entity_id and fact_a.source == fact_b.source:
            time_diff = abs(fact_a.timestamp - fact_b.timestamp)
            if time_diff > 86400:  # 超过1天
                return ConflictType.TEMPORAL

        # 3. 来源冲突：不同来源的矛盾
        if fact_a.source != fact_b.source:
            return ConflictType.SOURCE

        # 4. 范围冲突：scope 不一致
        if fact_a.scope != fact_b.scope:
            return ConflictType.SCOPE

        # 5. 偏好冲突：检测偏好变化信号
        for signal in self.PREFERENCE_SIGNALS:
            if signal in content_b:
                return ConflictType.PREFERENCE

        # 6. 默认：事实冲突
        return ConflictType.FACTUAL

    def _is_semantic_equivalent(
        self, content_a: str, content_b: str, fact_a: MemoryFact, fact_b: MemoryFact
    ) -> bool:
        """检测两条事实是否语义等价（表述不同但含义相同）。"""
        # 简化实现：如果除去停用词后内容高度重叠
        words_a = set(content_a.split())
        words_b = set(content_b.split())
        if not words_a or not words_b:
            return False
        overlap = len(words_a & words_b) / max(len(words_a), len(words_b))
        # 如果重叠度很高且事实属性一致
        if overlap > 0.7 and fact_a.entity_id == fact_b.entity_id:
            return True
        return False

    def _has_substantive_disagreement(self, content_a: str, content_b: str) -> bool:
        """检查是否有实质性的矛盾。"""
        # 仅当文本较短时检测直接矛盾
        if len(content_a) > 500 or len(content_b) > 500:
            return False

        for pos_word, neg_word in self.FACTUAL_CONTRADICTIONS:
            if pos_word in content_a and neg_word in content_b:
                return True
            if pos_word in content_b and neg_word in content_a:
                return True

        return False

    def _assess_severity(
        self, conflict_type: ConflictType, fact_a: MemoryFact, fact_b: MemoryFact
    ) -> ConflictSeverity:
        """评估冲突严重程度。"""
        if conflict_type == ConflictType.SEMANTIC_EQUIVALENCE:
            return ConflictSeverity.TRIVIAL

        if conflict_type == ConflictType.FACTUAL:
            if fact_a.authority > 0.8 and fact_b.authority > 0.8:
                return ConflictSeverity.CRITICAL
            return ConflictSeverity.MAJOR

        if conflict_type == ConflictType.PREFERENCE:
            return ConflictSeverity.MODERATE

        if conflict_type == ConflictType.TEMPORAL:
            return ConflictSeverity.MINOR

        return ConflictSeverity.MODERATE

    def _describe_conflict(
        self, conflict_type: ConflictType, fact_a: MemoryFact, fact_b: MemoryFact
    ) -> str:
        """生成冲突描述。"""
        summaries = {
            ConflictType.FACTUAL: f"Factual conflict: '{fact_a.content[:80]}' vs '{fact_b.content[:80]}'",
            ConflictType.PREFERENCE: f"Preference change: '{fact_a.content[:80]}' -> '{fact_b.content[:80]}'",
            ConflictType.TEMPORAL: f"Temporal update: older '{fact_a.content[:60]}' vs newer '{fact_b.content[:60]}'",
            ConflictType.SOURCE: f"Source conflict: [{fact_a.source}] vs [{fact_b.source}]",
            ConflictType.SCOPE: f"Scope mismatch: {fact_a.scope} vs {fact_b.scope}",
            ConflictType.SEMANTIC_EQUIVALENCE: f"Semantic equivalence: same fact, different phrasing",
        }
        return summaries.get(conflict_type, "Unknown conflict type")


# ── 自动裁决规则引擎 ────────────────────────────────────────────────


class RulingEngine:
    """自动裁决规则引擎。

    依据时效/来源权威性/特异性优先级自动裁决冲突。

    规则优先级（从高到低）：
      1. 时效优先：显著更新的信息覆盖旧信息
      2. 来源权威性优先：高权威来源胜出
      3. 特异性优先：更具体的声明胜出
      4. 多数共识：相同主张更多的一方胜出
    """

    # 时效阈值：超过此时间差认为新信息更有价值
    TEMPORAL_THRESHOLD_SECONDS = 86400  # 1天

    def resolve(self, conflict: Conflict) -> Conflict:
        """自动裁决冲突。

        Args:
            conflict: 待裁决的冲突

        Returns:
            更新后的冲突对象（含裁决结果）
        """
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        # 按冲突类型选择策略
        strategy_handlers = {
            ConflictType.FACTUAL: self._resolve_factual,
            ConflictType.PREFERENCE: self._resolve_preference,
            ConflictType.TEMPORAL: self._resolve_temporal,
            ConflictType.SOURCE: self._resolve_source,
            ConflictType.SCOPE: self._resolve_scope,
            ConflictType.SEMANTIC_EQUIVALENCE: self._resolve_semantic_equivalence,
        }

        handler = strategy_handlers.get(conflict.conflict_type)
        if handler:
            conflict = handler(conflict)

        conflict.resolved = True
        conflict.resolved_at = time.time()
        return conflict

    def _resolve_factual(self, conflict: Conflict) -> Conflict:
        """解决事实冲突：权威性 > 特异性 > 最新。"""
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        # 1. 权威性比较
        if abs(fact_a.authority - fact_b.authority) > 0.2:
            if fact_a.authority > fact_b.authority:
                conflict.winner_fact_id = fact_a.fact_id
                conflict.resolution_strategy = ResolutionStrategy.HIGHEST_AUTHORITY
            else:
                conflict.winner_fact_id = fact_b.fact_id
                conflict.resolution_strategy = ResolutionStrategy.HIGHEST_AUTHORITY
            return conflict

        # 2. 特异性比较
        if abs(fact_a.specificity - fact_b.specificity) > 0.1:
            if fact_a.specificity > fact_b.specificity:
                conflict.winner_fact_id = fact_a.fact_id
                conflict.resolution_strategy = ResolutionStrategy.MOST_SPECIFIC
            else:
                conflict.winner_fact_id = fact_b.fact_id
                conflict.resolution_strategy = ResolutionStrategy.MOST_SPECIFIC
            return conflict

        # 3. 最新优先
        if fact_b.timestamp > fact_a.timestamp:
            conflict.winner_fact_id = fact_b.fact_id
        else:
            conflict.winner_fact_id = fact_a.fact_id
        conflict.resolution_strategy = ResolutionStrategy.MOST_RECENT
        return conflict

    def _resolve_preference(self, conflict: Conflict) -> Conflict:
        """偏好冲突：后写入的覆盖旧版本。"""
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        if fact_b.timestamp > fact_a.timestamp:
            conflict.winner_fact_id = fact_b.fact_id
        else:
            conflict.winner_fact_id = fact_a.fact_id

        conflict.resolution_strategy = ResolutionStrategy.MOST_RECENT
        return conflict

    def _resolve_temporal(self, conflict: Conflict) -> Conflict:
        """时效冲突：新版本覆盖旧版本。"""
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        if fact_b.timestamp > fact_a.timestamp:
            conflict.winner_fact_id = fact_b.fact_id
        else:
            conflict.winner_fact_id = fact_a.fact_id

        conflict.resolution_strategy = ResolutionStrategy.MOST_RECENT
        return conflict

    def _resolve_source(self, conflict: Conflict) -> Conflict:
        """来源冲突：权威性高的来源胜出。"""
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        if fact_a.authority >= fact_b.authority:
            conflict.winner_fact_id = fact_a.fact_id
        else:
            conflict.winner_fact_id = fact_b.fact_id

        conflict.resolution_strategy = ResolutionStrategy.HIGHEST_AUTHORITY
        return conflict

    def _resolve_scope(self, conflict: Conflict) -> Conflict:
        """范围冲突：更具体的范围优先。"""
        scope_order = {"utterance": 4, "task": 3, "session": 2, "global": 1}

        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        if scope_order.get(fact_a.scope, 0) >= scope_order.get(fact_b.scope, 0):
            conflict.winner_fact_id = fact_a.fact_id
        else:
            conflict.winner_fact_id = fact_b.fact_id

        conflict.resolution_strategy = ResolutionStrategy.MOST_SPECIFIC
        return conflict

    def _resolve_semantic_equivalence(self, conflict: Conflict) -> Conflict:
        """语义等价：合并两者（保留内容更完整的一条作为主）。"""
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        # 保留更长的内容作为主要事实
        if len(fact_b.content) > len(fact_a.content):
            conflict.winner_fact_id = fact_b.fact_id
        else:
            conflict.winner_fact_id = fact_a.fact_id

        conflict.resolution_strategy = ResolutionStrategy.MERGE
        return conflict


# ── 主类：类型化冲突消解引擎 ────────────────────────────────────────


class ConflictResolver:
    """类型化冲突消解引擎。

    实现 Mnemos + MindMemOS 的冲突消解机制：
      - 六种冲突类型自动识别
      - supersedes 链管理（新版归档旧版）
      - 自动裁决规则引擎
      - 完整审计日志
    """

    MODULE_ID = "P8-3"
    MODULE_NAME = "Typed Conflict Resolution Engine"
    PAPER_REF = "Mnemos + MindMemOS (2026)"
    PAPER_TITLE = "Typed Conflict Resolution for Memory Agent Systems"

    def __init__(
        self,
        detector: Optional[ConflictDetector] = None,
        ruling_engine: Optional[RulingEngine] = None,
    ):
        """初始化冲突消解引擎。

        Args:
            detector: 冲突检测器
            ruling_engine: 裁决规则引擎
        """
        self._lock = threading.RLock()

        self._detector = detector or ConflictDetector()
        self._ruling_engine = ruling_engine or RulingEngine()

        # 事实存储
        self._facts: Dict[str, MemoryFact] = {}

        # 冲突记录
        self._conflicts: Dict[str, Conflict] = {}
        self._resolved_conflicts: Dict[str, Conflict] = {}

        # supersedes 链
        self._supersedes_links: Dict[str, SupersedesLink] = {}
        self._supersedes_forward: Dict[str, str] = {}   # old -> new
        self._supersedes_backward: Dict[str, List[str]] = defaultdict(list)  # new -> [old]

        # 审计日志
        self._audit_log: deque = deque(maxlen=5000)

        # 统计
        self._total_facts = 0
        self._total_conflicts_detected = 0
        self._total_conflicts_resolved = 0

    # ── 事实管理 ──────────────────────────────────────────────────

    def add_fact(
        self,
        content: str,
        entity_type: str = "general",
        entity_id: str = "",
        source: str = "unknown",
        authority: float = 0.5,
        scope: str = "global",
        specificity: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryFact:
        """添加事实并自动检测冲突。

        Args:
            content: 事实内容
            entity_type: 关联实体类型
            entity_id: 关联实体ID
            source: 来源标识
            authority: 来源权威性 (0-1)
            scope: 适用范围
            specificity: 特异性 (0-1)
            metadata: 元数据

        Returns:
            新创建的 MemoryFact
        """
        with self._lock:
            fact_id = f"FCT-{uuid.uuid4().hex[:12]}"
            fact = MemoryFact(
                fact_id=fact_id,
                content=content,
                entity_type=entity_type,
                entity_id=entity_id,
                source=source,
                authority=authority,
                scope=scope,
                specificity=specificity,
                metadata=metadata or {},
            )
            self._facts[fact_id] = fact
            self._total_facts += 1

            # 自动检测与已有事实的冲突
            self._detect_conflicts_for_fact(fact)

            self._log_audit(
                AuditAction.CONFLICT_DETECTED,
                [fact_id],
                f"Fact added: {content[:100]}",
            )

            return fact

    def _detect_conflicts_for_fact(self, new_fact: MemoryFact) -> List[str]:
        """为新增事实检测与已有事实的冲突。"""
        conflict_ids = []
        for existing_fact in self._facts.values():
            if existing_fact.fact_id == new_fact.fact_id:
                continue
            # 仅检测同实体或同类型的事实
            if new_fact.entity_id and existing_fact.entity_id:
                if new_fact.entity_id != existing_fact.entity_id:
                    continue

            conflict = self._detector.detect(existing_fact, new_fact)
            if conflict:
                self._conflicts[conflict.conflict_id] = conflict
                self._total_conflicts_detected += 1
                conflict_ids.append(conflict.conflict_id)
                logger.debug(
                    "Conflict detected: %s between %s and %s",
                    conflict.conflict_type.value,
                    existing_fact.fact_id[:8],
                    new_fact.fact_id[:8],
                )

        return conflict_ids

    # ── 冲突消解 ──────────────────────────────────────────────────

    def resolve_conflict(self, conflict_id: str) -> Optional[Conflict]:
        """解决指定冲突（自动裁决）。

        Args:
            conflict_id: 冲突ID

        Returns:
            已解决的冲突对象
        """
        with self._lock:
            conflict = self._conflicts.get(conflict_id)
            if not conflict:
                return None
            if conflict.resolved:
                return conflict

            # 自动裁决
            conflict = self._ruling_engine.resolve(conflict)

            # 建立 supersedes 链
            self._create_supersedes_link(conflict)

            # 归档被取代的事实
            loser_id = (
                conflict.fact_b.fact_id
                if conflict.winner_fact_id == conflict.fact_a.fact_id
                else conflict.fact_a.fact_id
            )
            if loser_id in self._facts:
                self._facts[loser_id].state = MemoryRecordState.SUPERSEDED
                self._log_audit(
                    AuditAction.SUPERSEDES_CREATED,
                    [loser_id, conflict.winner_fact_id],
                    f"Archived {loser_id[:8]} superseded by {conflict.winner_fact_id[:8]}",
                )

            # 移至已解决
            self._resolved_conflicts[conflict_id] = conflict
            del self._conflicts[conflict_id]
            self._total_conflicts_resolved += 1

            self._log_audit(
                AuditAction.RESOLUTION_APPLIED,
                [conflict.winner_fact_id],
                f"Resolved {conflict_id}: {conflict.resolution_strategy.value if conflict.resolution_strategy else 'unknown'}",
            )

            return conflict

    def resolve_all_conflicts(self) -> int:
        """批量解决所有待处理冲突。

        Returns:
            解决的冲突数量
        """
        with self._lock:
            count = 0
            for conflict_id in list(self._conflicts.keys()):
                result = self.resolve_conflict(conflict_id)
                if result and result.resolved:
                    count += 1
            return count

    def resolve_conflicts_for_entity(self, entity_id: str) -> int:
        """解决特定实体的所有冲突。"""
        with self._lock:
            relevant = [
                cid for cid, c in self._conflicts.items()
                if c.fact_a.entity_id == entity_id or c.fact_b.entity_id == entity_id
            ]
            count = 0
            for cid in relevant:
                result = self.resolve_conflict(cid)
                if result and result.resolved:
                    count += 1
            return count

    # ── supersedes 链管理 ─────────────────────────────────────────

    def _create_supersedes_link(self, conflict: Conflict) -> None:
        """创建 supersedes 替代关系。"""
        fact_a = conflict.fact_a
        fact_b = conflict.fact_b

        if conflict.winner_fact_id == fact_a.fact_id:
            older_id = fact_b.fact_id
            newer_id = fact_a.fact_id
        else:
            older_id = fact_a.fact_id
            newer_id = fact_b.fact_id

        link_id = f"SPR-{uuid.uuid4().hex[:8]}"
        link = SupersedesLink(
            link_id=link_id,
            older_fact_id=older_id,
            newer_fact_id=newer_id,
            reason=f"Resolved {conflict.conflict_type.value} conflict via {conflict.resolution_strategy.value if conflict.resolution_strategy else 'auto'}",
            conflict_id=conflict.conflict_id,
        )
        self._supersedes_links[link_id] = link
        self._supersedes_forward[older_id] = newer_id
        self._supersedes_backward[newer_id].append(older_id)

    def get_current_fact(self, fact_id: str) -> Optional[MemoryFact]:
        """追溯 supersedes 链获取当前有效版本。

        如果该事实已被取代，沿链找到最新版本。
        """
        with self._lock:
            current_id = fact_id
            visited = set()
            while current_id in self._supersedes_forward:
                if current_id in visited:
                    break  # 防止环路
                visited.add(current_id)
                current_id = self._supersedes_forward[current_id]
            return self._facts.get(current_id)

    def get_supersedes_history(self, fact_id: str) -> List[MemoryFact]:
        """获取事实的完整替代历史（从最旧到最新）。"""
        with self._lock:
            # 先回溯到最旧版本
            root_id = fact_id
            for older_id, newer_id in self._supersedes_forward.items():
                if newer_id == root_id:
                    root_id = older_id
                    break

            # 沿链正向遍历
            history = []
            current = root_id
            visited = set()
            while current:
                if current in visited:
                    break
                visited.add(current)
                fact = self._facts.get(current)
                if fact:
                    history.append(fact)
                current = self._supersedes_forward.get(current, "")

            return history

    def restore_fact(self, fact_id: str) -> bool:
        """恢复一个被取代的事实（撤销 supersedes 关系）。"""
        with self._lock:
            fact = self._facts.get(fact_id)
            if not fact or fact.state != MemoryRecordState.SUPERSEDED:
                return False

            fact.state = MemoryRecordState.ACTIVE

            # 移除对应的 supersedes 链接
            if fact_id in self._supersedes_forward:
                del self._supersedes_forward[fact_id]

            self._log_audit(AuditAction.RESTORED, [fact_id], "Restored from superseded state")
            return True

    # ── 查询接口（干净上下文）──────────────────────────────────────

    def get_active_facts(
        self,
        entity_id: Optional[str] = None,
        entity_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[MemoryFact]:
        """获取活跃事实（不含已解决冲突和 superseded 事实）。

        对应 MindMemOS 设计：消解后的检索上下文干净无歧义。
        """
        with self._lock:
            results = []
            for fact in self._facts.values():
                if fact.state != MemoryRecordState.ACTIVE:
                    continue
                if entity_id and fact.entity_id != entity_id:
                    continue
                if entity_type and fact.entity_type != entity_type:
                    continue
                results.append(fact)

            results.sort(key=lambda f: f.timestamp, reverse=True)
            return results[:limit]

    def get_fact(self, fact_id: str) -> Optional[MemoryFact]:
        """获取事实（包含已归档的）。"""
        with self._lock:
            return self._facts.get(fact_id)

    # ── 审计日志 ──────────────────────────────────────────────────

    def _log_audit(
        self, action: AuditAction, entity_ids: List[str], details: str
    ) -> None:
        """记录审计日志。"""
        entry = AuditEntry(
            entry_id=f"AUD-{uuid.uuid4().hex[:8]}",
            action=action,
            entity_ids=entity_ids,
            details=details,
        )
        self._audit_log.append(entry)

    def get_audit_log(
        self,
        action: Optional[AuditAction] = None,
        limit: int = 100,
    ) -> List[AuditEntry]:
        """查询审计日志。"""
        with self._lock:
            entries = list(self._audit_log)
            if action:
                entries = [e for e in entries if e.action == action]
            return entries[-limit:]

    def export_audit_trail(self) -> List[Dict[str, Any]]:
        """导出完整审计轨迹（JSON 可序列化）。"""
        with self._lock:
            return [
                {
                    "entry_id": e.entry_id,
                    "action": e.action.value,
                    "entity_ids": e.entity_ids,
                    "details": e.details,
                    "operator": e.operator,
                    "timestamp": e.timestamp,
                }
                for e in self._audit_log
            ]

    # ── 统计与状态 ────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """返回运行时统计指标。"""
        with self._lock:
            active = sum(1 for f in self._facts.values() if f.state == MemoryRecordState.ACTIVE)
            superseded = sum(1 for f in self._facts.values() if f.state == MemoryRecordState.SUPERSEDED)
            return {
                "module": self.MODULE_NAME,
                "paper": self.PAPER_REF,
                "total_facts": len(self._facts),
                "active_facts": active,
                "superseded_facts": superseded,
                "pending_conflicts": len(self._conflicts),
                "resolved_conflicts": len(self._resolved_conflicts),
                "total_conflicts_detected": self._total_conflicts_detected,
                "total_conflicts_resolved": self._total_conflicts_resolved,
                "supersedes_links": len(self._supersedes_links),
                "audit_entries": len(self._audit_log),
                "conflicts_by_type": self._conflict_type_distribution(),
            }

    def _conflict_type_distribution(self) -> Dict[str, int]:
        """各类型冲突数量分布。"""
        dist: Dict[str, int] = defaultdict(int)
        for c in self._conflicts.values():
            dist[c.conflict_type.value] += 1
        for c in self._resolved_conflicts.values():
            dist[c.conflict_type.value] += 1
        return dict(dist)

    def reset(self) -> None:
        """重置所有状态。"""
        with self._lock:
            self._facts.clear()
            self._conflicts.clear()
            self._resolved_conflicts.clear()
            self._supersedes_links.clear()
            self._supersedes_forward.clear()
            self._supersedes_backward.clear()
            self._audit_log.clear()
            self._total_facts = 0
            self._total_conflicts_detected = 0
            self._total_conflicts_resolved = 0
            logger.info("ConflictResolver reset complete")


# ── 便捷工厂 ────────────────────────────────────────────────────────


def create_conflict_resolver() -> ConflictResolver:
    """创建预配置的冲突消解引擎。"""
    return ConflictResolver(
        detector=ConflictDetector(),
        ruling_engine=RulingEngine(),
    )
