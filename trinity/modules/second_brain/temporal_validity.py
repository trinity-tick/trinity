"""
P3-6: Automatic Temporal Metadata Extraction + Decay (对标 Mem0 May 2026)
==========================================================================
Auto-extract event occurrence time, ongoing/completed status, time precision,
memory type (fact/preference/relationship/plan/timeless), and time-based
memory decay weights.

Mem0 Temporal Reasoning 的设计要点（May 2026）：
  - 写入记忆时提取时间元数据：事件发生时间、进行中/已完成、时间精度、记忆类型
  - 时间感知检索：区分当前事实、历史事实、未来计划、偏好、关系、永恒知识
  - 时间衰减：基于时间距离的权重衰减
  - 在 <7,000 token retrieval budget 下实现 +3.8 时序推理、+1.5 多轮推理

Benchmark: LoCoMo 92.5% (+0.9), LongMemEval 94.4% (+1.0)

Reference:
  - Mem0 Blog: "The Token-Efficient Memory Algorithm Now Has Temporal Reasoning"
  - Temporal metadata: when, ongoing/completed, precision, memory type
"""

from __future__ import annotations

import logging
import math
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────

class TemporalPrecision(Enum):
    """Granularity of temporal information."""
    EXACT = "exact"           # Precise timestamp known
    DAY = "day"               # Known to the day
    WEEK = "week"             # Known to the week
    MONTH = "month"           # Known to the month
    YEAR = "year"             # Known to the year
    VAGUE = "vague"           # "recently", "a while ago"
    UNKNOWN = "unknown"       # No temporal info


class MemoryStatus(Enum):
    """Status of a memory's validity in time."""
    ONGOING = "ongoing"       # Still happening / currently true
    COMPLETED = "completed"   # Finished / no longer active
    PLANNED = "planned"       # Scheduled for the future
    UNKNOWN = "unknown"       # Not determined


class MemoryType(Enum):
    """Classification of memory by nature."""
    FACT = "fact"             # Objective fact (e.g., "Alice works at X")
    PREFERENCE = "preference" # Subjective preference (e.g., "likes Python")
    RELATIONSHIP = "relationship"  # Relationship between entities
    PLAN = "plan"             # Future plan or intent
    TIMELESS = "timeless"     # Eternal/unchanging knowledge (e.g., "2+2=4")
    EVENT = "event"           # One-time event
    UNKNOWN = "unknown"       # Not classified


# ── Data Structures ──────────────────────────────────────────────────────

@dataclass
class TemporalMetadata:
    """Extracted temporal metadata for a single memory."""
    memory_id: str
    event_timestamp: Optional[float]      # Unix timestamp of the event
    precision: TemporalPrecision
    status: MemoryStatus
    memory_type: MemoryType
    extracted_at: float = field(default_factory=time.time)
    source_text_snippet: str = ""         # Snippet that triggered extraction
    confidence: float = 1.0               # Extraction confidence


@dataclass
class DecayWeight:
    """Time-based decay weight for a memory."""
    memory_id: str
    base_weight: float              # Original weight
    decayed_weight: float            # After applying decay
    age_days: float                  # Age in days
    half_life_days: float            # Half-life used for decay
    memory_type: MemoryType


# ── Expiry Scheduler: extraction + classification ────────────────────────

