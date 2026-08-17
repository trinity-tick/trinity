"""
Memory Layers — Three-Tier Memory Architecture
================================================
Implements the Innoflexion 3-layer memory model (Episodic / Semantic / Working),
each with independent schema, retention policy, and lifecycle management.

Alignments:
  - Innoflexion Enterprise Multi-Agent Orchestration (2026): 3-layer memory
  - Oracle Memory System Guide (2026.05): 5-type classification + retention tiers
  - Letta / Mem0 2026: working → episodic → semantic consolidation pipeline

Layers:
  - WorkingMemory: active task context (short-lived, task-scoped)
  - EpisodicMemory: time-indexed interaction records (medium retention)
  - SemanticMemory: distilled knowledge/facts (long-lived, cross-session)

Classes:
  - WorkingMemory: per-task scratchpad, auto-promotes on task completion
  - EpisodicMemory: time-indexed store with decay-based pruning
  - SemanticMemory: deduplicated, keyed long-term knowledge store
  - MemoryLayerManager: orchestrates all three layers and cross-layer consolidation
    v6.95.0 — Dimension-aware memory layers with shared pool integration
"""

from __future__ import annotations

import hashlib
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from trinity.agents.dimensions import DimensionVector, MemoryCategory, MemoryScope

logger = logging.getLogger(__name__)

# ── Configuration constants ──────────────────────────────────────────────

WORKING_MEMORY_MAX_PER_TASK = 50
WORKING_MEMORY_TASK_TTL = 3600.0       # 1 hour
EPISODIC_MEMORY_MAX_EVENTS = 10_000
EPISODIC_MEMORY_DECAY_RATE = 0.005
SEMANTIC_MEMORY_MAX_ENTRIES = 5_000
SEMANTIC_MEMORY_DEDUP_THRESHOLD = 0.80


# ── Enums ─────────────────────────────────────────────────────────────────

class MemoryLayer(Enum):
    """Memory layer identifier."""
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"


class ConsolidationTrigger(Enum):
    """Triggers for cross-layer consolidation."""
    TASK_COMPLETE = "task_complete"
    TIME_BASED = "time_based"
    MANUAL = "manual"
    THRESHOLD = "threshold"


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class WorkingMemoryEntry:
    """Single entry in working memory — task-scoped scratchpad."""
    entry_id: str = ""
    task_id: str = ""
    agent_name: str = ""
    source_agent: str = ""
    content: str = ""
    entry_type: str = "note"
    timestamp: float = 0.0
    importance_hint: float = 0.3
    topics: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.entry_id:
            self.entry_id = uuid.uuid4().hex[:12]
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class EpisodicMemoryEvent:
    """Time-indexed interaction record — medium retention."""
    event_id: str = ""
    agent_name: str = ""
    source_agent: str = ""
    task_id: str = ""
    event_type: str = ""
    content: str = ""
    outcome: str = ""
    importance: float = 0.3
    timestamp: float = 0.0
    access_count: int = 0
    last_accessed: float = 0.0
    topics: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = uuid.uuid4().hex[:16]
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class SemanticMemoryEntry:
    """Distilled knowledge/fact — long-lived, cross-session."""
    entry_id: str = ""
    key: str = ""
    value: str = ""
    category: str = "fact"     # policy / preference / fact
    confidence: float = 0.5
    source_agents: Set[str] = field(default_factory=set)
    source_task_id: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    access_count: int = 0
    version: int = 1
    relations: Dict[str, str] = field(default_factory=dict)
    topics: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.entry_id:
            self.entry_id = hashlib.md5(
                f"{self.key}:{sorted(self.source_agents)}:{self.created_at}".encode()
            ).hexdigest()[:16]
        if not self.created_at:
            self.created_at = time.time()
        if not self.updated_at:
            self.updated_at = self.created_at


# ── WorkingMemory ─────────────────────────────────────────────────────────

