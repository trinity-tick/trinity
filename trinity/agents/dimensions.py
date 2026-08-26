# -*- coding: utf-8 -*-
"""
Dimension Engine — 8-Dimension Memory Indexing
==============================================
Eight-dimensional vector indexing for Trinity memory,
enabling fine-grained cross-dimension querying and
priority scoring.

Dimensions (aligned with Oracle 5-Type + Innoflexion):
  1. Source      — which Agents contributed this memory
  2. Topic       — auto-extracted keywords / named entities
  3. Temporal    — created_at / updated_at + time bucket
  4. Confidence  — single-source=0.5, +0.15 per confirming agent (max 1.0)
  5. Relational  — supports / contradicts / extends / duplicates
  6. Category    — Oracle 5-type (policy/preference/fact/episodic/trace)
  7. Scope       — local / cross_agent / global
  8. Priority    — composite: importance × recency × access_frequency

Classes:
  - DimensionVector: 8-dimension value encapsulation
  - DimensionEngine: index / query / boost / relation management
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ── Config Constants ──────────────────────────────────────────────────────

DEFAULT_CONFIDENCE = 0.5
CONFIDENCE_BOOST_PER_AGENT = 0.15
MAX_CONFIDENCE = 1.0
PRIORITY_RECENCY_HALFLIFE = 3600.0       # seconds
TOPIC_MIN_WORD_LEN = 3
TOPIC_MAX_TOPICS = 8
MAX_MEMORIES = 50000

# ── Dimension Enums ────────────────────────────────────────────────────────


class MemoryCategory(str, Enum):
    """Oracle 5-type memory classification."""
    POLICY = "policy"
    PREFERENCE = "preference"
    FACT = "fact"
    EPISODIC = "episodic"
    TRACE = "trace"


class MemoryScope(str, Enum):
    """Visibility scope of a memory."""
    LOCAL = "local"              # single Agent visible
    CROSS_AGENT = "cross_agent"  # multi-Agent shared
    GLOBAL = "global"            # all Agents visible


class RelationType(str, Enum):
    """Typed relationships between memory vectors."""
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    EXTENDS = "extends"
    DUPLICATES = "duplicates"


class TimeBucket(str, Enum):
    """Temporal granularity buckets."""
    MINUTE = "minute"
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"


# ── DimensionVector ────────────────────────────────────────────────────────


@dataclass
class DimensionVector:
    """Eight-dimension memory vector.

    Encapsulates all dimension values for a single memory unit,
    supporting multi-dimensional queries and priority scoring.
    """

    memory_id: str
    content: str
    source_agents: Set[str] = field(default_factory=set)
    topics: List[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    confidence: float = DEFAULT_CONFIDENCE
    relations: Dict[str, str] = field(default_factory=dict)
    category: str = MemoryCategory.EPISODIC.value
    scope: str = MemoryScope.LOCAL.value
    priority: float = 0.0

    # ── Lifecycle (P0-2) ──
    expire_at: Optional[float] = None   # Unix timestamp, None = never expires
    access_count: int = 0
    last_accessed: float = 0.0

    # ── Source status (P0-1, 2026-08-24 R8) ──
    # 源库（SQLite 引擎库）的记忆状态快照：active / archived / deleted。
    # 聚合池与引擎库检索口径统一的基石——引擎库只检索 active（1,882 条），
    # 聚合池此前无 status 概念（11,412 条含已归档），导致归档记忆仍可被
    # API/MCP 侧检索命中。None = 未知（旧数据，视为 active 兼容）。
    source_status: Optional[str] = None

    # ── Derived properties ──

    @property
    def age_seconds(self, now: Optional[float] = None) -> float:
        """Age of this memory in seconds."""
        now = now or time.time()
        return max(0.0, now - self.created_at)

    @property
    def time_bucket(self) -> str:
        """Determine the temporal bucket for this memory."""
        age = self.age_seconds
        if age < 3600:          # < 1 hour
            return TimeBucket.MINUTE.value
        elif age < 86400:       # < 1 day
            return TimeBucket.HOUR.value
        elif age < 604800:      # < 1 week
            return TimeBucket.DAY.value
        else:
            return TimeBucket.WEEK.value

    @property
    def source_count(self) -> int:
        """Number of distinct source agents."""
        return len(self.source_agents)

    def to_dict(self, full: bool = False) -> Dict[str, Any]:
        """Serialize to plain dict (source_agents → sorted list).

        Args:
            full: If True, include full content and all fields for persistence.
        """
        d = {
            "memory_id": self.memory_id,
            "content": self.content if full else self.content[:200],
            "source_agents": sorted(self.source_agents),
            "topics": self.topics,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "confidence": round(self.confidence, 3),
            "relations": dict(self.relations),
            "category": self.category,
            "scope": self.scope,
            "priority": round(self.priority, 4),
            "expire_at": self.expire_at,
            "access_count": self.access_count,
            "last_accessed": self.last_accessed,
            "source_status": self.source_status,
        }
        if not full:
            d["time_bucket"] = self.time_bucket
            d["source_count"] = self.source_count
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DimensionVector":
        """Deserialize from plain dict (persistence restore)."""
        return cls(
            memory_id=d["memory_id"],
            content=d["content"],
            source_agents=set(d.get("source_agents", [])),
            topics=d.get("topics", []),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            confidence=d.get("confidence", DEFAULT_CONFIDENCE),
            relations=d.get("relations", {}),
            category=d.get("category", MemoryCategory.EPISODIC.value),
            scope=d.get("scope", MemoryScope.LOCAL.value),
            priority=d.get("priority", 0.0),
            expire_at=d.get("expire_at", None),
            access_count=d.get("access_count", 0),
            last_accessed=d.get("last_accessed", 0.0),
            source_status=d.get("source_status", None),
        )


# ── DimensionEngine ────────────────────────────────────────────────────────


class DimensionEngine:
    """Eight-dimension memory indexing engine.

    Provides create / read / query / boost / relate operations
    across all eight dimensions, with thread-safe vector storage.

    Usage:
        engine = DimensionEngine()
        dv = engine.index_memory("dark mode preferred", "main")
        results = engine.query({"category": "preference", "scope": "global"})
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._vectors: Dict[str, DimensionVector] = {}
        self._topic_index: Dict[str, Set[str]] = {}       # topic → {memory_id}
        self._category_index: Dict[str, Set[str]] = {}    # category → {memory_id}
        self._scope_index: Dict[str, Set[str]] = {}       # scope → {memory_id}
        self._source_index: Dict[str, Set[str]] = {}      # agent_name → {memory_id}
        self._stats = {
            "total_indexed": 0,
            "total_queries": 0,
            "total_boosts": 0,
            "total_relations": 0,
            "pruned": 0,
        }
        logger.info("DimensionEngine initialized (max_memories=%d)", MAX_MEMORIES)

    # ── Public API ────────────────────────────────────────────────────────

    def index_memory(
        self,
        content: str,
        source_agent: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> DimensionVector:
        """Index a new memory and return its DimensionVector.

        Args:
            content: raw memory text
            source_agent: agent name that originated this memory
            metadata: optional {category, scope, confidence, ...} overrides

        Returns:
            DimensionVector with all 8 dimensions populated
        """
        with self._lock:
            # Enforce capacity
            self._enforce_capacity()

            metadata = metadata or {}
            now = time.time()
            memory_id = self._generate_id(content, source_agent)

            # Auto-extract topics
            topics = self.extract_topics(content)

            # Determine category
            category = metadata.get(
                "category",
                self._infer_category(content)
            )
            if isinstance(category, MemoryCategory):
                category = category.value

            # Determine scope
            scope = metadata.get("scope", MemoryScope.LOCAL.value)
            if isinstance(scope, MemoryScope):
                scope = scope.value

            # Build vector
            dv = DimensionVector(
                memory_id=memory_id,
                content=content,
                source_agents={source_agent},
                topics=topics,
                created_at=now,
                updated_at=now,
                confidence=metadata.get("confidence", DEFAULT_CONFIDENCE),
                relations={},
                category=category,
                scope=scope,
                priority=0.0,
            )

            # Compute initial priority
            dv.priority = self.compute_priority(dv)

            # Store
            self._vectors[memory_id] = dv
            self._stats["total_indexed"] += 1

            # Update indexes
            self._add_to_topic_index(memory_id, topics)
            self._add_to_category_index(memory_id, category)
            self._add_to_scope_index(memory_id, scope)
            self._add_to_source_index(memory_id, source_agent)

            logger.debug(
                "Indexed memory %s (source=%s, category=%s, scope=%s, "
                "topics=%s, priority=%.4f)",
                memory_id, source_agent, category, scope, topics, dv.priority,
            )
            return dv

    def query(self, dimension_filters: Dict[str, Any]) -> List[DimensionVector]:
        """Query memories by arbitrary dimension combination.

        Supported filter keys:
          - source_agent: str  →  match by source agent
          - topics: List[str]  →  match any topic (OR)
          - category: str      →  match exact category
          - scope: str         →  match exact scope
          - time_bucket: str   →  match temporal bucket
          - confidence_min: float → minimum confidence
          - confidence_max: float → maximum confidence
          - priority_min: float   → minimum priority
          - content_contains: str → substring match in content
          - memory_ids: List[str] → restrict to specific IDs

        Returns:
            List of matching DimensionVectors, sorted by priority descending
        """
        with self._lock:
            self._stats["total_queries"] += 1
            candidates: Optional[Set[str]] = None

            # source_agent filter
            if "source_agent" in dimension_filters:
                sa = dimension_filters["source_agent"]
                ids = self._source_index.get(sa, set())
                candidates = ids if candidates is None else candidates & ids

            # topics filter (OR match)
            if "topics" in dimension_filters:
                req_topics = dimension_filters["topics"]
                matching = set()
                for topic in req_topics:
                    if topic in self._topic_index:
                        matching |= self._topic_index[topic]
                candidates = matching if candidates is None else candidates & matching

            # category filter
            if "category" in dimension_filters:
                cat = dimension_filters["category"]
                ids = self._category_index.get(cat, set())
                candidates = ids if candidates is None else candidates & ids

            # scope filter
            if "scope" in dimension_filters:
                sc = dimension_filters["scope"]
                ids = self._scope_index.get(sc, set())
                candidates = ids if candidates is None else candidates & ids

            # memory_ids filter
            if "memory_ids" in dimension_filters:
                mids = set(dimension_filters["memory_ids"])
                candidates = mids if candidates is None else candidates & mids

            # No filter specified → return all
            if candidates is None:
                candidates = set(self._vectors.keys())

            # Build result set with scalar filters
            results = []
            for mid in candidates:
                dv = self._vectors.get(mid)
                if dv is None:
                    continue

                # confidence range filter
                if "confidence_min" in dimension_filters:
                    if dv.confidence < dimension_filters["confidence_min"]:
                        continue
                if "confidence_max" in dimension_filters:
                    if dv.confidence > dimension_filters["confidence_max"]:
                        continue

                # priority min filter
                if "priority_min" in dimension_filters:
                    if dv.priority < dimension_filters["priority_min"]:
                        continue

                # time_bucket filter
                if "time_bucket" in dimension_filters:
                    if dv.time_bucket != dimension_filters["time_bucket"]:
                        continue

                # content_contains filter
                if "content_contains" in dimension_filters:
                    sub = dimension_filters["content_contains"].lower()
                    if sub not in dv.content.lower():
                        continue

                results.append(dv)

            # Sort by priority descending
            results.sort(key=lambda dv: dv.priority, reverse=True)

            logger.debug(
                "Query returned %d results (filters=%s)",
                len(results), list(dimension_filters.keys()),
            )
            return results

    def compute_priority(self, dv: DimensionVector) -> float:
        """Compute composite priority score.

        Formula:
            priority = base_importance × recency_decay × access_bonus

        where:
            base_importance = f(category, content length, confidence)
            recency_decay    = 2^(-age / halflife)
            access_bonus     = 1 + log(source_count + 1) * 0.1
        """
        with self._lock:
            # Base importance from category
            category_weights = {
                MemoryCategory.POLICY.value: 0.9,
                MemoryCategory.PREFERENCE.value: 0.7,
                MemoryCategory.FACT.value: 0.8,
                MemoryCategory.EPISODIC.value: 0.5,
                MemoryCategory.TRACE.value: 0.2,
            }
            base = category_weights.get(dv.category, 0.5)

            # Length bonus
            if len(dv.content) > 200:
                base += 0.05

            # Confidence bonus
            base += dv.confidence * 0.1

            # Recency decay (exponential halflife)
            age = dv.age_seconds
            recency = 2.0 ** (-age / PRIORITY_RECENCY_HALFLIFE)

            # Source count bonus
            access_bonus = 1.0 + math.log(dv.source_count + 1) * 0.1

            priority = base * recency * access_bonus

            # Clamp
            return max(0.0, min(1.0, priority))

    def boost_confidence(
        self,
        dv: DimensionVector,
        new_agent: str,
        content_similar: bool,
    ) -> DimensionVector:
        """Boost confidence when an independent agent confirms the same fact.

        Only boosts if content_similar is True and the agent is not
        already in source_agents. Each confirming agent adds
        CONFIDENCE_BOOST_PER_AGENT (0.15) up to MAX_CONFIDENCE (1.0).

        Args:
            dv: the dimension vector to boost
            new_agent: confirming agent name
            content_similar: whether the new content is semantically similar

        Returns:
            Updated DimensionVector
        """
        with self._lock:
            if not content_similar:
                logger.debug("Confidence boost skipped (not similar)")
                return dv

            if new_agent in dv.source_agents:
                logger.debug("Confidence boost skipped (agent %s already in sources)", new_agent)
                return dv

            old_confidence = dv.confidence
            dv.source_agents.add(new_agent)
            dv.confidence = min(dv.confidence + CONFIDENCE_BOOST_PER_AGENT, MAX_CONFIDENCE)
            dv.updated_at = time.time()

            # Recompute priority
            dv.priority = self.compute_priority(dv)

            # Update source index
            self._add_to_source_index(dv.memory_id, new_agent)

            self._stats["total_boosts"] += 1
            logger.info(
                "Confidence boosted: %s %.3f→%.3f (agent=%s, sources=%d)",
                dv.memory_id, old_confidence, dv.confidence,
                new_agent, dv.source_count,
            )
            return dv

    def extract_topics(self, content: str) -> List[str]:
        """Extract topics/keywords from content.

        Uses heuristics:
          - Split into lowercase words
          - Filter: len >= TOPIC_MIN_WORD_LEN, not in stop words
          - Score by frequency (within single content)
          - Select top TOPIC_MAX_TOPICS
        """
        stop_words = {
            "the", "and", "for", "this", "that", "with", "from",
            "have", "was", "are", "not", "but", "you", "all",
            "can", "has", "had", "were", "been", "will", "would",
            "could", "should", "may", "did", "does", "its",
            "just", "like", "very", "also", "then", "than",
            "about", "some", "into", "over", "such", "when",
            "what", "which", "who", "how", "where", "after",
            "before", "each", "well", "too", "only", "other",
            "more", "much", "any", "most", "now", "new", "get",
            "use", "one", "two", "out", "up", "so", "if",
            "or", "by", "at", "on", "in", "to", "of", "it",
            "is", "be", "as", "an", "no", "we", "he", "she",
            "they", "them", "their", "his", "her", "our",
            "my", "me", "do", "go", "see",
        }

        # Normalize and tokenize
        words = re.findall(r'[a-zA-Z\u4e00-\u9fff_]+', content.lower())

        # Filter
        candidates = [
            w for w in words
            if len(w) >= TOPIC_MIN_WORD_LEN and w not in stop_words
        ]

        # Frequency scoring
        freq = Counter(candidates)

        # Also detect named-entity patterns (Capitalized words in original)
        original_words = re.findall(r'[a-zA-Z]+', content)
        capitalized = set(
            w for w in original_words
            if w[0].isupper() and len(w) >= 2 and w.lower() not in stop_words
        )

        # Rank: frequency * boost for capitalized (entity-like)
        scores = {}
        for word, count in freq.items():
            score = count
            if word in {c.lower() for c in capitalized}:
                score *= 1.5
            scores[word] = score

        # Select top N
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        topics = [word for word, _ in ranked[:TOPIC_MAX_TOPICS]]

        return topics

    def add_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: str,
    ) -> None:
        """Add a typed relation edge between two memory vectors.

        Args:
            source_id: memory_id of the source vector
            target_id: memory_id of the target vector
            relation_type: one of supports/contradicts/extends/duplicates
        """
        with self._lock:
            if source_id not in self._vectors:
                logger.warning("Relation source not found: %s", source_id)
                return
            if target_id not in self._vectors:
                logger.warning("Relation target not found: %s", target_id)
                return

            rt = relation_type
            if isinstance(relation_type, RelationType):
                rt = relation_type.value

            self._vectors[source_id].relations[target_id] = rt
            self._vectors[source_id].updated_at = time.time()
            self._stats["total_relations"] += 1

            logger.debug(
                "Added relation: %s --[%s]--> %s", source_id, rt, target_id
            )

    def find_contradictions(self, content: str) -> List[DimensionVector]:
        """Find memories that potentially contradict the given content.

        Checks for existing memories with contradictory relations
        or opposite-polarity content in the same category.

        Returns:
            List of potentially contradictory DimensionVectors
        """
        with self._lock:
            results = []

            # Extract topics from input for topic overlap check
            input_topics = set(self.extract_topics(content))

            for dv in self._vectors.values():
                score = 0

                # Check existing contradict relations
                for target_id, rel_type in dv.relations.items():
                    if rel_type == RelationType.CONTRADICTS.value:
                        score += 2

                # Topic overlap → potential conflict zone
                dv_topics = set(dv.topics)
                overlap = len(input_topics & dv_topics)
                if overlap >= 2:
                    score += 1

                # Negation heuristics in content
                content_lower = content.lower()
                dv_lower = dv.content.lower()
                negation_markers = ["not", "don't", "cannot", "shouldn't", "never", "no "]
                input_has_negation = any(m in content_lower for m in negation_markers)
                dv_has_negation = any(m in dv_lower for m in negation_markers)
                if input_has_negation != dv_has_negation and overlap >= 1:
                    score += 1

                if score >= 2:
                    results.append(dv)

            results.sort(key=lambda dv: dv.priority, reverse=True)
            return results

    # ── Statistics ────────────────────────────────────────────────────────

    def statistics(self) -> Dict[str, Any]:
        """Return engine statistics."""
        with self._lock:
            return {
                **self._stats,
                "total_vectors": len(self._vectors),
                "categories": {
                    cat: len(ids)
                    for cat, ids in self._category_index.items()
                },
                "scopes": {
                    sc: len(ids)
                    for sc, ids in self._scope_index.items()
                },
                "distinct_topics": len(self._topic_index),
                "distinct_sources": len(self._source_index),
                "avg_confidence": (
                    sum(dv.confidence for dv in self._vectors.values()) / max(len(self._vectors), 1)
                ),
            }

    # ── Internal Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _generate_id(content: str, source: str) -> str:
        """Generate a deterministic memory_id."""
        raw = f"{source}:{content}:{time.time()}:{uuid.uuid4().hex[:8]}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _enforce_capacity(self) -> None:
        """Prune lowest-priority vectors when over MAX_MEMORIES."""
        if len(self._vectors) < MAX_MEMORIES:
            return

        # Sort by priority ascending, remove bottom 10%
        prune_count = max(1, int(MAX_MEMORIES * 0.1))
        sorted_ids = sorted(
            self._vectors.keys(),
            key=lambda mid: self._vectors[mid].priority,
        )
        for mid in sorted_ids[:prune_count]:
            self._remove_vector(mid)
            self._stats["pruned"] += 1

        logger.info("Pruned %d low-priority memories (capacity enforcement)", prune_count)

    def _remove_vector(self, memory_id: str) -> None:
        """Remove a vector and all index references."""
        dv = self._vectors.pop(memory_id, None)
        if dv is None:
            return

        # Clean topic index
        for topic in dv.topics:
            if topic in self._topic_index:
                self._topic_index[topic].discard(memory_id)
                if not self._topic_index[topic]:
                    del self._topic_index[topic]

        # Clean category index
        if dv.category in self._category_index:
            self._category_index[dv.category].discard(memory_id)

        # Clean scope index
        if dv.scope in self._scope_index:
            self._scope_index[dv.scope].discard(memory_id)

        # Clean source index
        for agent in dv.source_agents:
            if agent in self._source_index:
                self._source_index[agent].discard(memory_id)

    def _add_to_topic_index(self, memory_id: str, topics: List[str]) -> None:
        for topic in topics:
            self._topic_index.setdefault(topic, set()).add(memory_id)

    def _add_to_category_index(self, memory_id: str, category: str) -> None:
        self._category_index.setdefault(category, set()).add(memory_id)

    def _add_to_scope_index(self, memory_id: str, scope: str) -> None:
        self._scope_index.setdefault(scope, set()).add(memory_id)

    def _add_to_source_index(self, memory_id: str, agent: str) -> None:
        self._source_index.setdefault(agent, set()).add(memory_id)

    @staticmethod
    def _infer_category(content: str) -> str:
        """Heuristic category inference from content."""
        lower = content.lower()

        policy_markers = ["must", "required", "policy", "rule", "always", "never", "should", "禁止", "必须"]
        preference_markers = ["prefer", "like", "favorite", "default", "preference", "喜欢", "偏好"]
        fact_markers = ["is a", "contains", "located", "version", "config", "settings", "定义", "配置"]

        if any(m in lower for m in policy_markers):
            return MemoryCategory.POLICY.value
        if any(m in lower for m in preference_markers):
            return MemoryCategory.PREFERENCE.value
        if any(m in lower for m in fact_markers):
            return MemoryCategory.FACT.value

        return MemoryCategory.EPISODIC.value