class _ExpiryScheduler:
    """Extract temporal metadata and classify memory types.

    Detection pipeline:
      1. Status detection: ongoing / completed / planned
      2. Memory type classification: fact / preference / relationship / plan / timeless
      3. Timestamp extraction: date patterns in text
      4. Precision estimation
    """

    def __init__(self):
        self._time_patterns = self._build_time_patterns()

    def extract_temporal_metadata(
        self,
        memory_id: str,
        text: str,
        known_timestamp: Optional[float] = None,
        known_precision: Optional[TemporalPrecision] = None,
    ) -> TemporalMetadata:
        text_lower = text.lower()
        status = self._detect_status(text_lower)
        mem_type = self._classify_memory_type(text_lower, status)

        event_ts: Optional[float] = known_timestamp
        precision: TemporalPrecision = known_precision or TemporalPrecision.UNKNOWN

        if event_ts is None:
            event_ts, precision, snippet = self._extract_timestamp(text)
        else:
            snippet = text[:200]

        return TemporalMetadata(
            memory_id=memory_id,
            event_timestamp=event_ts,
            precision=precision,
            status=status,
            memory_type=mem_type,
            source_text_snippet=snippet,
        )

    @staticmethod
    def _detect_status(text_lower: str) -> MemoryStatus:
        ongoing_markers = [
            "is", "are", "currently", "now", "still", "continues",
            "一直在", "正在", "目前", "仍然", "持续",
        ]
        completed_markers = [
            "was", "were", "used to", "no longer", "stopped", "finished",
            "ended", "previously", "before",
            "曾经", "过去", "不再", "已经", "结束", "停止",
        ]
        planned_markers = [
            "will", "plan", "going to", "intend", "schedule", "upcoming",
            "planned", "future",
            "将会", "计划", "打算", "未来", "安排",
        ]

        ongoing_score = sum(1 for m in ongoing_markers if m in text_lower)
        completed_score = sum(1 for m in completed_markers if m in text_lower)
        planned_score = sum(1 for m in planned_markers if m in text_lower)

        max_score = max(ongoing_score, completed_score, planned_score, 0)
        if max_score == 0:
            return MemoryStatus.UNKNOWN
        if ongoing_score == max_score:
            return MemoryStatus.ONGOING
        elif completed_score == max_score:
            return MemoryStatus.COMPLETED
        return MemoryStatus.PLANNED

    @staticmethod
    def _classify_memory_type(
        text_lower: str,
        status: MemoryStatus,
    ) -> MemoryType:
        if status == MemoryStatus.PLANNED:
            return MemoryType.PLAN

        preference_markers = [
            "prefer", "like", "love", "hate", "favorite", "enjoy",
            "喜欢", "偏好", "最爱", "讨厌",
        ]
        if any(m in text_lower for m in preference_markers):
            return MemoryType.PREFERENCE

        relationship_markers = [
            "works with", "reports to", "colleague", "friend", "family",
            "同事", "朋友", "家人", "汇报给",
        ]
        if any(m in text_lower for m in relationship_markers):
            return MemoryType.RELATIONSHIP

        timeless_markers = [
            "is defined as", "always", "fundamentally", "the capital of",
            "equals", "law of",
            "定义", "永远是", "定理", "公理",
        ]
        if any(m in text_lower for m in timeless_markers):
            return MemoryType.TIMELESS

        event_markers = [
            "happened", "occurred", "took place", "meeting on",
            "conference", "launched",
            "发生", "会议", "发布",
        ]
        if any(m in text_lower for m in event_markers):
            return MemoryType.EVENT

        return MemoryType.FACT

    @staticmethod
    def _build_time_patterns() -> List[Tuple[re.Pattern, TemporalPrecision]]:
        patterns: List[Tuple[str, TemporalPrecision]] = [
            (r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b", TemporalPrecision.DAY),
            (r"(\d{4})年(\d{1,2})月(\d{1,2})日", TemporalPrecision.DAY),
            (r"(\d{4})年(\d{1,2})月(?!\d*日)", TemporalPrecision.MONTH),
            (r"(\d+)\s*(day|week|month|year)s?\s*ago", TemporalPrecision.VAGUE),
            (
                r"(January|February|March|April|May|June|July|"
                r"August|September|October|November|December)\s+(\d{1,2})?,?\s*(\d{4})",
                TemporalPrecision.DAY,
            ),
            (r"\bin\s+(\d{4})\b", TemporalPrecision.YEAR),
            (r"(\d{1,2})月(\d{1,2})日", TemporalPrecision.DAY),
        ]
        return [(re.compile(p, re.IGNORECASE), prec) for p, prec in patterns]

    def _extract_timestamp(
        self, text: str
    ) -> Tuple[Optional[float], TemporalPrecision, str]:
        from datetime import datetime

        best_ts: Optional[float] = None
        best_precision = TemporalPrecision.UNKNOWN
        best_snippet = ""

        prec_rank = {
            TemporalPrecision.EXACT: 6,
            TemporalPrecision.DAY: 5,
            TemporalPrecision.WEEK: 4,
            TemporalPrecision.MONTH: 3,
            TemporalPrecision.YEAR: 2,
            TemporalPrecision.VAGUE: 1,
            TemporalPrecision.UNKNOWN: 0,
        }

        for pattern, prec in self._time_patterns:
            match = pattern.search(text)
            if not match:
                continue
            groups = match.groups()
            snippet = match.group(0)

            try:
                if prec == TemporalPrecision.DAY:
                    if len(groups) >= 3 and groups[0].isdigit():
                        year = int(groups[0])
                        month = int(groups[1])
                        day = int(groups[2])
                    elif len(groups) >= 3:
                        month_names = {
                            "january": 1, "february": 2, "march": 3, "april": 4,
                            "may": 5, "june": 6, "july": 7, "august": 8,
                            "september": 9, "october": 10, "november": 11,
                            "december": 12,
                        }
                        month = month_names.get(groups[0].lower(), 1)
                        day = int(groups[1]) if groups[1] else 1
                        year = int(groups[2])
                    else:
                        continue
                    dt = datetime(year, min(month, 12), min(day, 28))
                    ts = dt.timestamp()
                elif prec == TemporalPrecision.MONTH:
                    year = int(groups[0])
                    month = int(groups[1])
                    dt = datetime(year, min(month, 12), 1)
                    ts = dt.timestamp()
                elif prec == TemporalPrecision.YEAR:
                    year = int(groups[0]) if groups[0].isdigit() else int(groups[2])
                    dt = datetime(year, 1, 1)
                    ts = dt.timestamp()
                elif prec == TemporalPrecision.VAGUE:
                    num = int(groups[0])
                    unit = groups[1]
                    now = time.time()
                    if unit.startswith("day"):
                        ts = now - num * 86400
                    elif unit.startswith("week"):
                        ts = now - num * 7 * 86400
                    elif unit.startswith("month"):
                        ts = now - num * 30 * 86400
                    elif unit.startswith("year"):
                        ts = now - num * 365 * 86400
                    else:
                        ts = now
                else:
                    continue

                if prec_rank.get(prec, 0) > prec_rank.get(best_precision, 0):
                    best_ts = ts
                    best_precision = prec
                    best_snippet = snippet

            except (ValueError, IndexError):
                continue

        return best_ts, best_precision, best_snippet

    def extract_batch(
        self,
        memories: List[Dict[str, Any]],
    ) -> Dict[str, TemporalMetadata]:
        results: Dict[str, TemporalMetadata] = {}
        for mem in memories:
            mem_id = mem.get("id", mem.get("memory_id", f"mem_{hash(str(mem))}"))
            content = mem.get("content", mem.get("text", ""))
            ts = mem.get("timestamp", mem.get("event_timestamp"))
            precision = mem.get("precision")
            if isinstance(precision, str):
                try:
                    precision = TemporalPrecision(precision)
                except ValueError:
                    precision = None

            meta = self.extract_temporal_metadata(
                memory_id=mem_id,
                text=content,
                known_timestamp=ts,
                known_precision=precision,
            )
            results[mem_id] = meta

        logger.info("Extracted temporal metadata for %d memories", len(results))
        return results

    @staticmethod
    def get_summary_stats(
        metadata_map: Dict[str, TemporalMetadata]
    ) -> Dict[str, Any]:
        type_counts = defaultdict(int)
        status_counts = defaultdict(int)
        precision_counts = defaultdict(int)

        for meta in metadata_map.values():
            type_counts[meta.memory_type.value] += 1
            status_counts[meta.status.value] += 1
            precision_counts[meta.precision.value] += 1

        return {
            "total": len(metadata_map),
            "memory_type_distribution": dict(type_counts),
            "status_distribution": dict(status_counts),
            "precision_distribution": dict(precision_counts),
        }


# ── Recency Booster: decay + filtering ───────────────────────────────────

class _RecencyBooster:
    """Compute time-based decay weights and filter by temporal relevance.

    Uses exponential decay: w(t) = w0 * exp(-ln(2) * t / half_life).
    """

    def __init__(
        self,
        default_half_life_days: float = 90.0,
        half_life_by_type: Optional[Dict[MemoryType, float]] = None,
    ):
        self.default_half_life = default_half_life_days
        self._half_life_by_type: Dict[MemoryType, float] = {
            MemoryType.FACT: 180.0,
            MemoryType.PREFERENCE: 60.0,
            MemoryType.RELATIONSHIP: 120.0,
            MemoryType.PLAN: 30.0,
            MemoryType.TIMELESS: float("inf"),
            MemoryType.EVENT: 90.0,
            MemoryType.UNKNOWN: default_half_life_days,
        }
        if half_life_by_type:
            self._half_life_by_type.update(half_life_by_type)

    def compute_decay(
        self,
        memory_id: str,
        base_weight: float,
        event_timestamp: Optional[float],
        memory_type: MemoryType = MemoryType.UNKNOWN,
        reference_time: Optional[float] = None,
    ) -> DecayWeight:
        now = reference_time or time.time()

        if event_timestamp is None:
            return DecayWeight(
                memory_id=memory_id,
                base_weight=base_weight,
                decayed_weight=base_weight,
                age_days=0.0,
                half_life_days=float("inf"),
                memory_type=memory_type,
            )

        half_life = self._half_life_by_type.get(memory_type, self.default_half_life)
        age_seconds = max(0, now - event_timestamp)
        age_days = age_seconds / 86400.0

        if half_life == float("inf") or memory_type == MemoryType.TIMELESS:
            return DecayWeight(
                memory_id=memory_id,
                base_weight=base_weight,
                decayed_weight=base_weight,
                age_days=age_days,
                half_life_days=half_life,
                memory_type=memory_type,
            )

        decay_factor = math.exp(-math.log(2) * age_days / half_life)
        decayed = base_weight * decay_factor

        return DecayWeight(
            memory_id=memory_id,
            base_weight=base_weight,
            decayed_weight=round(decayed, 6),
            age_days=round(age_days, 2),
            half_life_days=half_life,
            memory_type=memory_type,
        )

    def apply_decay_to_memories(
        self,
        memories: List[Dict[str, Any]],
        reference_time: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        now = reference_time or time.time()
        enriched: List[Dict[str, Any]] = []

        for mem in memories:
            mem_id = mem.get("id", mem.get("memory_id", "unknown"))
            base_score = mem.get("score", mem.get("weight", 1.0))
            content = mem.get("content", mem.get("text", ""))
            ts = mem.get("timestamp", mem.get("event_timestamp"))
            mem_type = mem.get("memory_type")

            if isinstance(mem_type, str):
                try:
                    mem_type = MemoryType(mem_type)
                except ValueError:
                    mem_type = _ExpiryScheduler._classify_memory_type(
                        content.lower(), MemoryStatus.UNKNOWN
                    )
            elif mem_type is None:
                mem_type = _ExpiryScheduler._classify_memory_type(
                    content.lower(), MemoryStatus.UNKNOWN
                )

            decay = self.compute_decay(
                memory_id=mem_id,
                base_weight=base_score,
                event_timestamp=ts,
                memory_type=mem_type,
                reference_time=now,
            )

            mem_copy = dict(mem)
            mem_copy["decayed_score"] = decay.decayed_weight
            mem_copy["age_days"] = decay.age_days
            mem_copy["half_life_days"] = decay.half_life_days
            mem_copy["memory_type"] = mem_type.value
            enriched.append(mem_copy)

        enriched.sort(key=lambda m: -m["decayed_score"])
        return enriched

    def filter_by_time_relevance(
        self,
        memories: List[Dict[str, Any]],
        query: str,
        reference_time: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        query_lower = query.lower()
        now = reference_time or time.time()

        current_markers = [
            "currently", "now", "at present", "right now", "still",
            "目前", "现在", "当前", "仍然",
        ]
        past_markers = [
            "was", "used to", "before", "previously", "history", "past",
            "过去", "以前", "曾经", "历史",
        ]
        future_markers = [
            "will", "going to", "plan", "future", "upcoming", "next",
            "将会", "计划", "未来", "即将",
        ]

        is_current_query = any(m in query_lower for m in current_markers)
        is_past_query = any(m in query_lower for m in past_markers)
        is_future_query = any(m in query_lower for m in future_markers)

        enriched: List[Dict[str, Any]] = []

        for mem in memories:
            mem_id = mem.get("id", mem.get("memory_id", "unknown"))
            base_score = mem.get("score", mem.get("weight", 1.0))
            content = mem.get("content", mem.get("text", ""))
            ts = mem.get("timestamp", mem.get("event_timestamp"))
            mem_type_raw = mem.get("memory_type")
            status_raw = mem.get("status")

            if isinstance(mem_type_raw, str):
                try:
                    mem_type = MemoryType(mem_type_raw)
                except ValueError:
                    mem_type = _ExpiryScheduler._classify_memory_type(
                        content.lower(), MemoryStatus.UNKNOWN
                    )
            else:
                mem_type = _ExpiryScheduler._classify_memory_type(
                    content.lower(), MemoryStatus.UNKNOWN
                )

            if isinstance(status_raw, str):
                try:
                    mem_status = MemoryStatus(status_raw)
                except ValueError:
                    mem_status = _ExpiryScheduler._detect_status(content.lower())
            else:
                mem_status = _ExpiryScheduler._detect_status(content.lower())

            decay = self.compute_decay(mem_id, base_score, ts, mem_type, now)
            score = decay.decayed_weight

            if is_current_query:
                if mem_status == MemoryStatus.ONGOING:
                    score *= 1.5
                elif mem_status == MemoryStatus.COMPLETED:
                    score *= 0.5
            elif is_past_query:
                if mem_status == MemoryStatus.COMPLETED:
                    score *= 1.5
                elif mem_status == MemoryStatus.ONGOING:
                    score *= 0.8
            elif is_future_query:
                if mem_status == MemoryStatus.PLANNED:
                    score *= 2.0
                elif mem_status == MemoryStatus.COMPLETED:
                    score *= 0.3

            mem_copy = dict(mem)
            mem_copy["decayed_score"] = round(score, 6)
            mem_copy["age_days"] = decay.age_days
            mem_copy["memory_type"] = mem_type.value
            mem_copy["status"] = mem_status.value
            enriched.append(mem_copy)

        enriched.sort(key=lambda m: -m["decayed_score"])
        return enriched


# ── Facade ────────────────────────────────────────────────────────────────

class TemporalValidityManager:
    """Extract temporal metadata and compute time-based decay weights.

    Usage::

        tvm = TemporalValidityManager()
        meta = tvm.extract_temporal_metadata(
            memory_id="mem_1",
            text="Alice started working at Google in March 2024",
        )
        # meta.event_timestamp, meta.precision, meta.status, meta.memory_type

        weight = tvm.compute_decay(
            memory_id="mem_1",
            base_weight=1.0,
            event_timestamp=meta.event_timestamp,
            memory_type=meta.memory_type,
        )
        # weight.decayed_weight
    """

    def __init__(
        self,
        default_half_life_days: float = 90.0,
        half_life_by_type: Optional[Dict[MemoryType, float]] = None,
    ):
        self._scheduler = _ExpiryScheduler()
        self._booster = _RecencyBooster(
            default_half_life_days=default_half_life_days,
            half_life_by_type=half_life_by_type,
        )

    @property
    def default_half_life(self) -> float:
        return self._booster.default_half_life

    # ── Extraction ────────────────────────────────────────────────────

    def extract_temporal_metadata(
        self,
        memory_id: str,
        text: str,
        known_timestamp: Optional[float] = None,
        known_precision: Optional[TemporalPrecision] = None,
    ) -> TemporalMetadata:
        return self._scheduler.extract_temporal_metadata(
            memory_id=memory_id,
            text=text,
            known_timestamp=known_timestamp,
            known_precision=known_precision,
        )

    # ── Decay ─────────────────────────────────────────────────────────

    def compute_decay(
        self,
        memory_id: str,
        base_weight: float,
        event_timestamp: Optional[float],
        memory_type: MemoryType = MemoryType.UNKNOWN,
        reference_time: Optional[float] = None,
    ) -> DecayWeight:
        return self._booster.compute_decay(
            memory_id=memory_id,
            base_weight=base_weight,
            event_timestamp=event_timestamp,
            memory_type=memory_type,
            reference_time=reference_time,
        )

    def apply_decay_to_memories(
        self,
        memories: List[Dict[str, Any]],
        reference_time: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        return self._booster.apply_decay_to_memories(memories, reference_time)

    # ── Filter ────────────────────────────────────────────────────────

    def filter_by_time_relevance(
        self,
        memories: List[Dict[str, Any]],
        query: str,
        reference_time: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        return self._booster.filter_by_time_relevance(memories, query, reference_time)

    # ── Batch ─────────────────────────────────────────────────────────

    def extract_batch(
        self,
        memories: List[Dict[str, Any]],
    ) -> Dict[str, TemporalMetadata]:
        return self._scheduler.extract_batch(memories)

    def get_summary_stats(
        self, metadata_map: Dict[str, TemporalMetadata]
    ) -> Dict[str, Any]:
        return _ExpiryScheduler.get_summary_stats(metadata_map)