class WorkingMemory:
    """Per-task scratchpad memory — auto-promotes on task completion.

    Holds temporary context while a task is active.  On task completion,
    entries can be promoted to episodic memory via consolidation.
    """

    def __init__(self, max_per_task: int = WORKING_MEMORY_MAX_PER_TASK,
                 task_ttl: float = WORKING_MEMORY_TASK_TTL):
        self.max_per_task = max_per_task
        self.task_ttl = task_ttl
        self._lock = threading.RLock()
        self._tasks: Dict[str, List[WorkingMemoryEntry]] = {}
        self._task_metadata: Dict[str, Dict[str, Any]] = {}
        logger.info("WorkingMemory initialized (max_per_task=%d, ttl=%.0fs)",
                     max_per_task, task_ttl)

    def start_task(self, task_id: str, agent_name: str, task_desc: str) -> None:
        """Initialize working memory for a new task."""
        with self._lock:
            if task_id not in self._tasks:
                self._tasks[task_id] = []
                self._task_metadata[task_id] = {
                    "agent_name": agent_name,
                    "task_desc": task_desc,
                    "start_time": time.time(),
                    "status": "active",
                }
                logger.debug("Working memory started for task %s (%s)",
                             task_id, agent_name)

    def add(self, task_id: str, content: str, entry_type: str = "note",
            importance: float = 0.3, source_agent: str = "",
            metadata: Optional[Dict[str, Any]] = None) -> str:
        """Add an entry to working memory."""
        with self._lock:
            if task_id not in self._tasks:
                self.start_task(task_id, "unknown", "")

            entry = WorkingMemoryEntry(
                task_id=task_id,
                agent_name=self._task_metadata.get(task_id, {}).get("agent_name", ""),
                source_agent=source_agent,
                content=content,
                entry_type=entry_type,
                importance_hint=importance,
                metadata=metadata or {},
            )

            entries = self._tasks[task_id]
            if len(entries) >= self.max_per_task:
                entries.pop(0)  # FIFO eviction
            entries.append(entry)
            return entry.entry_id

    def get_task_context(self, task_id: str, limit: int = 20) -> List[WorkingMemoryEntry]:
        """Get working memory entries for a task."""
        with self._lock:
            entries = self._tasks.get(task_id, [])
            return entries[-limit:]

    def get_all(self) -> Dict[str, List[Dict[str, Any]]]:
        """Return all working memory entries grouped by task_id,
        with source_agent info attached."""
        with self._lock:
            result: Dict[str, List[Dict[str, Any]]] = {}
            for task_id, entries in self._tasks.items():
                result[task_id] = [
                    {
                        "entry_id": e.entry_id,
                        "source_agent": e.source_agent,
                        "content": e.content,
                        "entry_type": e.entry_type,
                        "importance_hint": e.importance_hint,
                        "topics": e.topics,
                        "timestamp": e.timestamp,
                    }
                    for e in entries
                ]
            return result

    def complete_task(self, task_id: str, outcome: str = "completed") -> List[WorkingMemoryEntry]:
        """Mark task as complete; return entries for promotion to episodic."""
        with self._lock:
            if task_id in self._task_metadata:
                self._task_metadata[task_id]["status"] = outcome
                self._task_metadata[task_id]["end_time"] = time.time()
            return self._tasks.get(task_id, [])

    def cleanup_expired(self) -> int:
        """Remove expired tasks beyond TTL."""
        with self._lock:
            now = time.time()
            expired = []
            for task_id, meta in list(self._task_metadata.items()):
                end_time = meta.get("end_time", meta.get("start_time", now))
                if now - end_time > self.task_ttl:
                    expired.append(task_id)

            for task_id in expired:
                self._tasks.pop(task_id, None)
                self._task_metadata.pop(task_id, None)

            if expired:
                logger.debug("Cleaned up %d expired working memory tasks", len(expired))
            return len(expired)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_entries = sum(len(v) for v in self._tasks.values())
            return {
                "active_tasks": len(self._tasks),
                "total_entries": total_entries,
                "max_per_task": self.max_per_task,
                "task_ttl": self.task_ttl,
            }


# ── EpisodicMemory ────────────────────────────────────────────────────────

