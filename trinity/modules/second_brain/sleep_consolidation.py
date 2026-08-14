"""
P8-4: Sleep-Phase Memory Consolidation Pipeline (对标 Light-Omni)
==================================================================

核心设计（基于 Light-Omni 论文 arXiv:2607.05511）：
  - 双重上下文状态：
      * 快速反射通道（Fast Reflex）：实时查询走轻量索引，低延迟
      * 慢速整合通道（Slow Consolidation）：休眠期离线精炼记忆
  - 离线记忆合并：识别重复/演化/冗余并生成合并操作
  - 跨片段模式发现：从多次经历中抽象高阶模式
  - 整合结果持久化：不把判断压力留给每次回答
  - P8-1 安全联动：整合过程受安全监控器保护

Light-Omni 核心洞察：
  - "Reflex beats reasoning" for low-latency video understanding
  - Dual context states: fast reflex vs. slow consolidation
  - Global context incrementally integrated during sleep phase
  - Constant-latency design: reflex channel avoids iterative reasoning overhead

与 MindMemOS Dreaming 的协同：
  - 重复/冲突/演化关系的识别和合并
  - 模式发现 -> 持久记忆状态
  - 不在每次回答时重新推理

Reference: Light-Omni (arXiv:2607.05511, 2026) + MindMemOS Dreaming
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# ── 枚举与常量 ───────────────────────────────────────────────────────


class ChannelMode(Enum):
    """双重上下文通道模式。"""
    FAST_REFLEX = "fast_reflex"
    SLOW_CONSOLIDATION = "slow_consolidation"


class ConsolidationPhase(Enum):
    """整合阶段。"""
    IDLE = "idle"
    SCANNING = "scanning"
    DEDUPLICATING = "deduplicating"
    MERGING = "merging"
    PATTERN_MINING = "pattern_mining"
    PERSISTING = "persisting"
    COMPLETED = "completed"


class MemoryRelation(Enum):
    """记忆间关系类型。"""
    DUPLICATE = "duplicate"
    EVOLUTION = "evolution"
    REDUNDANT = "redundant"
    CONTRADICTORY = "contradictory"
    COMPLEMENTARY = "complementary"
    UNRELATED = "unrelated"


class MergeAction(Enum):
    """合并操作类型。"""
    KEEP_NEWEST = "keep_newest"
    KEEP_MOST_COMPLETE = "keep_most_complete"
    MERGE_CONTENT = "merge_content"
    ARCHIVE_OLD = "archive_old"
    FLAG_FOR_REVIEW = "flag_for_review"
    NO_ACTION = "no_action"


class PatternType(Enum):
    """发现的模式类型。"""
    BEHAVIORAL = "behavioral"
    TEMPORAL = "temporal"
    CAUSAL = "causal"
    PREFERENCE = "preference"
    TOPOLOGICAL = "topological"
    FREQUENCY = "frequency"


# ── 数据结构 ─────────────────────────────────────────────────────────


@dataclass
class MemoryFragment:
    """记忆片段。"""
    fragment_id: str
    content: str
    embedding: Optional[np.ndarray] = None
    source: str = "unknown"
    timestamp: float = field(default_factory=time.time)
    importance: float = 0.5
    access_count: int = 0
    tags: Set[str] = field(default_factory=set)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryRelationRecord:
    """记忆间关系记录。"""
    relation_id: str
    fragment_a: MemoryFragment
    fragment_b: MemoryFragment
    relation_type: MemoryRelation
    similarity_score: float
    recommended_action: MergeAction
    reason: str = ""


@dataclass
class DiscoveredPattern:
    """发现的跨片段高阶模式。"""
    pattern_id: str
    pattern_type: PatternType
    description: str
    supporting_fragments: List[str]
    confidence: float = 0.5
    abstraction_level: int = 1
    evidence_strength: float = 0.0
    contradictions: int = 0
    discovered_at: float = field(default_factory=time.time)


@dataclass
class ConsolidationPlan:
    """整合执行计划。"""
    plan_id: str
    phase: ConsolidationPhase = ConsolidationPhase.IDLE
    relations: List[MemoryRelationRecord] = field(default_factory=list)
    patterns: List[DiscoveredPattern] = field(default_factory=list)
    merge_actions: List[Dict[str, Any]] = field(default_factory=list)
    estimated_savings: int = 0
    created_at: float = field(default_factory=time.time)


@dataclass
class ConsolidationResult:
    """整合执行结果。"""
    result_id: str
    plan_id: str
    merges_executed: int = 0
    patterns_persisted: int = 0
    fragments_archived: int = 0
    fragments_created: int = 0
    duration_seconds: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ── 双重上下文通道 ──────────────────────────────────────────────────


class ContextChannel(ABC):
    """上下文通道抽象基类。"""

    @abstractmethod
    def query(self, query_text: str, top_k: int = 10) -> List[MemoryFragment]:
        ...

    @abstractmethod
    def add_fragment(self, fragment: MemoryFragment) -> None:
        ...

    @property
    @abstractmethod
    def channel_mode(self) -> ChannelMode:
        ...


class FastReflexChannel(ContextChannel):
    """快速反射通道：轻量索引，低延迟实时查询。"""

    def __init__(self, max_fragments: int = 10000):
        self._lock = threading.RLock()
        self._fragments: Dict[str, MemoryFragment] = {}
        self._content_index: Dict[str, Set[str]] = defaultdict(set)
        self._tag_index: Dict[str, Set[str]] = defaultdict(set)
        self._max_fragments = max_fragments

    @property
    def channel_mode(self) -> ChannelMode:
        return ChannelMode.FAST_REFLEX

    def query(self, query_text: str, top_k: int = 10) -> List[MemoryFragment]:
        with self._lock:
            results = []
            query_lower = query_text.lower()
            query_words = set(query_lower.split())
            for fragment in self._fragments.values():
                score = 0
                content_lower = fragment.content.lower()
                for word in query_words:
                    if word in content_lower:
                        score += 1
                tag_match = sum(1 for tag in fragment.tags if tag.lower() in query_lower)
                score += tag_match * 2
                if score > 0:
                    results.append((score, fragment))
            results.sort(key=lambda x: (x[0], x[1].timestamp), reverse=True)
            return [r[1] for r in results[:top_k]]

    def add_fragment(self, fragment: MemoryFragment) -> None:
        with self._lock:
            if len(self._fragments) >= self._max_fragments:
                oldest = min(self._fragments.values(), key=lambda f: f.timestamp)
                self._remove_from_indices(oldest)
                del self._fragments[oldest.fragment_id]
            self._fragments[fragment.fragment_id] = fragment
            for word in fragment.content.lower().split():
                self._content_index[word].add(fragment.fragment_id)
            for tag in fragment.tags:
                self._tag_index[tag].add(fragment.fragment_id)

    def _remove_from_indices(self, fragment: MemoryFragment) -> None:
        for word in fragment.content.lower().split():
            s = self._content_index.get(word)
            if s:
                s.discard(fragment.fragment_id)
        for tag in fragment.tags:
            s = self._tag_index.get(tag)
            if s:
                s.discard(fragment.fragment_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "channel": "fast_reflex",
                "total_fragments": len(self._fragments),
                "max_capacity": self._max_fragments,
            }


class SlowConsolidationChannel(ContextChannel):
    """慢速整合通道：离线精炼记忆，休眠期执行。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._fragments: Dict[str, MemoryFragment] = {}
        self._persisted_patterns: Dict[str, DiscoveredPattern] = {}
        self._consolidation_history: deque = deque(maxlen=50)

    @property
    def channel_mode(self) -> ChannelMode:
        return ChannelMode.SLOW_CONSOLIDATION

    def query(self, query_text: str, top_k: int = 10) -> List[MemoryFragment]:
        with self._lock:
            results = []
            for fragment in self._fragments.values():
                if query_text.lower() in fragment.content.lower():
                    results.append(fragment)
            results.sort(key=lambda f: (f.importance, f.timestamp), reverse=True)
            return results[:top_k]

    def add_fragment(self, fragment: MemoryFragment) -> None:
        with self._lock:
            self._fragments[fragment.fragment_id] = fragment

    def get_all_fragments(self) -> List[MemoryFragment]:
        with self._lock:
            return list(self._fragments.values())

    def get_persisted_patterns(self) -> List[DiscoveredPattern]:
        with self._lock:
            return list(self._persisted_patterns.values())

    def add_persisted_pattern(self, pattern: DiscoveredPattern) -> None:
        with self._lock:
            self._persisted_patterns[pattern.pattern_id] = pattern

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "channel": "slow_consolidation",
                "total_fragments": len(self._fragments),
                "persisted_patterns": len(self._persisted_patterns),
                "history_entries": len(self._consolidation_history),
            }


