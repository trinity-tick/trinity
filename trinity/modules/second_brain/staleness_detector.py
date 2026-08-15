"""
# status: orphan (2026-08-15 audit, not in runtime path)
P5-5: High-Relevance Memory Staleness Detector (对标 Mem0 2026 年报)
=======================================================================

区分低相关性记忆（自然衰减已覆盖）和高相关性记忆（高检索频率但
事实已过期，如用户换了工作）。实现主动探测机制——对高置信高检索
记忆周期性验证时效性，发现过期自动标记/降权。

Reference: Mem0 Blog, "AI Agent Memory 2026: Progress Benchmark Report",
           Memory Staleness section.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class MemoryFreshness(Enum):
    FRESH = "fresh"
    STALE_SUSPECT = "suspect"
    STALE_CONFIRMED = "stale"
    DEPRECATED = "deprecated"
    UNKNOWN = "unknown"


class StalenessRisk(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ProbeStrategy(Enum):
    DIRECT_QUESTION = "direct"
    IMPLICIT_CHECK = "implicit"
    CONTEXT_CLASH = "context_clash"
    TIME_BASED_EXPIRY = "time_expiry"


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class StalenessConfig:
    high_relevance_fetch_threshold: int = 5
    high_confidence_threshold: float = 0.7
    probe_interval_high_risk: float = 3600
    probe_interval_medium_risk: float = 86400
    probe_interval_low_risk: float = 604800
    max_probe_retries: int = 3
    auto_deprecate_stale: bool = False
    staleness_grace_period: float = 2592000
    decay_factor_stale: float = 0.1


@dataclass
class MemoryRecord:
    memory_id: str
    content: str
    category: str = "fact"
    confidence: float = 0.5
    created_at: float = field(default_factory=time.time)
    last_verified_at: float = field(default_factory=time.time)
    last_retrieved_at: float = 0.0
    retrieval_count_total: int = 0
    retrieval_count_recent: int = 0
    freshness: MemoryFreshness = MemoryFreshness.FRESH
    risk: StalenessRisk = StalenessRisk.LOW
    probe_count: int = 0
    evidence_signals: List[Dict[str, Any]] = field(default_factory=list)
    original_fact: str = ""
    updated_fact: str = ""
    deprecated_at: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProbeResult:
    memory_id: str
    probe_strategy: ProbeStrategy
    probe_time: float
    evidence: str
    freshness_before: MemoryFreshness
    freshness_after: MemoryFreshness
    confidence_change: float
    action_taken: str
    recommendation: str


@dataclass
class StalenessReport:
    total_tracked: int
    fresh_count: int
    suspect_count: int
    stale_count: int
    deprecated_count: int
    probe_queue_size: int
    recent_probes: List[ProbeResult]
    risk_distribution: Dict[str, int]
    recommendations: List[str]


# ── 风险判定规则 ─────────────────────────────────────────────────────

CATEGORY_RISK_MAP: Dict[str, StalenessRisk] = {
    "fact": StalenessRisk.MEDIUM,
    "preference": StalenessRisk.LOW,
    "relationship": StalenessRisk.MEDIUM,
    "plan": StalenessRisk.HIGH,
    "location": StalenessRisk.HIGH,
    "employment": StalenessRisk.CRITICAL,
    "contact": StalenessRisk.MEDIUM,
    "timeless": StalenessRisk.LOW,
}

HIGH_RISK_KEYWORDS: Set[str] = {
    "公司", "工作", "职位", "入职", "跳槽", "离职",
    "手机号", "电话", "地址", "住址", "搬家",
    "今天", "明天", "现在", "当前", "目前",
    "company", "job", "position", "phone", "address",
}

# risk ordering for max()
_RISK_ORDER = [StalenessRisk.LOW, StalenessRisk.MEDIUM, StalenessRisk.HIGH, StalenessRisk.CRITICAL]

PROBE_TEMPLATES: Dict[str, List[str]] = {
    "employment": [
        "你还在{company}工作吗？",
        "你目前的工作单位有变化吗？",
        "你现在的职位还是{position}吗？",
    ],
    "location": [
        "你现在还在{location}吗？",
        "你的地址有变化吗？",
    ],
    "contact": [
        "你的联系方式还是{contact}吗？",
    ],
    "plan": [
        "你之前提到的{plan}计划有变化吗？",
    ],
}


# ── 辅助类：陈旧评分与队列构建 ──────────────────────────────────────

class _StalenessScorer:
    """评分引擎：追踪记忆、判定高相关性、构建探测队列、检测冲突。

    从 StalenessDetector 拆分而来。
    """

    def __init__(self, config: StalenessConfig):
        self.config = config
        self._lock = threading.RLock()
        self._memories: Dict[str, MemoryRecord] = {}
        self._by_freshness: Dict[MemoryFreshness, Set[str]] = {
            fs: set() for fs in MemoryFreshness
        }
        self._by_risk: Dict[StalenessRisk, Set[str]] = {
            sr: set() for sr in StalenessRisk
        }
        self._total_tracked: int = 0
        self._probe_queue: deque = deque()

    def track_memory(
        self, memory_id: str, content: str, category: str = "fact",
        confidence: float = 0.5, risk: Optional[StalenessRisk] = None,
    ) -> str:
        with self._lock:
            if risk is None:
                risk = CATEGORY_RISK_MAP.get(category, StalenessRisk.MEDIUM)
                content_lower = content.lower()
                for kw in HIGH_RISK_KEYWORDS:
                    if kw in content_lower:
                        risk = max(risk, StalenessRisk.HIGH, key=lambda r: _RISK_ORDER.index(r))
                        break
            record = MemoryRecord(
                memory_id=memory_id, content=content, category=category,
                confidence=confidence, risk=risk, original_fact=content,
            )
            self._memories[memory_id] = record
            self._by_freshness[MemoryFreshness.FRESH].add(memory_id)
            self._by_risk[risk].add(memory_id)
            self._total_tracked += 1
            return memory_id

    def record_retrieval(self, memory_id: str) -> None:
        with self._lock:
            record = self._memories.get(memory_id)
            if not record:
                return
            now = time.time()
            record.retrieval_count_total += 1
            record.last_retrieved_at = now
            if now - record.created_at < 604800:
                record.retrieval_count_recent += 1

    def _is_high_relevance(self, record: MemoryRecord) -> bool:
        return (
            record.retrieval_count_recent >= self.config.high_relevance_fetch_threshold
            and record.confidence >= self.config.high_confidence_threshold
        )

    def get_high_relevance_memories(self) -> List[MemoryRecord]:
        with self._lock:
            return [
                r for r in self._memories.values()
                if self._is_high_relevance(r)
                and r.freshness not in (MemoryFreshness.DEPRECATED,)
            ]

    def build_probe_queue(self) -> int:
        with self._lock:
            now = time.time()
            added = 0
            high_rel = self.get_high_relevance_memories()
            for record in high_rel:
                if record.risk in (StalenessRisk.CRITICAL, StalenessRisk.HIGH):
                    interval = self.config.probe_interval_high_risk
                elif record.risk == StalenessRisk.MEDIUM:
                    interval = self.config.probe_interval_medium_risk
                else:
                    interval = self.config.probe_interval_low_risk

                if (
                    now - record.last_verified_at >= interval
                    and record.probe_count < self.config.max_probe_retries
                ):
                    if record.memory_id not in self._probe_queue:
                        self._probe_queue.append(record.memory_id)
                        added += 1
            return added

    def generate_probe_question(self, memory_id: str) -> Optional[str]:
        record = self._memories.get(memory_id)
        if not record:
            return None
        templates = PROBE_TEMPLATES.get(
            record.category,
            ["关于 {content} 的信息，现在有变化吗？"],
        )
        template = templates[record.probe_count % len(templates)]
        content = record.content
        for match in re.findall(r'\{(\w+)\}', template):
            template = template.replace(f"{{{match}}}", content[:50])
        if "{" in template:
            template = f"关于'{record.content[:60]}'的信息，现在有变化吗？"
        return template

    def pop_queue(self) -> Optional[str]:
        with self._lock:
            if self._probe_queue:
                return self._probe_queue.popleft()
            return None

    def queue_size(self) -> int:
        with self._lock:
            return len(self._probe_queue)

    def get_record(self, memory_id: str) -> Optional[MemoryRecord]:
        with self._lock:
            return self._memories.get(memory_id)

    def get_all_records(self) -> Dict[str, MemoryRecord]:
        with self._lock:
            return dict(self._memories)

    def get_by_freshness(self, fs: MemoryFreshness) -> Set[str]:
        with self._lock:
            return set(self._by_freshness[fs])

    def get_by_risk(self) -> Dict[StalenessRisk, Set[str]]:
        with self._lock:
            return {sr: set(s) for sr, s in self._by_risk.items()}

    def move_freshness(self, memory_id: str, old_fs: MemoryFreshness, new_fs: MemoryFreshness) -> None:
        with self._lock:
            self._by_freshness[old_fs].discard(memory_id)
            self._by_freshness[new_fs].add(memory_id)

    def _detect_fact_conflict(self, old_fact: str, new_context: str) -> float:
        old_tokens = set(old_fact.lower().split())
        new_tokens = set(new_context.lower().split())
        negation_words = {"不", "不是", "没有", "换了", "变了", "改", "不再是", "离职", "跳槽"}
        has_negation = bool(new_tokens & negation_words)
        key_entities_old = {t for t in old_tokens if len(t) > 2 and t.isalpha()}
        key_entities_new = {t for t in new_tokens if len(t) > 2 and t.isalpha()}
        entity_overlap = key_entities_old & key_entities_new
        new_entities = key_entities_new - key_entities_old

        score = 0.0
        if has_negation and entity_overlap:
            score += 0.6
        if new_entities:
            score += 0.2
        if entity_overlap:
            overlap_ratio = len(entity_overlap) / max(len(key_entities_old), 1)
            score = max(score, 0.3 + overlap_ratio * 0.3)
        return min(1.0, score)

    def detect_conflict(self, old_fact: str, new_context: str) -> float:
        return self._detect_fact_conflict(old_fact, new_context)


# ── 辅助类：探测执行与刷新规划 ──────────────────────────────────────

class _RefreshPlanner:
    """探测执行器：探测记忆、运行检测周期、手动操作。

    从 StalenessDetector 拆分而来。
    """

    def __init__(self, config: StalenessConfig, scorer: _StalenessScorer):
        self.config = config
        self._scorer = scorer
        self._lock = threading.RLock()
        self._probe_history: List[ProbeResult] = []
        self._total_probes: int = 0
        self._total_stale_detected: int = 0
        self._total_deprecated: int = 0

    def probe_memory(
        self, memory_id: str, new_context: str = "",
        strategy: ProbeStrategy = ProbeStrategy.CONTEXT_CLASH,
    ) -> Optional[ProbeResult]:
        with self._lock:
            record = self._scorer.get_record(memory_id)
            if not record:
                return None

            freshness_before = record.freshness
            old_confidence = record.confidence
            evidence = ""
            is_stale = False
            action = ""

            if strategy == ProbeStrategy.CONTEXT_CLASH and new_context:
                conflict_score = self._scorer.detect_conflict(record.content, new_context)
                if conflict_score > 0.6:
                    evidence = (
                        f"检测到矛盾信号（score={conflict_score:.2f}）: "
                        f"旧事实='{record.content[:80]}' vs 新上下文='{new_context[:80]}'"
                    )
                    is_stale = True
                    action = "标记为 STALE_CONFIRMED，降权"
                else:
                    evidence = f"未检测到矛盾（score={conflict_score:.2f}），视为仍有效"
            elif strategy == ProbeStrategy.TIME_BASED_EXPIRY:
                age_days = (time.time() - record.created_at) / 86400
                if age_days > 90 and record.risk == StalenessRisk.CRITICAL:
                    evidence = f"创建已 {age_days:.0f} 天，超高风险等级，触发过期检查"
                    is_stale = True
                    action = "基于时间的过期判定"

            if is_stale:
                self._scorer.move_freshness(memory_id, record.freshness, MemoryFreshness.STALE_CONFIRMED)
                record.freshness = MemoryFreshness.STALE_CONFIRMED
                record.confidence *= self.config.decay_factor_stale
                self._total_stale_detected += 1
            else:
                record.last_verified_at = time.time()
                record.confidence = min(0.99, record.confidence + 0.01)

            record.probe_count += 1
            record.evidence_signals.append({
                "time": time.time(),
                "strategy": strategy.value,
                "evidence": evidence,
                "is_stale": is_stale,
            })

            result = ProbeResult(
                memory_id=memory_id, probe_strategy=strategy,
                probe_time=time.time(), evidence=evidence,
                freshness_before=freshness_before,
                freshness_after=record.freshness,
                confidence_change=record.confidence - old_confidence,
                action_taken=action,
                recommendation=(
                    "建议更新记忆内容" if is_stale else "记忆当前有效，无需操作"
                ),
            )
            self._probe_history.append(result)
            self._total_probes += 1
            return result

    def run_detection_cycle(
        self, recent_contexts: Optional[Dict[str, str]] = None,
    ) -> StalenessReport:
        self._scorer.build_probe_queue()
        probe_results: List[ProbeResult] = []
        while True:
            mem_id = self._scorer.pop_queue()
            if mem_id is None:
                break
            context = ""
            if recent_contexts:
                context = recent_contexts.get(mem_id, "")
            result = self.probe_memory(mem_id, new_context=context)
            if result:
                probe_results.append(result)

        return self._generate_report(probe_results)

    def _generate_report(self, recent_probes: List[ProbeResult]) -> StalenessReport:
        fresh = len(self._scorer.get_by_freshness(MemoryFreshness.FRESH))
        suspect = len(self._scorer.get_by_freshness(MemoryFreshness.STALE_SUSPECT))
        stale = len(self._scorer.get_by_freshness(MemoryFreshness.STALE_CONFIRMED))
        deprecated = len(self._scorer.get_by_freshness(MemoryFreshness.DEPRECATED))

        risk_dist = {
            risk.value: len(mem_ids)
            for risk, mem_ids in self._scorer.get_by_risk().items()
        }

        recommendations = []
        if stale > 0:
            recommendations.append(
                f"有 {stale} 条记忆已确认过期，建议执行内容更新或手动弃用"
            )
        if suspect > 0:
            recommendations.append(
                f"有 {suspect} 条记忆疑似过期，需要进一步验证"
            )

        return StalenessReport(
            total_tracked=len(self._scorer.get_all_records()),
            fresh_count=fresh, suspect_count=suspect,
            stale_count=stale, deprecated_count=deprecated,
            probe_queue_size=self._scorer.queue_size(),
            recent_probes=recent_probes,
            risk_distribution=risk_dist,
            recommendations=recommendations,
        )

    def mark_deprecated(self, memory_id: str, reason: str = "") -> bool:
        record = self._scorer.get_record(memory_id)
        if not record:
            return False
        self._scorer.move_freshness(memory_id, record.freshness, MemoryFreshness.DEPRECATED)
        record.freshness = MemoryFreshness.DEPRECATED
        record.deprecated_at = time.time()
        record.updated_fact = reason
        self._total_deprecated += 1
        return True

    def update_memory(
        self, memory_id: str, new_content: str, new_confidence: float = 0.5,
    ) -> bool:
        record = self._scorer.get_record(memory_id)
        if not record:
            return False
        if record.freshness == MemoryFreshness.STALE_CONFIRMED:
            self._scorer.move_freshness(memory_id, MemoryFreshness.STALE_CONFIRMED, MemoryFreshness.FRESH)
        record.content = new_content
        record.confidence = new_confidence
        record.freshness = MemoryFreshness.FRESH
        record.last_verified_at = time.time()
        record.probe_count = 0
        record.updated_fact = new_content
        record.evidence_signals.clear()
        if record.freshness != MemoryFreshness.FRESH:
            self._scorer.move_freshness(memory_id, record.freshness, MemoryFreshness.FRESH)
            record.freshness = MemoryFreshness.FRESH
        return True

    def get_stale_memories(self) -> List[MemoryRecord]:
        stale_ids = (
            self._scorer.get_by_freshness(MemoryFreshness.STALE_CONFIRMED)
            | self._scorer.get_by_freshness(MemoryFreshness.STALE_SUSPECT)
        )
        all_records = self._scorer.get_all_records()
        return [all_records[mid] for mid in stale_ids if mid in all_records]

    def get_effective_weight(self, memory_id: str) -> float:
        record = self._scorer.get_record(memory_id)
        if not record:
            return 0.0
        if record.freshness == MemoryFreshness.STALE_CONFIRMED:
            return record.confidence * self.config.decay_factor_stale
        elif record.freshness == MemoryFreshness.DEPRECATED:
            return 0.0
        elif record.freshness == MemoryFreshness.STALE_SUSPECT:
            return record.confidence * 0.5
        return record.confidence

    def get_total_probes(self) -> int:
        return self._total_probes

    def get_total_stale_detected(self) -> int:
        return self._total_stale_detected

    def get_total_deprecated(self) -> int:
        return self._total_deprecated


# ── StalenessDetector Facade ──────────────────────────────────────────

class StalenessDetector:
    """高相关性记忆陈旧检测器。

    内部委托至 _StalenessScorer（评分+队列）与 _RefreshPlanner（探测+操作）。
    """

    def __init__(self, config: Optional[StalenessConfig] = None):
        self.config = config or StalenessConfig()
        self._scorer = _StalenessScorer(self.config)
        self._planner = _RefreshPlanner(self.config, self._scorer)

    def track_memory(
        self, memory_id: str, content: str, category: str = "fact",
        confidence: float = 0.5, risk: Optional[StalenessRisk] = None,
    ) -> str:
        return self._scorer.track_memory(memory_id, content, category, confidence, risk)

    def record_retrieval(self, memory_id: str) -> None:
        self._scorer.record_retrieval(memory_id)

    def get_high_relevance_memories(self) -> List[MemoryRecord]:
        return self._scorer.get_high_relevance_memories()

    def build_probe_queue(self) -> int:
        return self._scorer.build_probe_queue()

    def generate_probe_question(self, memory_id: str) -> Optional[str]:
        return self._scorer.generate_probe_question(memory_id)

    def probe_memory(
        self, memory_id: str, new_context: str = "",
        strategy: ProbeStrategy = ProbeStrategy.CONTEXT_CLASH,
    ) -> Optional[ProbeResult]:
        return self._planner.probe_memory(memory_id, new_context, strategy)

    def run_detection_cycle(
        self, recent_contexts: Optional[Dict[str, str]] = None,
    ) -> StalenessReport:
        return self._planner.run_detection_cycle(recent_contexts)

    def mark_deprecated(self, memory_id: str, reason: str = "") -> bool:
        return self._planner.mark_deprecated(memory_id, reason)

    def update_memory(
        self, memory_id: str, new_content: str, new_confidence: float = 0.5,
    ) -> bool:
        return self._planner.update_memory(memory_id, new_content, new_confidence)

    def get_stale_memories(self) -> List[MemoryRecord]:
        return self._planner.get_stale_memories()

    def get_effective_weight(self, memory_id: str) -> float:
        return self._planner.get_effective_weight(memory_id)

    def statistics(self) -> Dict[str, Any]:
        all_records = self._scorer.get_all_records()
        return {
            "total_tracked": len(all_records),
            "total_probes": self._planner.get_total_probes(),
            "total_stale_detected": self._planner.get_total_stale_detected(),
            "total_deprecated": self._planner.get_total_deprecated(),
            "fresh_count": len(self._scorer.get_by_freshness(MemoryFreshness.FRESH)),
            "suspect_count": len(self._scorer.get_by_freshness(MemoryFreshness.STALE_SUSPECT)),
            "stale_count": len(self._scorer.get_by_freshness(MemoryFreshness.STALE_CONFIRMED)),
            "deprecated_count": len(self._scorer.get_by_freshness(MemoryFreshness.DEPRECATED)),
            "probe_queue_size": self._scorer.queue_size(),
            "high_relevance_count": len(self._scorer.get_high_relevance_memories()),
            "risk_distribution": {
                risk.value: len(mem_ids)
                for risk, mem_ids in self._scorer.get_by_risk().items()
            },
        }