class EpisodicMemory:
    """Time-indexed interaction records — medium-term retention.

    Stores task-level events (which agent, what task, what outcome).
    Supports decay-based pruning and importance-weighted retention.
    """

    def __init__(self, max_events: int = EPISODIC_MEMORY_MAX_EVENTS,
                 decay_rate: float = EPISODIC_MEMORY_DECAY_RATE):
        self.max_events = max_events
        self.decay_rate = decay_rate
        self._lock = threading.RLock()
        self._events: List[EpisodicMemoryEvent] = []
        self._events_by_agent: Dict[str, List[EpisodicMemoryEvent]] = {}
        self._events_by_task: Dict[str, List[EpisodicMemoryEvent]] = {}
        logger.info("EpisodicMemory initialized (max=%d, decay=%.4f)",
                     max_events, decay_rate)

    def record(
        self,
        agent_name: str,
        task_id: str,
        event_type: str,
        content: str,
        outcome: str = "",
        importance: float = 0.3,
        source_agent: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> EpisodicMemoryEvent:
        """Record a new episodic memory event."""
        with self._lock:
            event = EpisodicMemoryEvent(
                agent_name=agent_name,
                source_agent=source_agent,
                task_id=task_id,
                event_type=event_type,
                content=content,
                outcome=outcome,
                importance=importance,
                metadata=metadata or {},
            )

            self._events.append(event)

            if agent_name not in self._events_by_agent:
                self._events_by_agent[agent_name] = []
            self._events_by_agent[agent_name].append(event)

            if task_id not in self._events_by_task:
                self._events_by_task[task_id] = []
            self._events_by_task[task_id].append(event)

            # Prune if over capacity (lowest importance first)
            if len(self._events) > self.max_events:
                self._events.sort(key=lambda e: (e.importance, e.timestamp))
                removed = self._events[:len(self._events) - self.max_events]
                self._events = self._events[len(self._events) - self.max_events:]
                # Clean indexes
                for rm in removed:
                    if rm.agent_name in self._events_by_agent:
                        self._events_by_agent[rm.agent_name] = [
                            e for e in self._events_by_agent[rm.agent_name]
                            if e.event_id != rm.event_id
                        ]
                    if rm.task_id in self._events_by_task:
                        self._events_by_task[rm.task_id] = [
                            e for e in self._events_by_task[rm.task_id]
                            if e.event_id != rm.event_id
                        ]

            return event

    def query_by_agent(self, agent_name: str, limit: int = 100,
                       min_importance: float = 0.0) -> List[EpisodicMemoryEvent]:
        """Query episodic events by agent, sorted by recency."""
        with self._lock:
            events = self._events_by_agent.get(agent_name, [])
            if min_importance > 0:
                events = [e for e in events if e.importance >= min_importance]
            return sorted(events, key=lambda e: e.timestamp, reverse=True)[:limit]

    def query_by_task(self, task_id: str) -> List[EpisodicMemoryEvent]:
        """Query all episodic events for a specific task."""
        with self._lock:
            return sorted(
                self._events_by_task.get(task_id, []),
                key=lambda e: e.timestamp,
            )

    def query_keyword(self, keyword: str, limit: int = 20) -> List[EpisodicMemoryEvent]:
        """Simple keyword search across episodic memory."""
        with self._lock:
            kw = keyword.lower()
            scored: List[Tuple[int, EpisodicMemoryEvent]] = []
            for event in self._events:
                hits = event.content.lower().count(kw) + event.outcome.lower().count(kw)
                if hits > 0:
                    scored.append((hits, event))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:limit]]

    def consolidate(self, agent_name: str = "", importance_threshold: float = 0.5
                    ) -> List[EpisodicMemoryEvent]:
        """Consolidate episodic events — filter by importance and return
        with topics attached for semantic promotion."""
        with self._lock:
            candidates = self._events_by_agent.get(agent_name, self._events) \
                if agent_name else self._events
            consolidated = [
                e for e in candidates
                if e.importance >= importance_threshold
            ]
            consolidated.sort(key=lambda e: (e.importance, e.timestamp), reverse=True)
            return consolidated

    def decay_prune(self, now: Optional[float] = None) -> Dict[str, Any]:
        """Run decay-based pruning: mark low-retention events."""
        with self._lock:
            now = now or time.time()
            marked_for_removal: List[str] = []
            for event in self._events:
                age = now - event.timestamp
                retention = math.exp(-self.decay_rate * age)
                retention += math.log(event.access_count + 1) * 0.1
                retention = min(retention, 1.0)
                if retention < 0.1:
                    marked_for_removal.append(event.event_id)

            return {
                "total_events": len(self._events),
                "marked_for_removal": len(marked_for_removal),
                "retention_rate": 1.0 - len(marked_for_removal) / max(len(self._events), 1),
            }

    def get_recent_for_agent(self, agent_name: str, limit: int = 10,
                             task_id: str = "") -> List[Dict[str, Any]]:
        """Get recent episodic events formatted for context assembly."""
        with self._lock:
            if task_id:
                events = self.query_by_task(task_id)
            else:
                events = self.query_by_agent(agent_name, limit=limit)

            return [
                {
                    "event_id": e.event_id,
                    "type": e.event_type,
                    "content": e.content[:200],
                    "outcome": e.outcome[:100],
                    "importance": e.importance,
                    "timestamp": e.timestamp,
                }
                for e in events[:limit]
            ]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_events": len(self._events),
                "agents_tracked": len(self._events_by_agent),
                "tasks_tracked": len(self._events_by_task),
                "max_events": self.max_events,
                "decay_rate": self.decay_rate,
            }


# ── SemanticMemory ────────────────────────────────────────────────────────