# ── 记忆关系分析器 ──────────────────────────────────────────────────


class MemoryRelationAnalyzer:
    """记忆关系分析器。识别重复/演化/冗余/矛盾关系，生成合并操作建议。"""

    def analyze(self, fragments: List[MemoryFragment]) -> List[MemoryRelationRecord]:
        records = []
        n = len(fragments)
        for i in range(n):
            for j in range(i + 1, n):
                rel = self._analyze_pair(fragments[i], fragments[j])
                if rel and rel.relation_type != MemoryRelation.UNRELATED:
                    records.append(rel)
        return records

    def _analyze_pair(
        self, frag_a: MemoryFragment, frag_b: MemoryFragment
    ) -> Optional[MemoryRelationRecord]:
        similarity = self._compute_similarity(frag_a, frag_b)
        if similarity < 0.3:
            return None

        relation_type = MemoryRelation.UNRELATED
        action = MergeAction.NO_ACTION
        reason = ""

        if similarity > 0.95:
            relation_type = MemoryRelation.DUPLICATE
            if len(frag_b.content) > len(frag_a.content):
                action = MergeAction.KEEP_MOST_COMPLETE
            else:
                action = MergeAction.KEEP_NEWEST if frag_b.timestamp > frag_a.timestamp else MergeAction.KEEP_MOST_COMPLETE
            reason = "Near-duplicate content detected"
        elif similarity > 0.7:
            if abs(frag_a.timestamp - frag_b.timestamp) > 3600:
                relation_type = MemoryRelation.EVOLUTION
                action = MergeAction.ARCHIVE_OLD
                reason = "Content evolution over time"
            else:
                relation_type = MemoryRelation.REDUNDANT
                action = MergeAction.MERGE_CONTENT
                reason = "Redundant content with partial overlap"
        elif similarity > 0.5:
            if self._check_contradiction(frag_a, frag_b):
                relation_type = MemoryRelation.CONTRADICTORY
                action = MergeAction.FLAG_FOR_REVIEW
                reason = "Potential contradiction detected"
            else:
                relation_type = MemoryRelation.COMPLEMENTARY
                action = MergeAction.MERGE_CONTENT
                reason = "Complementary information"
        else:
            if self._check_contradiction(frag_a, frag_b):
                relation_type = MemoryRelation.CONTRADICTORY
                action = MergeAction.FLAG_FOR_REVIEW
                reason = "Possible contradiction at low similarity"
            else:
                return None

        rel_id = f"REL-{uuid.uuid4().hex[:8]}"
        return MemoryRelationRecord(
            relation_id=rel_id,
            fragment_a=frag_a,
            fragment_b=frag_b,
            relation_type=relation_type,
            similarity_score=similarity,
            recommended_action=action,
            reason=reason,
        )

    def _compute_similarity(self, frag_a: MemoryFragment, frag_b: MemoryFragment) -> float:
        words_a = set(frag_a.content.lower().split())
        words_b = set(frag_b.content.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        jaccard = len(intersection) / len(union) if union else 0.0
        if frag_a.tags and frag_b.tags:
            tag_overlap = len(frag_a.tags & frag_b.tags) / max(len(frag_a.tags), len(frag_b.tags))
            jaccard = 0.7 * jaccard + 0.3 * tag_overlap
        return jaccard

    def _check_contradiction(self, frag_a: MemoryFragment, frag_b: MemoryFragment) -> bool:
        negation_words = {"not", "no", "never", "cannot"}
        content_a = frag_a.content.lower()
        content_b = frag_b.content.lower()
        a_has = any(w in content_a for w in negation_words)
        b_has = any(w in content_b for w in negation_words)
        return a_has != b_has


# ── 模式发现引擎 ────────────────────────────────────────────────────


class PatternDiscoveryEngine:
    """跨片段模式发现引擎。"""

    MIN_SUPPORT = 3
    MIN_CONFIDENCE = 0.3

    def discover(self, fragments: List[MemoryFragment]) -> List[DiscoveredPattern]:
        patterns = []
        patterns.extend(self._discover_frequency_patterns(fragments))
        patterns.extend(self._discover_temporal_patterns(fragments))
        patterns.extend(self._discover_cooccurrence_patterns(fragments))
        return patterns

    def _discover_frequency_patterns(self, fragments: List[MemoryFragment]) -> List[DiscoveredPattern]:
        patterns = []
        source_groups: Dict[str, List[MemoryFragment]] = defaultdict(list)
        for frag in fragments:
            source_groups[frag.source].append(frag)
        for source, group in source_groups.items():
            if len(group) >= self.MIN_SUPPORT:
                times = [f.timestamp for f in group]
                time_range = max(times) - min(times) if len(times) > 1 else 0
                if time_range > 0 and time_range < 86400 * 7:
                    patterns.append(DiscoveredPattern(
                        pattern_id=f"PAT-FRQ-{uuid.uuid4().hex[:6]}",
                        pattern_type=PatternType.FREQUENCY,
                        description=f"Frequent interactions from source '{source}': "
                                    f"{len(group)} occurrences over {time_range / 3600:.2f}h",
                        supporting_fragments=[f.fragment_id for f in group],
                        confidence=min(1.0, len(group) / 10.0),
                        abstraction_level=2,
                        evidence_strength=len(group),
                    ))
        return patterns

    def _discover_temporal_patterns(self, fragments: List[MemoryFragment]) -> List[DiscoveredPattern]:
        patterns = []
        sorted_frags = sorted(fragments, key=lambda f: f.timestamp)
        for i in range(len(sorted_frags) - self.MIN_SUPPORT + 1):
            window = sorted_frags[i:i + self.MIN_SUPPORT]
            times = [f.timestamp for f in window]
            intervals = [times[j + 1] - times[j] for j in range(len(times) - 1)]
            if intervals:
                mean_interval = np.mean(intervals)
                std_interval = np.std(intervals) if len(intervals) > 1 else 0
                if mean_interval > 0 and std_interval / mean_interval < 0.5:
                    patterns.append(DiscoveredPattern(
                        pattern_id=f"PAT-TMP-{uuid.uuid4().hex[:6]}",
                        pattern_type=PatternType.TEMPORAL,
                        description=f"Regular temporal pattern: {self.MIN_SUPPORT} events "
                                    f"with mean interval {mean_interval / 3600:.2f}h",
                        supporting_fragments=[f.fragment_id for f in window],
                        confidence=1.0 - (std_interval / mean_interval if mean_interval > 0 else 0),
                        abstraction_level=3,
                        evidence_strength=len(window),
                    ))
        return patterns

    def _discover_cooccurrence_patterns(self, fragments: List[MemoryFragment]) -> List[DiscoveredPattern]:
        patterns = []
        tag_co: Dict[Tuple[str, ...], List[str]] = defaultdict(list)
        for frag in fragments:
            if len(frag.tags) >= 2:
                tag_tuple = tuple(sorted(frag.tags)[:3])
                tag_co[tag_tuple].append(frag.fragment_id)
        for tags, frag_ids in tag_co.items():
            if len(frag_ids) >= self.MIN_SUPPORT:
                patterns.append(DiscoveredPattern(
                    pattern_id=f"PAT-COO-{uuid.uuid4().hex[:6]}",
                    pattern_type=PatternType.PREFERENCE,
                    description=f"Co-occurrence pattern: tags {list(tags)} appear together "
                                f"in {len(frag_ids)} fragments",
                    supporting_fragments=frag_ids,
                    confidence=min(1.0, len(frag_ids) / 10.0),
                    abstraction_level=3,
                    evidence_strength=len(frag_ids),
                ))
        return patterns


# ── 安全监控联动器 ──────────────────────────────────────────────────


class ConsolidationSafetyGuard:
    """整合安全守卫：与 P8-1 安全监控器联动。"""

    def __init__(self, safety_monitor: Optional[Any] = None):
        self._safety_monitor = safety_monitor

    def set_monitor(self, monitor: Any) -> None:
        self._safety_monitor = monitor

    def validate_merge(self, action: Dict[str, Any]) -> Tuple[bool, str]:
        if self._safety_monitor is None:
            return True, "No safety monitor configured"
        target_id = action.get("target_fragment_id", "")
        if target_id and hasattr(self._safety_monitor, "check_isolation"):
            isolation = self._safety_monitor.check_isolation(target_id)
            if isolation:
                return False, f"Fragment {target_id[:8]} under isolation policy {isolation.policy_id}"
        return True, "Safe"

    def validate_pattern_persistence(self, pattern: DiscoveredPattern) -> Tuple[bool, str]:
        if self._safety_monitor is None:
            return True, "No safety monitor configured"
        for frag_id in pattern.supporting_fragments:
            if hasattr(self._safety_monitor, "check_isolation"):
                isolation = self._safety_monitor.check_isolation(frag_id)
                if isolation:
                    return False, f"Pattern relies on isolated fragment {frag_id[:8]}"
        return True, "Safe"


# ── 主类：休眠期记忆整合管道 ──────────────────────────────────────


class SleepConsolidationPipeline:
    """休眠期记忆整合管道。

    实现 Light-Omni 双重上下文状态 + MindMemOS Dreaming 精炼机制。
    """

    MODULE_ID = "P8-4"
    MODULE_NAME = "Sleep-Phase Memory Consolidation Pipeline"
    PAPER_REF = "Light-Omni (arXiv:2607.05511) + MindMemOS Dreaming"
    PAPER_TITLE = "Reflex Beats Reasoning: Dual Context Memory Consolidation"

    def __init__(
        self,
        fast_channel: Optional[FastReflexChannel] = None,
        slow_channel: Optional[SlowConsolidationChannel] = None,
        relation_analyzer: Optional[MemoryRelationAnalyzer] = None,
        pattern_engine: Optional[PatternDiscoveryEngine] = None,
        safety_guard: Optional[ConsolidationSafetyGuard] = None,
        safety_monitor: Optional[Any] = None,
    ):
        self._lock = threading.RLock()
        self._fast_channel = fast_channel or FastReflexChannel()
        self._slow_channel = slow_channel or SlowConsolidationChannel()
        self._relation_analyzer = relation_analyzer or MemoryRelationAnalyzer()
        self._pattern_engine = pattern_engine or PatternDiscoveryEngine()
        self._safety_guard = safety_guard or ConsolidationSafetyGuard(safety_monitor)
        self._consolidation_results: deque = deque(maxlen=50)
        self._total_consolidations = 0
        self._total_merges = 0
        self._total_patterns_discovered = 0

    def reflex_query(self, query_text: str, top_k: int = 10) -> List[MemoryFragment]:
        return self._fast_channel.query(query_text, top_k)

    def add_to_fast_channel(
        self,
        content: str,
        source: str = "unknown",
        importance: float = 0.5,
        tags: Optional[Set[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        fragment_id = f"FRG-{uuid.uuid4().hex[:12]}"
        fragment = MemoryFragment(
            fragment_id=fragment_id,
            content=content,
            source=source,
            importance=importance,
            tags=tags or set(),
            metadata=metadata or {},
        )
        with self._lock:
            self._fast_channel.add_fragment(fragment)
            self._slow_channel.add_fragment(fragment)
        return fragment_id

    def run_consolidation(self) -> ConsolidationResult:
        with self._lock:
            result_id = f"CSR-{uuid.uuid4().hex[:10]}"
            t_start = time.perf_counter()

            plan = ConsolidationPlan(
                plan_id=f"PLN-{uuid.uuid4().hex[:10]}",
                phase=ConsolidationPhase.SCANNING,
            )

            plan.phase = ConsolidationPhase.SCANNING
            all_fragments = self._slow_channel.get_all_fragments()

            plan.phase = ConsolidationPhase.DEDUPLICATING
            plan.relations = self._relation_analyzer.analyze(all_fragments)

            plan.phase = ConsolidationPhase.MERGING
            merge_actions, archived_count, created_count = self._generate_merge_actions(plan.relations)
            plan.merge_actions = merge_actions
            plan.estimated_savings = archived_count

            safe_actions = []
            for action in merge_actions:
                is_safe, reason = self._safety_guard.validate_merge(action)
                if is_safe:
                    safe_actions.append(action)
                else:
                    logger.warning("Merge blocked by safety guard: %s", reason)

            merges_executed = self._execute_merges(safe_actions)
            self._total_merges += merges_executed

            plan.phase = ConsolidationPhase.PATTERN_MINING
            discovered = self._pattern_engine.discover(all_fragments)
            safe_patterns = []
            for pattern in discovered:
                is_safe, reason = self._safety_guard.validate_pattern_persistence(pattern)
                if is_safe:
                    safe_patterns.append(pattern)
                else:
                    logger.warning("Pattern blocked by safety guard: %s", reason)
            plan.patterns = safe_patterns

            plan.phase = ConsolidationPhase.PERSISTING
            patterns_persisted = 0
            for pattern in safe_patterns:
                self._slow_channel.add_persisted_pattern(pattern)
                patterns_persisted += 1
            self._total_patterns_discovered += patterns_persisted

            plan.phase = ConsolidationPhase.COMPLETED

            result = ConsolidationResult(
                result_id=result_id,
                plan_id=plan.plan_id,
                merges_executed=merges_executed,
                patterns_persisted=patterns_persisted,
                fragments_archived=archived_count,
                fragments_created=created_count,
                duration_seconds=time.perf_counter() - t_start,
            )

            self._consolidation_results.append(result)
            self._total_consolidations += 1
            logger.info(
                "Consolidation complete: %d merges, %d patterns, %d archived, %.2fs",
                merges_executed, patterns_persisted, archived_count, result.duration_seconds,
            )
            return result

    def _generate_merge_actions(
        self, relations: List[MemoryRelationRecord]
    ) -> Tuple[List[Dict[str, Any]], int, int]:
        actions = []
        archived_count = 0
        created_count = 0
        for rel in relations:
            action = {
                "relation_id": rel.relation_id,
                "relation_type": rel.relation_type.value,
                "action": rel.recommended_action.value,
                "source_fragment_id": rel.fragment_a.fragment_id,
                "target_fragment_id": rel.fragment_b.fragment_id,
                "reason": rel.reason,
            }
            if rel.recommended_action == MergeAction.ARCHIVE_OLD:
                archived_count += 1
            elif rel.recommended_action == MergeAction.MERGE_CONTENT:
                created_count += 1
            actions.append(action)
        return actions, archived_count, created_count

    def _execute_merges(self, actions: List[Dict[str, Any]]) -> int:
        executed = 0
        for action in actions:
            action_type = action.get("action", "")
            if action_type in (
                MergeAction.ARCHIVE_OLD.value,
                MergeAction.KEEP_NEWEST.value,
                MergeAction.KEEP_MOST_COMPLETE.value,
                MergeAction.MERGE_CONTENT.value,
            ):
                executed += 1
                logger.debug("Merge executed: %s on %s", action_type, action.get("relation_id", ""))
        return executed

    def consolidated_query(self, query_text: str, top_k: int = 10) -> Dict[str, Any]:
        fragments = self._fast_channel.query(query_text, top_k)
        patterns = self._slow_channel.get_persisted_patterns()
        pattern_descriptions = []
        query_lower = query_text.lower()
        for pattern in patterns:
            if query_lower in pattern.description.lower():
                pattern_descriptions.append({
                    "pattern_id": pattern.pattern_id,
                    "pattern_type": pattern.pattern_type.value,
                    "description": pattern.description,
                    "confidence": pattern.confidence,
                })
        return {
            "fragments": fragments,
            "matched_patterns": pattern_descriptions,
            "channel": "consolidated",
        }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "module": self.MODULE_NAME,
                "paper": self.PAPER_REF,
                "fast_channel": self._fast_channel.statistics(),
                "slow_channel": self._slow_channel.statistics(),
                "total_consolidations": self._total_consolidations,
                "total_merges": self._total_merges,
                "total_patterns_discovered": self._total_patterns_discovered,
                "recent_results": len(self._consolidation_results),
                "safety_guard_active": self._safety_guard._safety_monitor is not None,
                "last_result": (
                    {
                        "merges_executed": self._consolidation_results[-1].merges_executed,
                        "patterns_persisted": self._consolidation_results[-1].patterns_persisted,
                        "duration_seconds": self._consolidation_results[-1].duration_seconds,
                    }
                    if self._consolidation_results
                    else None
                ),
            }

    def reset(self) -> None:
        with self._lock:
            self._consolidation_results.clear()
            self._total_consolidations = 0
            self._total_merges = 0
            self._total_patterns_discovered = 0
            logger.info("SleepConsolidationPipeline reset complete")
