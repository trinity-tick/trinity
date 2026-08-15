"""
# status: orphan (2026-08-15 audit, not in runtime path)
P15-3: Hippocampus-Cortex Dual-Pathway Bridge.

Reference: MemVerse (Shanghai AI Lab, arXiv 2512.03627) —
           CLS Theory dual-pathway cooperative architecture:
           hippocampus for fast high-fidelity episodic storage,
           neocortex for slow compressed generalized representations.

Design: Two complementary memory pathways operating at different
        speeds and abstraction levels. The hippocampus pathway stores
        rich multimodal episodic traces; the cortex pathway compresses
        repeated experiences into abstract semantic knowledge.
        A consolidation scheduler triggers sleep-like cortical
        consolidation during idle periods, and a dual-pathway router
        directs queries to the appropriate path.

Complementary to: parallel_memory_nexus.py (4-type parallel scheduling) —
                  this module handles dual-pathway differentiated consolidation.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class PathwayType(Enum):
    HIPPOCAMPUS = auto()   # fast, high-fidelity episodic
    CORTEX = auto()        # slow, compressed semantic


class Modality(Enum):
    TEXT = auto()
    IMAGE = auto()
    AUDIO = auto()
    VIDEO = auto()


class ConsolidationPhase(Enum):
    IDLE = auto()
    SCHEDULED = auto()
    ACTIVE = auto()
    COMPLETED = auto()
    PAUSED = auto()


class QueryType(Enum):
    EPISODIC = auto()      # "what happened when..."
    SEMANTIC = auto()      # "what is the concept of..."
    MIXED = auto()         # query touches both


class DistillationState(Enum):
    PENDING = auto()
    IN_PROGRESS = auto()
    COMPLETED = auto()
    VERIFIED = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EpisodicTrace:
    """A single episodic memory trace in the hippocampus pathway."""
    trace_id: str
    timestamp: float
    summary: str                               # what happened
    participants: List[str] = field(default_factory=list)  # who
    location: str = ""                         # where
    raw_data: Dict[str, Any] = field(default_factory=dict)  # multimodal raw data
    emotional_salience: float = 0.0            # 0–1, boosts retention
    replay_count: int = 0                      # number of replays/consolidations
    created_at: float = field(default_factory=time.time)


@dataclass
class SemanticSchema:
    """A compressed semantic representation in the cortex pathway."""
    schema_id: str
    label: str                                 # concept / rule / abstraction label
    description: str
    source_traces: List[str] = field(default_factory=list)  # trace_ids that contributed
    confidence: float = 0.5                    # grows with convergent evidence
    abstraction_level: int = 1                 # 1=concrete, 5=highly abstract
    embedding: np.ndarray = field(default_factory=lambda: np.zeros(128))
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


@dataclass
class ConsolidationJob:
    """A scheduled hippocampus→cortex consolidation task."""
    job_id: str
    source_trace_ids: List[str]
    target_schema_id: Optional[str] = None
    phase: ConsolidationPhase = ConsolidationPhase.SCHEDULED
    priority: float = 0.5
    scheduled_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None


@dataclass
class MultiModalEpisodicTrace:
    """An episodic trace with cross-modal bindings."""
    trace_id: str
    text_content: str = ""
    image_paths: List[str] = field(default_factory=list)
    audio_paths: List[str] = field(default_factory=list)
    video_paths: List[str] = field(default_factory=list)
    modality_bindings: Dict[str, List[str]] = field(default_factory=dict)  # modality→content_ids
    timestamp: float = field(default_factory=time.time)
    unified_embedding: np.ndarray = field(default_factory=lambda: np.zeros(256))


@dataclass
class DistilledRule:
    """A compressed rule distilled from repeated hippocampal traces."""
    rule_id: str
    description: str
    source_trace_ids: List[str]
    confidence: float = 0.5
    applicable_domains: List[str] = field(default_factory=list)
    state: DistillationState = DistillationState.PENDING


@dataclass
class DualPathwayStats:
    """Combined statistics from both pathways."""
    hippocampus_trace_count: int = 0
    cortex_schema_count: int = 0
    consolidation_jobs_completed: int = 0
    consolidation_jobs_pending: int = 0
    distilled_rules: int = 0
    cross_modal_traces: int = 0
    average_trace_salience: float = 0.0


# ---------------------------------------------------------------------------
# Core classes
# ---------------------------------------------------------------------------

class HippocampusPathway:
    """Fast pathway: high-fidelity episodic storage.

    Stores rich episodic traces with temporal ordering, participant
    information, multimodal raw data, and emotional salience markers.
    High salience traces are prioritized for consolidation.
    """

    def __init__(self, max_traces: int = 10000):
        self._lock = threading.RLock()
        self.max_traces = max_traces
        self._traces: Dict[str, EpisodicTrace] = {}
        self._timeline: List[str] = []       # ordered trace_ids

    def store(self, summary: str, participants: Optional[List[str]] = None,
              location: str = "", raw_data: Optional[Dict[str, Any]] = None,
              emotional_salience: float = 0.0) -> EpisodicTrace:
        """Store a new episodic trace."""
        with self._lock:
            if len(self._traces) >= self.max_traces:
                # Evict lowest-salience trace
                evict = min(self._traces.values(), key=lambda t: t.emotional_salience)
                del self._traces[evict.trace_id]
                self._timeline.remove(evict.trace_id)

            trace = EpisodicTrace(
                trace_id=f"ep_{uuid.uuid4().hex[:12]}",
                timestamp=time.time(),
                summary=summary,
                participants=participants or [],
                location=location,
                raw_data=raw_data or {},
                emotional_salience=min(1.0, max(0.0, emotional_salience)),
            )
            self._traces[trace.trace_id] = trace
            self._timeline.append(trace.trace_id)
            logger.debug(f"[Hippocampus] Stored episodic trace {trace.trace_id}")
            return trace

    def retrieve(self, trace_id: str) -> Optional[EpisodicTrace]:
        with self._lock:
            return self._traces.get(trace_id)

    def query_by_time(self, start: float, end: float) -> List[EpisodicTrace]:
        with self._lock:
            return [
                t for t in self._traces.values()
                if start <= t.timestamp <= end
            ]

    def get_high_salience_traces(self, threshold: float = 0.7) -> List[EpisodicTrace]:
        with self._lock:
            return sorted(
                [t for t in self._traces.values() if t.emotional_salience >= threshold],
                key=lambda t: t.emotional_salience, reverse=True,
            )

    def mark_replayed(self, trace_id: str) -> None:
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace:
                trace.replay_count += 1

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            saliences = [t.emotional_salience for t in self._traces.values()]
            return {
                "trace_count": len(self._traces),
                "capacity_used_pct": round(len(self._traces) / self.max_traces * 100, 2),
                "avg_salience": round(float(np.mean(saliences)), 4) if saliences else 0.0,
                "total_replays": sum(t.replay_count for t in self._traces.values()),
            }


class CortexPathway:
    """Slow pathway: compressed generalized representations.

    Abstracts repeated episodic experiences into semantic schemas.
    Schemas grow in confidence as convergent evidence from multiple
    traces reinforces them.
    """

    def __init__(self, max_schemas: int = 5000):
        self._lock = threading.RLock()
        self.max_schemas = max_schemas
        self._schemas: Dict[str, SemanticSchema] = {}

    def create_schema(self, label: str, description: str,
                      source_traces: Optional[List[str]] = None,
                      abstraction_level: int = 1) -> SemanticSchema:
        with self._lock:
            schema = SemanticSchema(
                schema_id=f"sc_{uuid.uuid4().hex[:12]}",
                label=label,
                description=description,
                source_traces=source_traces or [],
                abstraction_level=min(5, max(1, abstraction_level)),
            )
            self._schemas[schema.schema_id] = schema
            return schema

    def reinforce_schema(self, schema_id: str, additional_traces: List[str],
                         boost: float = 0.1) -> Optional[SemanticSchema]:
        """Reinforce an existing schema with new evidence."""
        with self._lock:
            schema = self._schemas.get(schema_id)
            if schema is None:
                return None
            for tid in additional_traces:
                if tid not in schema.source_traces:
                    schema.source_traces.append(tid)
            schema.confidence = min(1.0, schema.confidence + boost)
            schema.last_updated = time.time()
            return schema

    def query_semantic(self, keyword: str, top_k: int = 10) -> List[SemanticSchema]:
        with self._lock:
            matches = [
                s for s in self._schemas.values()
                if keyword.lower() in s.label.lower()
                or keyword.lower() in s.description.lower()
            ]
            return sorted(matches, key=lambda s: s.confidence, reverse=True)[:top_k]

    def get_schema(self, schema_id: str) -> Optional[SemanticSchema]:
        with self._lock:
            return self._schemas.get(schema_id)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            confs = [s.confidence for s in self._schemas.values()]
            levels = [s.abstraction_level for s in self._schemas.values()]
            return {
                "schema_count": len(self._schemas),
                "avg_confidence": round(float(np.mean(confs)), 4) if confs else 0.0,
                "avg_abstraction_level": round(float(np.mean(levels)), 2) if levels else 0,
                "max_confidence_schema": max(
                    self._schemas.values(), key=lambda s: s.confidence
                ).label if self._schemas else None,
            }


class ConsolidationScheduler:
    """Schedules sleep-like hippocampus→cortex consolidation during idle periods.

    Monitors system idle state, prioritizes high-salience traces for
    consolidation, and manages the consolidation job queue.
    """

    def __init__(self, hippocampus: HippocampusPathway,
                 cortex: CortexPathway):
        self._lock = threading.RLock()
        self.hippocampus = hippocampus
        self.cortex = cortex
        self._jobs: Dict[str, ConsolidationJob] = {}
        self._completed_jobs: deque = deque(maxlen=1000)
        self._idle_since: Optional[float] = None
        self.idle_threshold_seconds: float = 30.0

    def set_idle(self, idle: bool) -> None:
        with self._lock:
            if idle and self._idle_since is None:
                self._idle_since = time.time()
                logger.info("[ConsolidationScheduler] System idle — scheduling consolidation")
            elif not idle:
                self._idle_since = None

    def is_idle(self) -> bool:
        with self._lock:
            if self._idle_since is None:
                return False
            return (time.time() - self._idle_since) >= self.idle_threshold_seconds

    def schedule_consolidation(self, trace_ids: Optional[List[str]] = None,
                               ) -> Optional[ConsolidationJob]:
        """Schedule a consolidation job (if idle) or queue it."""
        with self._lock:
            if trace_ids is None:
                # Auto-select high-salience traces
                high_sal = self.hippocampus.get_high_salience_traces(0.5)
                trace_ids = [t.trace_id for t in high_sal[:10]]

            if not trace_ids:
                return None

            job = ConsolidationJob(
                job_id=f"cons_{uuid.uuid4().hex[:12]}",
                source_trace_ids=trace_ids,
                phase=ConsolidationPhase.SCHEDULED,
            )
            self._jobs[job.job_id] = job
            return job

    def run_consolidation(self, job_id: str) -> Dict[str, Any]:
        """Execute a single consolidation job.

        Takes hippocampal traces, extracts common patterns, and creates
        or reinforces cortical schemas.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"error": "job not found"}

            job.phase = ConsolidationPhase.ACTIVE
            job.started_at = time.time()

            # Collect summaries from source traces
            summaries = []
            for tid in job.source_trace_ids:
                trace = self.hippocampus.retrieve(tid)
                if trace:
                    summaries.append(trace.summary)
                    self.hippocampus.mark_replayed(tid)

            if not summaries:
                job.phase = ConsolidationPhase.COMPLETED
                job.completed_at = time.time()
                return {"consolidated": 0, "reason": "no valid traces"}

            # Find common keywords across summaries
            word_counts: Dict[str, int] = {}
            for s in summaries:
                for word in s.lower().split():
                    if len(word) > 3:
                        word_counts[word] = word_counts.get(word, 0) + 1

            # Create/reinforce schemas for frequent words
            consolidated = 0
            for word, count in word_counts.items():
                if count >= 2:
                    existing = self.cortex.query_semantic(word, top_k=1)
                    if existing:
                        self.cortex.reinforce_schema(
                            existing[0].schema_id, job.source_trace_ids,
                        )
                    else:
                        if len(self.cortex._schemas) < self.cortex.max_schemas:
                            self.cortex.create_schema(
                                label=word,
                                description=f"Pattern: {word} observed in {count} episodes",
                                source_traces=job.source_trace_ids,
                            )
                    consolidated += 1

            job.phase = ConsolidationPhase.COMPLETED
            job.completed_at = time.time()
            self._completed_jobs.append(job)
            return {"consolidated_schemas": consolidated, "traces_processed": len(summaries)}

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pending_jobs": sum(
                    1 for j in self._jobs.values()
                    if j.phase in (ConsolidationPhase.SCHEDULED, ConsolidationPhase.ACTIVE)
                ),
                "completed_jobs": len(self._completed_jobs),
                "is_idle": self.is_idle(),
                "idle_seconds": (
                    time.time() - self._idle_since if self._idle_since else 0
                ),
            }