# ── Factory ───────────────────────────────────────────────────────────────


def create_dimension_engine() -> DimensionEngine:
    """Factory function for DimensionEngine."""
    return DimensionEngine()


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> bool:
    """Comprehensive self-test for DimensionEngine."""
    print("=" * 60)
    print("  Trinity Dimension Engine — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: Engine creation ──
    total += 1
    print("\n[Test 1] DimensionEngine creation")
    try:
        engine = create_dimension_engine()
        assert len(engine._vectors) == 0
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: index_memory ──
    total += 1
    print("\n[Test 2] index_memory")
    try:
        dv1 = engine.index_memory(
            "user prefers dark mode in all applications",
            "main",
            metadata={"category": "preference", "scope": "global"},
        )
        assert dv1.memory_id
        assert "main" in dv1.source_agents
        assert dv1.category == "preference"
        assert dv1.scope == "global"
        assert dv1.confidence == DEFAULT_CONFIDENCE
        assert len(dv1.topics) > 0
        assert 0.0 <= dv1.priority <= 1.0
        print(f"    memory_id={dv1.memory_id}, category={dv1.category}, "
              f"topics={dv1.topics}, priority={dv1.priority:.4f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: Multiple indexings with category inference ──
    total += 1
    print("\n[Test 3] Index multiple memories")
    try:
        dv_policy = engine.index_memory(
            "All agents must use HTTPS for external requests",
            "computer-agent",
        )
        dv_fact = engine.index_memory(
            "The project config is located at /etc/trinity/config.yaml",
            "file-agent",
        )
        dv_episodic = engine.index_memory(
            "file-agent processed 3 invoices this morning",
            "main",
        )
        assert dv_policy.category == "policy", f"expected policy, got {dv_policy.category}"
        assert dv_fact.category == "fact", f"expected fact, got {dv_fact.category}"
        print(f"    policy='{dv_policy.category}', fact='{dv_fact.category}', "
              f"episodic='{dv_episodic.category}'")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: query by category ──
    total += 1
    print("\n[Test 4] query by category")
    try:
        results = engine.query({"category": "preference"})
        assert len(results) >= 1
        assert all(dv.category == "preference" for dv in results)
        print(f"    found {len(results)} preference memories")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: query by source_agent ──
    total += 1
    print("\n[Test 5] query by source_agent")
    try:
        results = engine.query({"source_agent": "file-agent"})
        assert len(results) >= 1
        print(f"    found {len(results)} memories from file-agent")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: query by multiple filters ──
    total += 1
    print("\n[Test 6] query by multiple filters")
    try:
        results = engine.query({
            "confidence_min": 0.4,
            "confidence_max": 0.8,
            "time_bucket": "minute",
        })
        print(f"    confidence [0.4,0.8] + time_bucket=minute: {len(results)} results")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: boost_confidence ──
    total += 1
    print("\n[Test 7] boost_confidence")
    try:
        old_conf = dv1.confidence
        boosted = engine.boost_confidence(dv1, "browser", True)
        assert boosted.confidence == min(old_conf + CONFIDENCE_BOOST_PER_AGENT, MAX_CONFIDENCE)
        assert "browser" in boosted.source_agents
        assert boosted.source_count == 2
        print(f"    confidence: {old_conf:.2f} → {boosted.confidence:.2f}, "
              f"sources: {boosted.source_count}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: boost_confidence — not similar → no boost ──
    total += 1
    print("\n[Test 8] boost_confidence (not similar → skip)")
    try:
        dv_test = engine.index_memory("isolated memory unit for testing", "main")
        old_conf = dv_test.confidence
        boosted = engine.boost_confidence(dv_test, "search-agent", False)
        assert boosted.confidence == old_conf
        print(f"    confidence unchanged: {old_conf:.2f}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 9: add_relation ──
    total += 1
    print("\n[Test 9] add_relation")
    try:
        dv_a = engine.index_memory("Python is the primary language", "main",
                                   metadata={"category": "fact"})
        dv_b = engine.index_memory("Python 3.11+ is required", "computer-agent",
                                   metadata={"category": "fact"})
        engine.add_relation(dv_a.memory_id, dv_b.memory_id, "extends")
        assert dv_b.memory_id in engine._vectors[dv_a.memory_id].relations
        assert engine._vectors[dv_a.memory_id].relations[dv_b.memory_id] == "extends"
        print(f"    relation: {dv_a.memory_id} --extends--> {dv_b.memory_id}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 10: find_contradictions ──
    total += 1
    print("\n[Test 10] find_contradictions")
    try:
        dv_c = engine.index_memory("do not use Python 2, it is deprecated",
                                   "main", metadata={"category": "policy"})
        engine.add_relation(dv_a.memory_id, dv_c.memory_id, "contradicts")
        contradictions = engine.find_contradictions("Python is the primary language")
        assert len(contradictions) >= 1
        print(f"    found {len(contradictions)} contradictions")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 11: statistics ──
    total += 1
    print("\n[Test 11] statistics")
    try:
        stats = engine.statistics()
        assert stats["total_vectors"] >= 7
        assert "categories" in stats
        assert "scopes" in stats
        print(f"    vectors={stats['total_vectors']}, "
              f"categories={stats['categories']}, "
              f"scopes={stats['scopes']}, "
              f"topics={stats['distinct_topics']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 12: Thread safety ──
    total += 1
    print("\n[Test 12] Thread safety")
    try:
        import random
        threads = []
        errors = []

        def worker(worker_id: int) -> None:
            try:
                for i in range(10):
                    engine.index_memory(
                        f"thread-{worker_id} message number {i}",
                        f"agent-{worker_id % 3}",
                    )
            except Exception as exc:
                errors.append(str(exc))

        for tid in range(4):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Thread errors: {errors}"
        print(f"    4 threads × 10 inserts OK, total vectors={len(engine._vectors)}")
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