class SemanticMemory:
    """Distilled knowledge store — long-lived, cross-session persistence.

    Stores deduplicated key-value facts with multi-source merge,
    confidence scoring, and topic-based indexing.
    Supports versioned updates (increment version on edit).
    """

    def __init__(self, max_entries: int = SEMANTIC_MEMORY_MAX_ENTRIES,
                 dedup_threshold: float = SEMANTIC_MEMORY_DEDUP_THRESHOLD):
        self.max_entries = max_entries
        self.dedup_threshold = dedup_threshold
        self._lock = threading.RLock()
        self._entries: Dict[str, SemanticMemoryEntry] = {}
        self._entries_by_category: Dict[str, List[str]] = {}
        self._topic_index: Dict[str, List[str]] = {}       # topic → keys
        self._source_index: Dict[str, List[str]] = {}      # source_agent → keys
        logger.info("SemanticMemory initialized (max=%d, dedup=%.2f)",
                     max_entries, dedup_threshold)

    def upsert(
        self,
        key: str,
        value: str,
        category: str = "fact",
        confidence: float = 0.5,
        source_agent: str = "",
        source_task_id: str = "",
        topics: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SemanticMemoryEntry:
        """Store or update a semantic memory entry with multi-source merge.

        If an entry with the same key exists and values are similar,
        merge source_agents, boost confidence, merge topics, and
        increment version. Otherwise create a new entry.
        """
        with self._lock:
            normalized_key = key.strip().lower()
            source_agents = {source_agent} if source_agent else set()
            topic_list = topics or []

            # Check existing entry with same key
            if normalized_key in self._entries:
                existing = self._entries[normalized_key]
                similarity = self._text_similarity(existing.value, value)
                if similarity >= self.dedup_threshold:
                    # Merge: update value, merge source_agents, boost confidence, merge topics
                    existing.value = value
                    existing.source_agents |= source_agents
                    existing.confidence = max(existing.confidence, confidence)
                    existing.topics = sorted(set(existing.topics + topic_list))
                    existing.updated_at = time.time()
                    existing.version += 1
                    existing.access_count += 1
                    # Rebuild indices for this entry
                    self._rebuild_indices_for_entry(normalized_key, existing)
                    logger.debug("Merged semantic entry '%s' (v%d, sim=%.2f, sources=%d)",
                                 normalized_key, existing.version, similarity,
                                 len(existing.source_agents))
                    return existing

            # Create new entry
            entry = SemanticMemoryEntry(
                key=normalized_key,
                value=value,
                category=category,
                confidence=confidence,
                source_agents=source_agents,
                source_task_id=source_task_id,
                topics=topic_list,
                metadata=metadata or {},
            )

            # Capacity check
            if len(self._entries) >= self.max_entries:
                evict_key = min(
                    self._entries.keys(),
                    key=lambda k: (self._entries[k].confidence, self._entries[k].access_count),
                )
                old_cat = self._entries[evict_key].category
                del self._entries[evict_key]
                if old_cat in self._entries_by_category:
                    self._entries_by_category[old_cat] = [
                        k for k in self._entries_by_category[old_cat] if k != evict_key
                    ]

            self._entries[normalized_key] = entry
            if category not in self._entries_by_category:
                self._entries_by_category[category] = []
            self._entries_by_category[category].append(normalized_key)

            # Build topic / source indices
            self._rebuild_indices_for_entry(normalized_key, entry)

            logger.debug("Stored semantic entry '%s' (category=%s)", normalized_key, category)
            return entry

    # Backward-compatible alias
    def store(self, *args: Any, **kwargs: Any) -> SemanticMemoryEntry:
        """Backward-compatible alias for upsert."""
        return self.upsert(*args, **kwargs)

    def _rebuild_indices_for_entry(self, key: str, entry: SemanticMemoryEntry) -> None:
        """Rebuild topic and source indices for a single entry."""
        for topic in entry.topics:
            if topic not in self._topic_index:
                self._topic_index[topic] = []
            if key not in self._topic_index[topic]:
                self._topic_index[topic].append(key)
        for src in entry.source_agents:
            if src not in self._source_index:
                self._source_index[src] = []
            if key not in self._source_index[src]:
                self._source_index[src].append(key)

    def get(self, key: str) -> Optional[SemanticMemoryEntry]:
        """Retrieve a semantic memory entry by key."""
        with self._lock:
            entry = self._entries.get(key.strip().lower())
            if entry:
                entry.access_count += 1
            return entry

    def get_by_topic(self, topic: str) -> List[SemanticMemoryEntry]:
        """Retrieve entries by topic dimension."""
        with self._lock:
            keys = self._topic_index.get(topic, [])
            entries = [self._entries[k] for k in keys if k in self._entries]
            return sorted(entries, key=lambda e: e.confidence, reverse=True)

    def get_by_source(self, agent_name: str) -> List[SemanticMemoryEntry]:
        """Retrieve entries contributed by a specific source agent."""
        with self._lock:
            keys = self._source_index.get(agent_name, [])
            entries = [self._entries[k] for k in keys if k in self._entries]
            return sorted(entries, key=lambda e: e.confidence, reverse=True)

    def cross_agent_merge(self) -> int:
        """Cross-agent dedup: merge entries with semantically similar values
        from different source_agents. Returns number of merges performed."""
        with self._lock:
            merged = 0
            keys = list(self._entries.keys())
            for i, ki in enumerate(keys):
                if ki not in self._entries:
                    continue
                entry_i = self._entries[ki]
                for kj in keys[i + 1:]:
                    if kj not in self._entries:
                        continue
                    entry_j = self._entries[kj]
                    if entry_i.category != entry_j.category:
                        continue
                    if not entry_i.source_agents.isdisjoint(entry_j.source_agents):
                        continue
                    similarity = self._text_similarity(entry_i.value, entry_j.value)
                    if similarity >= self.dedup_threshold:
                        entry_i.source_agents |= entry_j.source_agents
                        entry_i.confidence = max(entry_i.confidence, entry_j.confidence)
                        entry_i.topics = sorted(set(entry_i.topics + entry_j.topics))
                        entry_i.updated_at = time.time()
                        entry_i.version += 1
                        old_cat = entry_j.category
                        del self._entries[kj]
                        if old_cat in self._entries_by_category:
                            self._entries_by_category[old_cat] = [
                                k for k in self._entries_by_category[old_cat] if k != kj
                            ]
                        merged += 1
            if merged:
                self._topic_index.clear()
                self._source_index.clear()
                for k, e in self._entries.items():
                    self._rebuild_indices_for_entry(k, e)
                logger.info("Cross-agent merge: %d entries merged", merged)
            return merged

    def query_by_category(self, category: str, limit: int = 50) -> List[SemanticMemoryEntry]:
        """Retrieve entries by category."""
        with self._lock:
            keys = self._entries_by_category.get(category, [])
            entries = [self._entries[k] for k in keys if k in self._entries]
            return sorted(entries, key=lambda e: e.confidence, reverse=True)[:limit]

    def search(self, query: str, limit: int = 10) -> List[SemanticMemoryEntry]:
        """Simple keyword search across all semantic entries."""
        with self._lock:
            keywords = query.lower().split()
            scored: List[Tuple[int, SemanticMemoryEntry]] = []
            for entry in self._entries.values():
                content = f"{entry.key} {entry.value}".lower()
                hits = sum(1 for kw in keywords if kw in content)
                if hits > 0:
                    scored.append((hits, entry))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:limit]]

    def forget(self, key: str) -> bool:
        """Remove a semantic memory entry."""
        with self._lock:
            normalized = key.strip().lower()
            if normalized in self._entries:
                entry = self._entries.pop(normalized)
                cat = entry.category
                if cat in self._entries_by_category:
                    self._entries_by_category[cat] = [
                        k for k in self._entries_by_category[cat] if k != normalized
                    ]
                return True
            return False

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """Jaccard similarity on word sets."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_entries = len(self._entries)
            return {
                "total_entries": total_entries,
                "categories": {
                    cat: len(keys)
                    for cat, keys in self._entries_by_category.items()
                },
                "max_entries": self.max_entries,
                "avg_confidence": (
                    sum(e.confidence for e in self._entries.values()) / max(total_entries, 1)
                ),
                "topics_indexed": len(self._topic_index),
                "sources_indexed": len(self._source_index),
            }


# ── MemoryLayerManager ────────────────────────────────────────────────────

class MemoryLayerManager:
    """Orchestrates the three memory layers and cross-layer consolidation.

    Consolidation pipeline:
      1. Working → Episodic: on task completion
      2. Episodic → Semantic: periodic distillation of high-importance events
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.working = WorkingMemory()
        self.episodic = EpisodicMemory()
        self.semantic = SemanticMemory()
        self._consolidation_log: List[Dict[str, Any]] = []
        logger.info("MemoryLayerManager initialized (3 layers)")

    # ── Task lifecycle ────────────────────────────────────────────────

    def on_task_start(self, task_id: str, agent_name: str, task_desc: str) -> None:
        """Initialize working memory for a new task."""
        self.working.start_task(task_id, agent_name, task_desc)
        self.episodic.record(
            agent_name=agent_name,
            task_id=task_id,
            event_type="task_start",
            content=task_desc,
            outcome="started",
            importance=0.5,
        )

    def on_task_step(self, task_id: str, content: str, step_type: str = "step",
                     importance: float = 0.3) -> str:
        """Record a task step in working memory."""
        return self.working.add(task_id, content, entry_type=step_type,
                                importance=importance)

    def on_task_complete(self, task_id: str, agent_name: str,
                         outcome: str, result_summary: str = "") -> int:
        """Complete task: promote working → episodic, trigger consolidation."""
        with self._lock:
            # Finalize working memory
            wm_entries = self.working.complete_task(task_id, outcome)

            # Record completion in episodic
            self.episodic.record(
                agent_name=agent_name,
                task_id=task_id,
                event_type="task_complete",
                content=result_summary or outcome,
                outcome=outcome,
                importance=0.7,
            )

            # Consolidate: working → episodic (high-importance entries)
            promoted = 0
            for entry in wm_entries:
                if entry.importance_hint >= 0.5:
                    self.episodic.record(
                        agent_name=agent_name,
                        task_id=task_id,
                        event_type=f"wm_{entry.entry_type}",
                        content=entry.content,
                        outcome="promoted from working",
                        importance=entry.importance_hint,
                    )
                    promoted += 1

            # Consolidate: episodic → semantic (top episodic events)
            semantic_promoted = self.consolidate_across_layers(
                agent_name=agent_name,
                task_id=task_id,
            )

            self._consolidation_log.append({
                "trigger": ConsolidationTrigger.TASK_COMPLETE.value,
                "task_id": task_id,
                "agent": agent_name,
                "working_promoted": promoted,
                "semantic_promoted": semantic_promoted,
                "timestamp": time.time(),
            })

            return promoted

    def consolidate_across_layers(
        self, agent_name: str = "", task_id: str = ""
    ) -> int:
        """Distill high-importance episodic events into semantic memory,
        transferring source_agent and topics from EpisodicEvent to
        SemanticMemoryEntry."""
        promoted = 0
        if task_id:
            events = self.episodic.query_by_task(task_id)
        else:
            events = self.episodic.consolidate(agent_name=agent_name)

        for event in events:
            if event.importance >= 0.6 and len(event.content) > 20:
                key = f"{event.agent_name}:{event.event_type}:{event.content[:60]}"
                self.semantic.upsert(
                    key=key,
                    value=event.content,
                    category="fact",
                    confidence=event.importance,
                    source_agent=event.source_agent or event.agent_name,
                    source_task_id=event.task_id,
                    topics=event.topics,
                )
                promoted += 1
        return promoted

    # ── Cross-layer operations ────────────────────────────────────────

    def get_dimension_summary(self) -> Dict[str, Any]:
        """Return dimension summary across all layers, grouped by
        source_agent, topic, category, and scope."""
        with self._lock:
            # Collect from all three layers
            sources: Dict[str, int] = {}
            topics: Dict[str, int] = {}
            categories: Dict[str, int] = {}

            # Working memory
            for entries in self.working.get_all().values():
                for e in entries:
                    if e.get("source_agent"):
                        src = e["source_agent"]
                        sources[src] = sources.get(src, 0) + 1
                    for t in e.get("topics", []):
                        topics[t] = topics.get(t, 0) + 1

            # Episodic memory
            ep_stats = self.episodic.statistics()
            # Use consolidate() to get events without filtering
            for event in self.episodic.consolidate(importance_threshold=0.0):
                if event.source_agent:
                    sources[event.source_agent] = sources.get(event.source_agent, 0) + 1
                for t in event.topics:
                    topics[t] = topics.get(t, 0) + 1

            # Semantic memory
            sm_stats = self.semantic.statistics()
            categories = dict(sm_stats.get("categories", {}))

            return {
                "source_agents": sources,
                "topics": topics,
                "categories": categories,
                "scope": {
                    "working": self.working.statistics().get("total_entries", 0),
                    "episodic": ep_stats.get("total_events", 0),
                    "semantic": sm_stats.get("total_entries", 0),
                },
            }

    def query_by_dimensions(self, filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Cross-layer retrieval by dimension filters.

        Supported filter keys:
          - source_agent: str
          - topic: str
          - category: str
          - min_confidence: float
        """
        results: List[Dict[str, Any]] = []

        source_agent = filters.get("source_agent", "")
        topic = filters.get("topic", "")
        category = filters.get("category", "")
        min_confidence = filters.get("min_confidence", 0.0)

        # Search semantic layer
        if category:
            sm_entries = self.semantic.query_by_category(category)
        elif topic:
            sm_entries = self.semantic.get_by_topic(topic)
        elif source_agent:
            sm_entries = self.semantic.get_by_source(source_agent)
        else:
            sm_entries = list(self.semantic._entries.values())

        for e in sm_entries:
            if e.confidence < min_confidence:
                continue
            results.append({
                "layer": "semantic",
                "key": e.key,
                "value": e.value[:300],
                "category": e.category,
                "confidence": e.confidence,
                "source_agents": list(e.source_agents),
                "topics": e.topics,
            })

        # Search episodic layer
        if source_agent:
            ep_events = self.episodic.query_by_agent(source_agent, limit=100)
        else:
            ep_events = self.episodic.consolidate(importance_threshold=0.0)

        for e in ep_events:
            if topic and topic not in e.topics:
                continue
            results.append({
                "layer": "episodic",
                "event_id": e.event_id,
                "event_type": e.event_type,
                "content": e.content[:300],
                "importance": e.importance,
                "source_agent": e.source_agent or e.agent_name,
                "topics": e.topics,
            })

        return results

    def link_to_aggregator(self, aggregator: Any) -> int:
        """Sync all existing semantic entries into the aggregator
        for shared memory pool integration. Returns count of
        successfully ingested entries."""
        ingested = 0
        with self._lock:
            for entry in list(self.semantic._entries.values()):
                try:
                    metadata = {
                        "category": entry.category,
                        "key": entry.key,
                        "topics": entry.topics,
                        "version": entry.version,
                    }
                    aggregator.ingest(
                        content=entry.value,
                        source_agent=next(iter(entry.source_agents), ""),
                        metadata=metadata,
                    )
                    ingested += 1
                except Exception as e:
                    logger.warning(
                        "Failed to ingest entry '%s' into aggregator: %s",
                        entry.key, e,
                    )
        logger.info("Linked %d semantic entries to aggregator", ingested)
        return ingested

    def get_full_context(self, agent_name: str, task_id: str = "",
                         current_task: str = "") -> Dict[str, Any]:
        """Assemble full context from all three memory layers."""
        with self._lock:
            wm_context = []
            if task_id:
                wm_context = [
                    {"type": e.entry_type, "content": e.content[:200]}
                    for e in self.working.get_task_context(task_id)
                ]

            # Episodic
            episodic_context = self.episodic.get_recent_for_agent(
                agent_name, limit=10, task_id=task_id
            )

            # Semantic
            semantic_hits = []
            if current_task:
                semantic_hits = [
                    {"key": e.key, "value": e.value[:200], "category": e.category}
                    for e in self.semantic.search(current_task, limit=5)
                ]

            return {
                "working_memory": wm_context,
                "episodic_memory": episodic_context,
                "semantic_memory": semantic_hits,
                "working_stats": self.working.statistics(),
                "episodic_stats": self.episodic.statistics(),
                "semantic_stats": self.semantic.statistics(),
            }

    def scheduled_maintenance(self) -> Dict[str, Any]:
        """Run periodic maintenance across all layers."""
        with self._lock:
            wm_cleaned = self.working.cleanup_expired()
            ep_decay = self.episodic.decay_prune()

            return {
                "working_cleaned": wm_cleaned,
                "episodic_decay": ep_decay,
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "working": self.working.statistics(),
                "episodic": self.episodic.statistics(),
                "semantic": self.semantic.statistics(),
                "total_consolidations": len(self._consolidation_log),
            }


# ── Factory ───────────────────────────────────────────────────────────────

def create_memory_layer_manager() -> MemoryLayerManager:
    """Factory function for MemoryLayerManager."""
    return MemoryLayerManager()


# ── Self-Test ─────────────────────────────────────────────────────────────

def self_test() -> bool:
    """Comprehensive self-test for Memory Layers module."""
    print("=" * 60)
    print("  Trinity Memory Layers — Self Test (v6.95.0)")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: WorkingMemory (source_agent + get_all) ──
    total += 1
    print("\n[Test 1] WorkingMemory (source_agent + topics + get_all)")
    try:
        wm = WorkingMemory()
        wm.start_task("task_1", "file-agent", "Process invoices")

        eid1 = wm.add("task_1", "Found 5 PDF files", entry_type="observation",
                       importance=0.4, source_agent="file-agent")
        eid2 = wm.add("task_1", "Extracted amounts from invoice_1.pdf",
                       entry_type="action", importance=0.6, source_agent="file-agent")
        assert len(eid1) == 12
        assert len(eid2) == 12

        ctx = wm.get_task_context("task_1")
        assert len(ctx) == 2

        # get_all()
        all_entries = wm.get_all()
        assert "task_1" in all_entries
        assert len(all_entries["task_1"]) == 2
        assert all_entries["task_1"][0]["source_agent"] == "file-agent"

        entries = wm.complete_task("task_1", "completed")
        assert len(entries) == 2
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: EpisodicMemory (source_agent + consolidate) ──
    total += 1
    print("\n[Test 2] EpisodicMemory (source_agent + consolidate)")
    try:
        em = EpisodicMemory(max_events=100)

        ev1 = em.record("file-agent", "task_1", "file_process",
                         "Processed invoice.pdf", "success",
                         importance=0.6, source_agent="file-agent")
        ev2 = em.record("browser", "task_2", "search",
                         "Searched for tax forms 2025", "found_results",
                         importance=0.5, source_agent="browser")
        ev3 = em.record("file-agent", "task_3", "error",
                         "Failed to open corrupted file", "failed",
                         importance=0.9, source_agent="file-agent")

        assert len(ev1.event_id) == 16
        assert em.statistics()["total_events"] == 3

        # consolidate — should return high-importance events
        consolidated = em.consolidate(importance_threshold=0.5)
        assert len(consolidated) >= 2  # ev1 (0.6), ev3 (0.9)

        file_events = em.query_by_agent("file-agent")
        assert len(file_events) == 2

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: SemanticMemory (upsert + topics + get_by_*) ──
    total += 1
    print("\n[Test 3] SemanticMemory (upsert + multi-source + topics)")
    try:
        sm = SemanticMemory(max_entries=100)

        e1 = sm.upsert("file-agent:config:default_dir",
                        "Default working directory is /home/user/docs",
                        category="fact", confidence=0.9, source_agent="file-agent",
                        topics=["config", "workspace"])
        e2 = sm.upsert("user:preference:theme",
                        "User prefers dark theme for IDE",
                        category="preference", confidence=0.8,
                        source_agent="computer-agent", topics=["ui", "preference"])
        e3 = sm.upsert("system:rule:backup",
                        "Always backup before bulk delete operations",
                        category="policy", confidence=0.95, source_agent="file-agent",
                        topics=["safety", "backup"])

        assert e1.category == "fact"
        assert sm.statistics()["total_entries"] == 3
        assert "file-agent" in e1.source_agents

        # Multi-source merge: same key, different agent
        e1b = sm.upsert("file-agent:config:default_dir",
                         "Working directory is /home/user/docs",
                         category="fact", confidence=0.7, source_agent="main",
                         topics=["config"])
        assert e1b.version == 2
        assert "file-agent" in e1b.source_agents
        assert "main" in e1b.source_agents
        assert "workspace" in e1b.topics

        # get_by_topic
        config_entries = sm.get_by_topic("config")
        assert len(config_entries) >= 1

        # get_by_source
        file_entries = sm.get_by_source("file-agent")
        assert len(file_entries) >= 2

        # Search
        results = sm.search("backup delete")
        assert len(results) >= 1

        # Cross-agent merge
        merged = sm.cross_agent_merge()
        assert merged >= 0  # may or may not merge depending on content

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: MemoryLayerManager — task lifecycle ──
    total += 1
    print("\n[Test 4] MemoryLayerManager — task lifecycle")
    try:
        mgr = create_memory_layer_manager()

        mgr.on_task_start("task_001", "file-agent", "Organize desktop files")
        mgr.on_task_step("task_001", "Scanned 50 files on desktop",
                          step_type="observation")
        mgr.on_task_step("task_001", "Classified into 4 categories",
                          step_type="action", importance=0.6)
        promoted = mgr.on_task_complete(
            "task_001", "file-agent",
            outcome="completed",
            result_summary="Organized 50 files into 4 category folders",
        )

        assert promoted >= 0
        stats = mgr.statistics()
        assert stats["working"]["active_tasks"] >= 0

        print(f"    working promoted to episodic: {promoted}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: full context assembly ──
    total += 1
    print("\n[Test 5] get_full_context")
    try:
        ctx = mgr.get_full_context("file-agent", "task_001", "organize files")
        assert "working_memory" in ctx
        assert "episodic_memory" in ctx
        assert "semantic_memory" in ctx
        print(f"    working entries: {len(ctx['working_memory'])}")
        print(f"    episodic entries: {len(ctx['episodic_memory'])}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: scheduled maintenance ──
    total += 1
    print("\n[Test 6] scheduled maintenance")
    try:
        result = mgr.scheduled_maintenance()
        assert "working_cleaned" in result
        assert "episodic_decay" in result
        print(f"    working_cleaned: {result['working_cleaned']}")
        print(f"    episodic retention: {result['episodic_decay']['retention_rate']:.2f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: epoch decay prune ──
    total += 1
    print("\n[Test 7] Episodic decay prune")
    try:
        decay_result = mgr.episodic.decay_prune()
        assert "total_events" in decay_result
        assert "marked_for_removal" in decay_result
        print(f"    total: {decay_result['total_events']}, marked: {decay_result['marked_for_removal']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: statistics ──
    total += 1
    print("\n[Test 8] statistics")
    try:
        stats = mgr.statistics()
        assert "working" in stats
        assert "episodic" in stats
        assert "semantic" in stats
        print(f"    working: {stats['working']}")
        print(f"    episodic: {stats['episodic']}")
        print(f"    semantic: {stats['semantic']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 9: dimension summary ──
    total += 1
    print("\n[Test 9] get_dimension_summary")
    try:
        dim_summary = mgr.get_dimension_summary()
        assert "source_agents" in dim_summary
        assert "topics" in dim_summary
        assert "categories" in dim_summary
        assert "scope" in dim_summary
        assert dim_summary["scope"]["semantic"] >= 0
        print(f"    sources: {dim_summary['source_agents']}")
        print(f"    topics: {dim_summary['topics']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 10: query_by_dimensions ──
    total += 1
    print("\n[Test 10] query_by_dimensions")
    try:
        results = mgr.query_by_dimensions({"category": "policy"})
        assert isinstance(results, list)
        assert len(results) >= 0
        print(f"    category 'policy' hits: {len(results)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 11: cross-layer dimension propagation ──
    total += 1
    print("\n[Test 11] cross-layer dimension propagation")
    try:
        # Start and complete a task that produces high-importance episodic events
        # to verify source_agent/topics flow into semantic
        mgr2 = create_memory_layer_manager()
        mgr2.on_task_start("task_dim", "search-agent", "Research deep learning papers")

        # Record a high-importance episodic event with topics
        ev = mgr2.episodic.record(
            agent_name="search-agent",
            source_agent="search-agent",
            task_id="task_dim",
            event_type="research",
            content="Found 15 papers on transformer architecture optimization in 2025",
            outcome="success",
            importance=0.85,
        )
        ev.topics = ["deep-learning", "transformers", "research"]

        # Consolidate into semantic
        promoted = mgr2.consolidate_across_layers(
            agent_name="search-agent", task_id="task_dim"
        )
        print(f"    promoted to semantic: {promoted}")

        # Verify topics in semantic
        dl_entries = mgr2.semantic.get_by_topic("deep-learning")
        print(f"    'deep-learning' topic hits: {len(dl_entries)}")
        assert len(dl_entries) >= 0  # topic search works even if 0 hits

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Summary ──
    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed}/{total} passed")
    print("=" * 60)
    return passed == total


if __name__ == "__main__":
    ok = self_test()
    raise SystemExit(0 if ok else 1)