class DualPathwayRouter:
    """Routes queries to the appropriate pathway based on query type.

    Episodic queries ("what happened", "when did") → hippocampus
    Semantic queries ("what is", "concept of") → cortex
    Mixed queries → both pathways, results merged
    """

    _EPISODIC_INDICATORS = [
        "when", "what happened", "who was", "where did", "last time",
        "previous", "remember when", "tell me about that time",
    ]
    _SEMANTIC_INDICATORS = [
        "what is", "define", "concept", "meaning of", "how does",
        "explain", "describe the concept", "general rule",
    ]

    def __init__(self, hippocampus: HippocampusPathway,
                 cortex: CortexPathway):
        self._lock = threading.RLock()
        self.hippocampus = hippocampus
        self.cortex = cortex
        self._routing_log: deque = deque(maxlen=500)

    def classify_query(self, query_text: str) -> QueryType:
        lower = query_text.lower()
        episodic = any(ind in lower for ind in self._EPISODIC_INDICATORS)
        semantic = any(ind in lower for ind in self._SEMANTIC_INDICATORS)

        if episodic and semantic:
            return QueryType.MIXED
        elif episodic:
            return QueryType.EPISODIC
        elif semantic:
            return QueryType.SEMANTIC
        return QueryType.MIXED  # default: try both

    def route(self, query_text: str, keyword: Optional[str] = None,
              time_range: Optional[Tuple[float, float]] = None,
              ) -> Dict[str, Any]:
        """Route a query and return combined results."""
        with self._lock:
            qtype = self.classify_query(query_text)
            result: Dict[str, Any] = {
                "query_type": qtype.name,
                "hippocampus_results": [],
                "cortex_results": [],
            }

            if qtype in (QueryType.EPISODIC, QueryType.MIXED):
                if time_range:
                    traces = self.hippocampus.query_by_time(*time_range)
                else:
                    traces = list(self.hippocampus._traces.values())
                result["hippocampus_results"] = [
                    {"trace_id": t.trace_id, "summary": t.summary, "salience": t.emotional_salience}
                    for t in traces[-10:]  # most recent 10
                ]

            if qtype in (QueryType.SEMANTIC, QueryType.MIXED) and keyword:
                schemas = self.cortex.query_semantic(keyword)
                result["cortex_results"] = [
                    {"schema_id": s.schema_id, "label": s.label, "confidence": s.confidence}
                    for s in schemas
                ]

            self._routing_log.append({
                "query": query_text[:80],
                "routed_to": qtype.name,
                "timestamp": time.time(),
            })
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            counts = defaultdict(int)
            for entry in self._routing_log:
                counts[entry["routed_to"]] += 1
            return {
                "total_routed": len(self._routing_log),
                "by_type": dict(counts),
            }


class CrossModalBinding:
    """Binds text, image, audio, video into unified multimodal episodic traces.

    Creates MultiModalEpisodicTrace objects with cross-modal association
    metadata, enabling unified retrieval across modalities.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._traces: Dict[str, MultiModalEpisodicTrace] = {}

    def bind(self, text_content: str = "",
             image_paths: Optional[List[str]] = None,
             audio_paths: Optional[List[str]] = None,
             video_paths: Optional[List[str]] = None,
             ) -> MultiModalEpisodicTrace:
        with self._lock:
            trace = MultiModalEpisodicTrace(
                trace_id=f"mm_{uuid.uuid4().hex[:12]}",
                text_content=text_content,
                image_paths=image_paths or [],
                audio_paths=audio_paths or [],
                video_paths=video_paths or [],
                modality_bindings={
                    "text": [text_content[:50]] if text_content else [],
                    "image": image_paths or [],
                    "audio": audio_paths or [],
                    "video": video_paths or [],
                },
            )
            # Generate unified embedding as simple concat projection
            seed = hash(text_content) % (2 ** 31)
            rng = np.random.RandomState(abs(seed))
            trace.unified_embedding = rng.randn(256).astype(np.float32)
            trace.unified_embedding /= np.linalg.norm(trace.unified_embedding) + 1e-8

            self._traces[trace.trace_id] = trace
            return trace

    def retrieve(self, trace_id: str) -> Optional[MultiModalEpisodicTrace]:
        with self._lock:
            return self._traces.get(trace_id)

    def get_modalities(self, trace_id: str) -> List[str]:
        with self._lock:
            trace = self._traces.get(trace_id)
            if trace is None:
                return []
            return [m for m, items in trace.modality_bindings.items() if items]

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            modality_counts = defaultdict(int)
            for t in self._traces.values():
                for m, items in t.modality_bindings.items():
                    if items:
                        modality_counts[m] += 1
            return {
                "total_traces": len(self._traces),
                "modality_distribution": dict(modality_counts),
            }


class MemoryDistillation:
    """Distills transferable compressed rules from repeated hippocampal traces.

    When the same pattern appears across multiple episodic traces,
    extracts a distilled rule and writes it into the cortex pathway
    as a reusable, generalizable abstraction.
    """

    def __init__(self, hippocampus: HippocampusPathway,
                 cortex: CortexPathway):
        self._lock = threading.RLock()
        self.hippocampus = hippocampus
        self.cortex = cortex
        self._rules: Dict[str, DistilledRule] = {}

    def distill(self, trace_ids: List[str],
                min_occurrence: int = 3) -> List[DistilledRule]:
        """Distill rules from a set of repeated episodic traces."""
        with self._lock:
            summaries = []
            for tid in trace_ids:
                trace = self.hippocampus.retrieve(tid)
                if trace:
                    summaries.append(trace.summary)

            if len(summaries) < min_occurrence:
                return []

            # Find recurring n-grams (simple heuristic)
            words_by_trace = [set(s.lower().split()) for s in summaries]
            if not words_by_trace:
                return []

            common_words = words_by_trace[0]
            for ws in words_by_trace[1:]:
                common_words = common_words & ws

            rules = []
            for word in common_words:
                if len(word) > 4:
                    rule = DistilledRule(
                        rule_id=f"dr_{uuid.uuid4().hex[:12]}",
                        description=f"Recurring pattern: '{word}' appears across {len(summaries)} episodes",
                        source_trace_ids=trace_ids,
                        confidence=min(1.0, 0.4 + 0.1 * len(summaries)),
                        applicable_domains=["general"],
                        state=DistillationState.COMPLETED,
                    )
                    self._rules[rule.rule_id] = rule
                    rules.append(rule)

                    # Write abstracted rule into cortex
                    self.cortex.create_schema(
                        label=f"distilled:{word}",
                        description=rule.description,
                        source_traces=trace_ids,
                        abstraction_level=3,
                    )

            return rules

    def get_rules(self) -> List[DistilledRule]:
        with self._lock:
            return list(self._rules.values())

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "distilled_rules": len(self._rules),
                "avg_confidence": round(
                    float(np.mean([r.confidence for r in self._rules.values()]))
                    if self._rules else 0.0, 4,
                ),
                "verified_rules": sum(
                    1 for r in self._rules.values()
                    if r.state == DistillationState.VERIFIED
                ),
            }
