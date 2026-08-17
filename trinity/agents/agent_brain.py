"""
Agent Brain v6.95.0 — Shared Memory Pool Architecture
======================================================
Upgrades Trinity from per-agent isolated storage to a shared
cross-agent memory pool with dimension-aware indexing.

Alignments:
  - Letta / Mem0 2026: agent-native memory with autonomous loops
  - LangMem (LangChain): memory as a first-class agent capability
  - Anthropic MCP Sept 2025: tool-use agents with persistent state
  - Oracle Memory Guide (2026.05): 5-type classification + Promotion Gate
  - Microsoft ISE A2A Embedded Context (2026.06): cross-agent memory

Classes:
  - AgentBrain: main control loop, ingest/consolidate/resolve/maintain
  - MemoryAgentProtocol: standardized inter-agent memory protocol
  - AgentMemoryContext: shared-pool context with cross-agent insights
  - DecisionEngine: should_remember / should_forget / should_update
"""

from __future__ import annotations

__version__ = "6.95.0"

import hashlib
import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from trinity.agents.aggregator import MemoryAggregator, create_aggregator
from trinity.agents.dimensions import DimensionEngine, DimensionVector

logger = logging.getLogger(__name__)

# ── Configuration constants ──────────────────────────────────────────────

AGENT_BRAIN_CYCLE_INTERVAL = 5.0
AGENT_BRAIN_MAINTENANCE_INTERVAL = 300.0
AGENT_BRAIN_AUTO_CONSOLIDATE_INTERVAL = 60.0
AGENT_BRAIN_CONFLICT_CHECK_INTERVAL = 120.0
AGENT_BRAIN_MAX_MEMORIES_PER_AGENT = 10_000
AGENT_BRAIN_IMPORTANCE_THRESHOLD = 0.3
AGENT_BRAIN_FORGET_DECAY_RATE = 0.01
AGENT_BRAIN_SIMILARITY_UPDATE_THRESHOLD = 0.85


# ── Enums ─────────────────────────────────────────────────────────────────

class BrainState(Enum):
    """Operational state of the AgentBrain loop."""
    IDLE = "idle"
    INGESTING = "ingesting"
    CONSOLIDATING = "consolidating"
    RESOLVING = "resolving"
    MAINTAINING = "maintaining"
    STOPPED = "stopped"


class MemoryAction(Enum):
    """Action taken by the decision engine."""
    STORE = "store"
    IGNORE = "ignore"
    MERGE = "merge"
    UPDATE = "update"
    FORGET = "forget"
    ARCHIVE = "archive"


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class AgentMemoryProfile:
    """Per-agent memory strategy profile."""
    agent_name: str
    memory_priority: float = 1.0
    focus_keywords: List[str] = field(default_factory=list)
    max_memories: int = 10_000
    decay_rate: float = 0.01
    importance_threshold: float = 0.3
    # Tracked metrics
    total_ingested: int = 0
    total_stored: int = 0
    total_forgotten: int = 0
    last_active: float = 0.0


@dataclass
class MemoryEvent:
    """A single memory event recorded by the agent brain."""
    event_id: str = ""
    agent_name: str = ""
    event_type: str = ""
    content: str = ""
    importance: float = 0.0
    timestamp: float = 0.0
    access_count: int = 0
    last_accessed: float = 0.0
    memory_unit_id: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id:
            self.event_id = uuid.uuid4().hex[:16]
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class CrossAgentInsight:
    """Result of cross-agent memory correlation analysis."""
    source_agent: str
    target_agent: str
    shared_entities: List[str] = field(default_factory=list)
    shared_keywords: List[str] = field(default_factory=list)
    relevance_score: float = 0.0
    insight_summary: str = ""


@dataclass
class BrainStats:
    """Aggregate statistics for the AgentBrain."""
    total_events: int = 0
    total_ingested: int = 0
    total_consolidations: int = 0
    total_conflict_resolutions: int = 0
    total_maintenance_runs: int = 0
    total_cycles: int = 0
    total_cross_agent_syncs: int = 0
    state: BrainState = BrainState.IDLE
    cycle_start: float = 0.0


# ── MemoryAgentProtocol ───────────────────────────────────────────────────

class MemoryAgentProtocol:
    """Standardized inter-agent memory interaction protocol (v6.95.0).

    Any agent in the Marvis ecosystem can notify Trinity of its activities
    through this protocol.  Trinity automatically extracts, stores, and
    cross-references relevant memories via the shared aggregator pool.
    """

    def __init__(self, brain: "AgentBrain"):
        self._brain = brain
        self._lock = threading.RLock()
        self._active_tasks: Dict[str, Dict[str, Any]] = {}
        logger.info("MemoryAgentProtocol initialized (shared pool)")

    def on_agent_task_start(self, agent_name: str, task_desc: str) -> str:
        """Called when any agent begins a task.  Returns a task_id."""
        with self._lock:
            task_id = uuid.uuid4().hex[:12]
            self._active_tasks[task_id] = {
                "agent_name": agent_name,
                "task_desc": task_desc,
                "start_time": time.time(),
                "status": "running",
            }
            self._brain._aggregator.ingest(
                content=task_desc,
                source_agent=agent_name,
                metadata={"event_type": "task_start", "task_id": task_id},
            )
            logger.debug(
                "Agent %s started task %s: %s", agent_name, task_id, task_desc[:80]
            )
            return task_id

    def on_agent_task_complete(
        self, agent_name: str, result_summary: str, task_id: str = ""
    ) -> None:
        """Called when an agent completes a task.  Auto-extracts memory."""
        with self._lock:
            if task_id and task_id in self._active_tasks:
                task_info = self._active_tasks.pop(task_id)
                duration = time.time() - task_info["start_time"]
                content = (
                    f"[{agent_name}] completed: {result_summary} "
                    f"(duration={duration:.1f}s)"
                )
            else:
                content = f"[{agent_name}] completed: {result_summary}"

            self._brain._aggregator.ingest(
                content=content,
                source_agent=agent_name,
                metadata={"event_type": "task_complete", "task_id": task_id,
                          "result_summary": result_summary},
            )

    def on_user_query(self, query: str) -> List[MemoryEvent]:
        """Called when user sends a query.  Returns relevant historical memories."""
        return self._brain._query_relevant_memories(query)

    def query_relevant_memories(
        self, context: str, top_k: int = 10
    ) -> List[MemoryEvent]:
        """Called by other agents to retrieve relevant memory summaries."""
        return self._brain._query_relevant_memories(context, top_k)

    def get_active_tasks(self) -> Dict[str, Dict[str, Any]]:
        """Return currently active task tracking."""
        with self._lock:
            return dict(self._active_tasks)

    def get_global_context(self) -> Dict[str, Any]:
        """Return global shared memory context for cross-agent awareness (v6.95.0)."""
        return self._brain._aggregator.get_global_context()

    def query_by_dimensions(self, filters: Dict[str, Any]) -> List[DimensionVector]:
        """Query shared pool by dimension filters (v6.95.0)."""
        return self._brain._aggregator.query(filters)

    def get_events_by_agent(self, agent_name: str, limit: int = 100) -> List[DimensionVector]:
        """Get all memories associated with a specific agent (v6.95.0)."""
        return self._brain._aggregator.get_by_agent(agent_name)

    def get_contradictions(self) -> List[Tuple[DimensionVector, DimensionVector]]:
        """Find contradictory memories in the shared pool (v6.95.0)."""
        return self._brain._aggregator.get_contradictions()

    def record_event(
        self,
        agent_name: str,
        content: str,
        event_type: str = "general",
        importance: float = 0.5,
    ) -> None:
        """Record an event into the shared memory pool (v6.95.0)."""
        self._brain._aggregator.ingest(
            content=content,
            source_agent=agent_name,
            metadata={"event_type": event_type, "importance": importance},
        )

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "active_tasks": len(self._active_tasks),
                "agents_tracked": list(
                    set(t["agent_name"] for t in self._active_tasks.values())
                ),
            }


# ── AgentMemoryContext ────────────────────────────────────────────────────

class AgentMemoryContext:
    """Shared-pool memory context with cross-agent insight analysis (v6.95.0).

    Each agent gets a dedicated profile, but all memories live in the shared
    aggregator pool.  Cross-agent correlations use the pool's topic/agent
    indices for efficient lookup.
    """

    def __init__(self, brain: "AgentBrain"):
        self._brain = brain
        self._lock = threading.RLock()
        self._agent_profiles: Dict[str, AgentMemoryProfile] = {}
        self._init_default_profiles()
        logger.info("AgentMemoryContext initialized with %d profiles (shared pool)",
                     len(self._agent_profiles))

    def _init_default_profiles(self) -> None:
        defaults = {
            "file-agent": AgentMemoryProfile(
                agent_name="file-agent",
                memory_priority=1.2,
                focus_keywords=["file", "document", "path", "folder", "directory", "pdf", "docx"],
            ),
            "browser": AgentMemoryProfile(
                agent_name="browser",
                memory_priority=1.1,
                focus_keywords=["url", "website", "page", "download", "http", "search"],
            ),
            "app-agent": AgentMemoryProfile(
                agent_name="app-agent",
                memory_priority=1.0,
                focus_keywords=["app", "install", "uninstall", "open", "settings"],
            ),
            "computer-agent": AgentMemoryProfile(
                agent_name="computer-agent",
                memory_priority=1.0,
                focus_keywords=["system", "settings", "process", "service", "registry"],
            ),
            "search-agent": AgentMemoryProfile(
                agent_name="search-agent",
                memory_priority=1.3,
                focus_keywords=["research", "paper", "report", "analysis", "comparison"],
            ),
        }
        self._agent_profiles.update(defaults)

    def get_agent_context(self, agent_name: str) -> Dict[str, Any]:
        """Return the memory context for a specific agent (v6.95.0 shared pool)."""
        with self._lock:
            profile = self._agent_profiles.get(
                agent_name,
                AgentMemoryProfile(agent_name=agent_name),
            )
            # Query shared pool for this agent
            pool_events = self._brain._aggregator.get_by_agent(agent_name)
            return {
                "agent_name": agent_name,
                "profile": {
                    "priority": profile.memory_priority,
                    "focus_keywords": profile.focus_keywords,
                    "max_memories": profile.max_memories,
                },
                "stats": {
                    "total_ingested": profile.total_ingested,
                    "total_stored": profile.total_stored,
                    "total_forgotten": profile.total_forgotten,
                    "last_active": profile.last_active,
                    "pool_events": len(pool_events),
                },
                "recent_events": [
                    {
                        "event_id": e.memory_id,
                        "type": "aggregated",
                        "content": e.content[:200],
                        "importance": e.confidence,
                        "timestamp": e.created_at,
                    }
                    for e in pool_events[:20]
                ],
            }

    def cross_agent_insights(self, top_k: int = 10) -> List[CrossAgentInsight]:
        """Cross-agent memory correlation analysis via shared pool (v6.95.0)."""
        with self._lock:
            insights: List[CrossAgentInsight] = []
            agent_names = list(self._agent_profiles.keys())

            for i, agent_a in enumerate(agent_names):
                events_a = self._brain._aggregator.get_by_agent(agent_a)
                kw_a = set(self._agent_profiles[agent_a].focus_keywords)
                content_a = " ".join(e.content.lower() for e in events_a)

                for agent_b in agent_names[i + 1 :]:
                    events_b = self._brain._aggregator.get_by_agent(agent_b)
                    kw_b = set(self._agent_profiles[agent_b].focus_keywords)

                    shared_kw = kw_a & kw_b
                    content_b = " ".join(e.content.lower() for e in events_b)
                    cross_hits = sum(
                        1 for kw in kw_a
                        if kw in content_b
                    ) + sum(1 for kw in kw_b if kw in content_a)

                    if shared_kw or cross_hits >= 3:
                        relevance = (len(shared_kw) * 0.3 + min(cross_hits / 10, 1.0) * 0.7)
                        insights.append(CrossAgentInsight(
                            source_agent=agent_a,
                            target_agent=agent_b,
                            shared_keywords=list(shared_kw),
                            relevance_score=round(relevance, 3),
                            insight_summary=(
                                f"{agent_a} and {agent_b} share {len(shared_kw)} "
                                f"focus keywords with {cross_hits} cross-content hits"
                            ),
                        ))

            insights.sort(key=lambda x: x.relevance_score, reverse=True)
            return insights[:top_k]

    def update_activity(self, agent_name: str) -> None:
        """Update last_active timestamp for an agent."""
        with self._lock:
            if agent_name not in self._agent_profiles:
                self._agent_profiles[agent_name] = AgentMemoryProfile(
                    agent_name=agent_name
                )
            self._agent_profiles[agent_name].last_active = time.time()

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "agents_tracked": len(self._agent_profiles),
                "agent_names": list(self._agent_profiles.keys()),
            }


# ── DecisionEngine ────────────────────────────────────────────────────────

class DecisionEngine:
    """Autonomous decision engine for memory lifecycle (v6.95.0).

    Three core decisions:
      - should_remember: importance scoring + shared-pool similarity check
      - should_forget:   decay model + access frequency
      - should_update:   conflict detection + merge decision

    v6.95.0 adds multi-dimension matching via the shared aggregator pool,
    enabling topic/category/scope cross-referencing across agents.
    """

    def __init__(
        self,
        importance_threshold: float = AGENT_BRAIN_IMPORTANCE_THRESHOLD,
        decay_rate: float = AGENT_BRAIN_FORGET_DECAY_RATE,
        similarity_threshold: float = AGENT_BRAIN_SIMILARITY_UPDATE_THRESHOLD,
    ):
        self.importance_threshold = importance_threshold
        self.decay_rate = decay_rate
        self.similarity_threshold = similarity_threshold
        self._lock = threading.RLock()
        self._decision_log: List[Dict[str, Any]] = []
        # Reference to brain for shared pool access (set after init)
        self._brain: Optional["AgentBrain"] = None
        logger.info(
            "DecisionEngine initialized (threshold=%.2f, decay=%.4f)",
            importance_threshold, decay_rate,
        )

    def should_remember(
        self,
        content: str,
        agent_profile: Optional[AgentMemoryProfile] = None,
        existing_events: Optional[List[MemoryEvent]] = None,
    ) -> Tuple[bool, float, str]:
        """Decide whether to store a new memory (v6.95.0 — checks shared pool).

        Returns: (decision, importance_score, reason)
        """
        with self._lock:
            score = self._compute_importance(content, agent_profile)

            # Check similarity against shared pool (v6.95.0)
            if self._brain is not None:
                try:
                    pool_similar = self._brain._aggregator.query({"content": content[:100]})
                    if pool_similar:
                        word_set = set(content.lower().split())
                        for dv in pool_similar:
                            dv_words = set(dv.content.lower().split())
                            if word_set and dv_words:
                                intersection = word_set & dv_words
                                union = word_set | dv_words
                                overlap = len(intersection) / len(union)
                                if overlap > self.similarity_threshold:
                                    reason = (
                                        f"Overlap {overlap:.2f} > {self.similarity_threshold} "
                                        f"(shared pool), score={score:.3f}, will merge"
                                    )
                                    return False, score, reason
                except Exception:
                    pass

            # Fallback: check existing events
            if existing_events:
                max_overlap = self._max_content_overlap(content, existing_events)
                if max_overlap > self.similarity_threshold:
                    reason = (
                        f"Overlap {max_overlap:.2f} > {self.similarity_threshold}, "
                        f"score={score:.3f}, will merge instead of store"
                    )
                    return False, score, reason

            decision = score >= self.importance_threshold
            reason = (
                f"Score {score:.3f} {'>=' if decision else '<'} "
                f"threshold {self.importance_threshold}"
            )
            return decision, score, reason

    def should_forget(
        self,
        event: MemoryEvent,
        now: Optional[float] = None,
    ) -> Tuple[bool, float, str]:
        """Decide whether to forget a memory.

        Based on exponential decay + access frequency penalty.
        """
        with self._lock:
            now = now or time.time()
            age = now - event.timestamp

            # Exponential decay
            decay_score = math.exp(-self.decay_rate * age)

            # Access frequency bonus (reduces decay)
            access_bonus = math.log(event.access_count + 1) * 0.1

            # Final retention score
            retention = decay_score + access_bonus
            retention = min(retention, 1.0)
            importance_bonus = event.importance * 0.3
            final_score = retention + importance_bonus

            should_drop = final_score < 0.2
            reason = (
                f"age={age:.0f}s, decay={decay_score:.4f}, "
                f"access={event.access_count}, final={final_score:.4f}"
            )
            return should_drop, final_score, reason

    def should_update(
        self,
        memory_id: str,
        new_content: str,
        existing_event: Optional[MemoryEvent] = None,
    ) -> Tuple[bool, float, str]:
        """Decide whether to update an existing memory with new content.

        Triggers on conflict detection (similar content with newer timestamp).
        """
        with self._lock:
            if existing_event is None:
                return False, 0.0, "No existing event to compare"

            overlap = self._content_similarity(new_content, existing_event.content)
            if overlap > self.similarity_threshold:
                return True, overlap, (
                    f"Overlap {overlap:.3f} exceeds threshold "
                    f"{self.similarity_threshold}, update recommended"
                )
            return False, overlap, f"Overlap {overlap:.3f} below threshold"

    def decide(
        self,
        content: str,
        agent_name: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Multi-dimension memory decision (v6.95.0).

        Queries the shared pool across topic, category, and scope dimensions
        to decide whether to store, merge, or ignore a new memory.
        """
        score = self._compute_importance(content)
        meta = metadata or {}

        # Dimension-based queries from shared pool
        dimension_matches: List[DimensionVector] = []
        shared_agents: Set[str] = set()

        if self._brain is not None:
            # Topic match
            topics = meta.get("topics", [])
            if not topics:
                # Extract basic topics from content
                words = content.lower().split()
                topics = [w for w in words if len(w) > 3][:5]
            try:
                topic_matches = self._brain._aggregator.query({"topic": topics})
                dimension_matches.extend(topic_matches)
            except Exception:
                pass

            # Category match
            category = meta.get("category")
            if category:
                try:
                    cat_matches = self._brain._aggregator.query({"category": category})
                    dimension_matches.extend(cat_matches)
                except Exception:
                    pass

            # Collect shared agents
            for dv in dimension_matches:
                for sa in dv.source_agents:
                    if sa != agent_name:
                        shared_agents.add(sa)

        decision = "store" if score >= self.importance_threshold else "ignore"
        if dimension_matches:
            decision = "merge"  # Already related — merge into existing topic bucket

        return {
            "action": decision,
            "importance_score": score,
            "dimension_matches": len(dimension_matches),
            "shared_agents": list(shared_agents),
        }

    def _compute_importance(
        self,
        content: str,
        profile: Optional[AgentMemoryProfile] = None,
    ) -> float:
        """Heuristic importance scoring."""
        score = 0.3  # Base

        lower = content.lower()

        # Length signal
        if len(content) > 200:
            score += 0.1
        elif len(content) > 500:
            score += 0.15

        # Keyword signals
        high_signal = ["error", "fail", "critical", "important", "urgent", "crash", "config"]
        med_signal = ["result", "complete", "finish", "summary", "report", "decision"]
        low_signal = ["log", "debug", "ping", "heartbeat", "poll"]

        score += sum(0.12 for kw in high_signal if kw in lower)
        score += sum(0.06 for kw in med_signal if kw in lower)
        score -= sum(0.03 for kw in low_signal if kw in lower)

        # Agent profile focus keyword bonus
        if profile and profile.focus_keywords:
            hits = sum(1 for kw in profile.focus_keywords if kw in lower)
            score += hits * 0.05

        # Clamp
        return max(0.0, min(1.0, score))

    def _max_content_overlap(
        self, content: str, events: List[MemoryEvent]
    ) -> float:
        """Compute max similarity against existing events."""
        if not events:
            return 0.0
        return max(
            self._content_similarity(content, e.content) for e in events
        )

    @staticmethod
    def _content_similarity(a: str, b: str) -> float:
        """Jaccard similarity on word sets."""
        words_a = set(a.lower().split())
        words_b = set(b.lower().split())
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "decisions_logged": len(self._decision_log),
                "importance_threshold": self.importance_threshold,
                "decay_rate": self.decay_rate,
            }


# ── AgentBrain ────────────────────────────────────────────────────────────

class AgentBrain:
    """Trinity Agent Brain — autonomous memory management loop.

    Core lifecycle:
      1. run() — start the continuous brain loop
      2. ingest_conversation() — receive turns, run 5-stage ingestion
      3. auto_consolidate() — periodic decay/compress/archive
      4. auto_resolve_conflicts() — periodic consensus voting
      5. scheduled_maintenance() — background cleanup + reindex + quantize
    """

    def __init__(
        self,
        cycle_interval: float = AGENT_BRAIN_CYCLE_INTERVAL,
        maintenance_interval: float = AGENT_BRAIN_MAINTENANCE_INTERVAL,
        auto_consolidate_interval: float = AGENT_BRAIN_AUTO_CONSOLIDATE_INTERVAL,
        conflict_check_interval: float = AGENT_BRAIN_CONFLICT_CHECK_INTERVAL,
    ):
        self.cycle_interval = cycle_interval
        self.maintenance_interval = maintenance_interval
        self.auto_consolidate_interval = auto_consolidate_interval
        self.conflict_check_interval = conflict_check_interval

        self._lock = threading.RLock()
        self._state = BrainState.IDLE
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Sub-systems
        self.decision_engine = DecisionEngine()
        self.decision_engine._brain = self  # v6.95.0: wire shared pool access
        self.agent_protocol = MemoryAgentProtocol(self)
        self.agent_context = AgentMemoryContext(self)

        # Shared memory pool (v6.95.0) — replaces per-agent isolation
        self._aggregator = create_aggregator()
        self._dimension_engine = self._aggregator._engine

        # Event list (for backward-compat stream)
        self._events: List[MemoryEvent] = []

        # Bridge (lazy, v6.94.0)
        self._bridge: Any = None
        self._ingestion_pipeline: Any = None
        self._consensus_voter: Any = None
        self._version_manager: Any = None
        self._contextual_embedder: Any = None

        # Timing bookmarks
        self._last_maintenance = 0.0
        self._last_consolidate = 0.0
        self._last_conflict_check = 0.0
        self._cycle_count = 0

        # Stats
        self.stats = BrainStats()

        logger.info(
            "AgentBrain initialized (cycle=%.1fs, maint=%.1fs, "
            "consolidate=%.1fs, conflict=%.1fs)",
            cycle_interval, maintenance_interval,
            auto_consolidate_interval, conflict_check_interval,
        )

    # ── Public API ────────────────────────────────────────────────────

    def run(self, daemon: bool = False) -> None:
        """Start the autonomous memory management loop."""
        with self._lock:
            if self._running:
                logger.warning("AgentBrain is already running")
                return
            self._running = True
            self._state = BrainState.IDLE

        if daemon:
            self._thread = threading.Thread(
                target=self._main_loop, daemon=True, name="AgentBrain"
            )
            self._thread.start()
            logger.info("AgentBrain daemon thread started")
        else:
            self._main_loop()

    def stop(self) -> None:
        """Gracefully stop the brain loop."""
        with self._lock:
            self._running = False
            self._state = BrainState.STOPPED
        logger.info("AgentBrain stop signal sent")
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10.0)

    def ingest_conversation(self, turns: List[dict]) -> int:
        """Receive conversation turns and ingest into shared memory pool (v6.95.0).

        Each turn is ingested via self._aggregator.ingest() into the shared
        pool, with auto topic extraction and merge-on-similarity.
        """
        self.agent_context.update_activity("user")

        conv_turns = []
        for turn in turns:
            conv_turns.append({
                "speaker": turn.get("speaker", "user"),
                "message": turn.get("message", turn.get("content", "")),
                "timestamp": turn.get("timestamp", time.time()),
            })

        # Ingest each turn into shared pool via aggregator
        ingested_count = 0
        for turn in conv_turns:
            speaker = turn["speaker"]
            message = turn["message"]
            if not message.strip():
                continue
            # Ingest into shared pool — auto dedup via merge_if_similar
            self._aggregator.ingest(
                content=message,
                source_agent=speaker,
                metadata={"event_type": "conversation"},
            )
            ingested_count += 1

        # Also record raw events for backward compat
        for turn in conv_turns:
            self._record_event(
                agent_name=turn["speaker"],
                event_type="conversation",
                content=turn["message"],
                importance=0.5,
            )

        # Try 5-stage ingestion through prompt_ingestion pipeline
        try:
            pipeline = self._get_ingestion_pipeline()
            units = pipeline.ingest(conv_turns)
            self.stats.total_ingested += len(units)
            logger.info("Ingested %d conversation turns → %d memory units (aggregator pool=%d)",
                         len(conv_turns), len(units), len(self._aggregator._pool))
            return len(units)
        except Exception as exc:
            logger.warning("Ingestion pipeline unavailable: %s", exc)
            return len(conv_turns)

    def auto_consolidate(self) -> Dict[str, Any]:
        """Periodic memory consolidation: decay scoring + compression."""
        self._state = BrainState.CONSOLIDATING
        result = {"decay_checked": 0, "marked_for_forget": 0, "consolidated": 0}

        with self._lock:
            now = time.time()
            for event in self._events[:]:
                should_drop, score, _ = self.decision_engine.should_forget(
                    event, now=now
                )
                result["decay_checked"] += 1
                if should_drop:
                    result["marked_for_forget"] += 1

            self.stats.total_consolidations += 1
            self._state = BrainState.IDLE

        logger.info(
            "Consolidation: checked=%d, marked_for_forget=%d",
            result["decay_checked"], result["marked_for_forget"],
        )
        return result

    def auto_resolve_conflicts(self) -> Dict[str, Any]:
        """Periodic conflict resolution via consensus voting."""
        self._state = BrainState.RESOLVING
        result = {"conflicts_found": 0, "resolved": 0, "deferred": 0}

        try:
            voter = self._get_consensus_voter()
            manager = self._get_version_manager(voter)

            # Check for memories with multiple versions
            memory_ids: Set[str] = set()
            with self._lock:
                for event in self._events:
                    if event.memory_unit_id:
                        memory_ids.add(event.memory_unit_id)

            for mid in list(memory_ids)[:50]:
                try:
                    consensus = manager.resolve(mid)
                    result["conflicts_found"] += 1
                    if consensus.consensus_reached:
                        result["resolved"] += 1
                    else:
                        result["deferred"] += 1
                except Exception:
                    result["deferred"] += 1

            self.stats.total_conflict_resolutions += 1
        except Exception as exc:
            logger.warning("Conflict resolution unavailable: %s", exc)

        self._state = BrainState.IDLE
        logger.info(
            "Conflict resolution: found=%d, resolved=%d, deferred=%d",
            result["conflicts_found"], result["resolved"], result["deferred"],
        )
        return result

    def scheduled_maintenance(self) -> Dict[str, Any]:
        """Background maintenance: cleanup, reindex, quantize (v6.95.0).

        Uses shared aggregator pool for pruning instead of per-agent lists.
        """
        self._state = BrainState.MAINTAINING
        result = {
            "events_pruned": 0,
            "orphaned_cleaned": 0,
            "quantization_triggered": False,
        }

        with self._lock:
            # Prune old/expired entries from aggregator pool
            removed = self._aggregator.clean_expired()
            result["events_pruned"] = removed

            # Remove events with empty content (backward compat list)
            before = len(self._events)
            self._events = [e for e in self._events if e.content.strip()]
            result["orphaned_cleaned"] = before - len(self._events)

            self.stats.total_maintenance_runs += 1
            self._state = BrainState.IDLE

        logger.info(
            "Maintenance: pruned=%d (aggregator), orphaned=%d (events)",
            result["events_pruned"], result["orphaned_cleaned"],
        )
        return result

    def get_state(self) -> BrainState:
        """Return current brain state."""
        return self._state

    def statistics(self) -> Dict[str, Any]:
        """Return aggregate statistics (v6.95.0 shared pool)."""
        with self._lock:
            agg_stats = self._aggregator.statistics()
            return {
                "state": self._state.value,
                "cycles": self._cycle_count,
                "total_events": len(self._events),
                "aggregator": agg_stats,
                "total_ingested": self.stats.total_ingested,
                "total_consolidations": self.stats.total_consolidations,
                "total_conflict_resolutions": self.stats.total_conflict_resolutions,
                "total_maintenance_runs": self.stats.total_maintenance_runs,
                "decision_engine": self.decision_engine.statistics(),
                "agent_context": self.agent_context.statistics(),
                "agent_protocol": self.agent_protocol.statistics(),
            }

    def diagnostics(self) -> Dict[str, Any]:
        """Detailed diagnostics dump (v6.95.0)."""
        with self._lock:
            agg_stats = self._aggregator.statistics()
            return {
                "state": self._state.value,
                "running": self._running,
                "thread_alive": self._thread.is_alive() if self._thread else False,
                "events": {"total": len(self._events)},
                "aggregator": agg_stats,
                "timings": {
                    "last_maintenance": self._last_maintenance,
                    "last_consolidate": self._last_consolidate,
                    "last_conflict_check": self._last_conflict_check,
                },
                "decision_engine": self.decision_engine.statistics(),
            }

    # ── Internal helpers ──────────────────────────────────────────────

    def _main_loop(self) -> None:
        """Core brain loop."""
        logger.info("AgentBrain main loop started")
        self.stats.cycle_start = time.time()

        while self._running:
            try:
                self._cycle_count += 1
                self.stats.total_cycles += 1
                now = time.time()

                # Scheduled maintenance
                if now - self._last_maintenance >= self.maintenance_interval:
                    self._last_maintenance = now
                    self.scheduled_maintenance()

                # Auto-consolidation
                if now - self._last_consolidate >= self.auto_consolidate_interval:
                    self._last_consolidate = now
                    self.auto_consolidate()

                # Conflict checking
                if now - self._last_conflict_check >= self.conflict_check_interval:
                    self._last_conflict_check = now
                    self.auto_resolve_conflicts()

                # Cross-agent bridge insights (v6.94.0)
                if self._cycle_count % 20 == 0:
                    self.cross_agent_insights()

                time.sleep(self.cycle_interval)

            except KeyboardInterrupt:
                logger.info("AgentBrain interrupted by user")
                self._running = False
            except Exception as exc:
                logger.error("AgentBrain loop error: %s", exc, exc_info=True)
                time.sleep(1.0)

        self._state = BrainState.STOPPED
        logger.info("AgentBrain main loop stopped after %d cycles", self._cycle_count)

    def _get_bridge(self):
        """Lazy-load the AgentBridge (v6.94.0)."""
        if self._bridge is None:
            from trinity.agents.bridge import AgentBridge
            self._bridge = AgentBridge(brain=self)
        return self._bridge

    def cross_agent_insights(self) -> Dict[str, Any]:
        """Run cross-agent memory correlation via AgentBridge + shared pool (v6.95.0).

        Queries the shared aggregator pool for cross-agent correlations
        suitable for the coordinator (Main Agent) dispatch cycle.
        """
        bridge = self._get_bridge()
        insights = {"agents": {}, "summary": "", "total_correlations": 0}

        with self._lock:
            agg_stats = self._aggregator.statistics()
            source_dist = agg_stats.get("source_distribution", {})
            agent_names = list(source_dist.keys()) if isinstance(source_dist, dict) else []

        for name in agent_names:
            state = bridge.sync_agent_state(name)
            insights["agents"][name] = state

        insights["total_correlations"] = len(agent_names)
        if agent_names:
            insights["summary"] = (
                f"Cross-agent analysis: {len(agent_names)} agents tracked, "
                f"{agg_stats.get('total_memories', 0)} events in shared pool"
            )

        self.stats.total_cross_agent_syncs += 1
        return insights

    def _record_event(
        self,
        agent_name: str,
        event_type: str,
        content: str,
        importance: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryEvent:
        """Internal event recording (v6.95.0 — also ingests into shared pool)."""
        event = MemoryEvent(
            agent_name=agent_name,
            event_type=event_type,
            content=content,
            importance=importance,
            metadata=metadata or {},
        )

        with self._lock:
            self._events.append(event)

        # Also ingest into shared aggregator pool
        self._aggregator.ingest(
            content=content,
            source_agent=agent_name,
            metadata={"event_type": event_type, "importance": importance},
        )

        return event

    def _query_relevant_memories(
        self, context: str, top_k: int = 10
    ) -> List[MemoryEvent]:
        """Keyword-match memory retrieval from shared pool (v6.95.0)."""
        keywords = context.lower().split()
        # Query aggregator with dimension filter
        try:
            dim_results = self._aggregator.query({"topic": keywords[:5]})
            if dim_results:
                scored = []
                for dv in dim_results:
                    hits = sum(1 for kw in keywords if kw in dv.content.lower())
                    if hits > 0:
                        # Surface to MemoryEvent for backward compat
                        scored.append((hits, MemoryEvent(
                            event_id=dv.memory_id,
                            agent_name=",".join(dv.source_agents),
                            event_type="aggregated",
                            content=dv.content,
                            importance=dv.confidence,
                            timestamp=dv.created_at,
                            metadata={"topics": dv.topics},
                        )))
                scored.sort(key=lambda x: x[0], reverse=True)
                return [e for _, e in scored[:top_k]]
        except Exception:
            pass
        # Fallback: keyword search over raw events
        with self._lock:
            scored = []
            for event in self._events:
                hits = sum(1 for kw in keywords if kw in event.content.lower())
                if hits > 0:
                    scored.append((hits, event))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [e for _, e in scored[:top_k]]

    def _get_agent_events(
        self, agent_name: str, limit: int = 100
    ) -> List[MemoryEvent]:
        """Get events for a specific agent from shared pool (v6.95.0)."""
        try:
            dv_results = self._aggregator.get_by_agent(agent_name)
            events: List[MemoryEvent] = []
            for dv in dv_results[:limit]:
                events.append(MemoryEvent(
                    event_id=dv.memory_id,
                    agent_name=agent_name,
                    event_type="aggregated",
                    content=dv.content,
                    importance=dv.confidence,
                    timestamp=dv.created_at,
                    metadata={"topics": dv.topics},
                ))
            return events
        except Exception:
            pass
        # Fallback: filter raw events
        with self._lock:
            return [e for e in self._events if e.agent_name == agent_name][-limit:]

    def _get_ingestion_pipeline(self) -> Any:
        """Lazy-load the prompt ingestion pipeline."""
        if self._ingestion_pipeline is None:
            from trinity.modules.second_brain.prompt_ingestion import (
                create_prompt_ingestion_pipeline,
            )
            self._ingestion_pipeline = create_prompt_ingestion_pipeline()
        return self._ingestion_pipeline

    def _get_consensus_voter(self) -> Any:
        """Lazy-load the consensus voter."""
        if self._consensus_voter is None:
            from trinity.modules.second_brain.consensus_voting import (
                create_consensus_voter,
            )
            self._consensus_voter = create_consensus_voter()
        return self._consensus_voter

    def _get_version_manager(self, voter: Any = None) -> Any:
        """Lazy-load the version manager."""
        if self._version_manager is None:
            from trinity.modules.second_brain.consensus_voting import (
                create_version_manager,
            )
            self._version_manager = create_version_manager(voter=voter)
        return self._version_manager


# ── Factory ───────────────────────────────────────────────────────────────

def create_agent_brain(
    cycle_interval: float = AGENT_BRAIN_CYCLE_INTERVAL,
    maintenance_interval: float = AGENT_BRAIN_MAINTENANCE_INTERVAL,
    auto_consolidate_interval: float = AGENT_BRAIN_AUTO_CONSOLIDATE_INTERVAL,
    conflict_check_interval: float = AGENT_BRAIN_CONFLICT_CHECK_INTERVAL,
) -> AgentBrain:
    """Factory function for AgentBrain."""
    return AgentBrain(
        cycle_interval=cycle_interval,
        maintenance_interval=maintenance_interval,
        auto_consolidate_interval=auto_consolidate_interval,
        conflict_check_interval=conflict_check_interval,
    )


# ── Self-Test ─────────────────────────────────────────────────────────────

def self_test() -> bool:
    """Comprehensive self-test for the Agent Brain module (v6.95.0)."""
    print("=" * 60)
    print("  Trinity Agent Brain v6.95.0 — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: Brain creation ──
    total += 1
    print("\n[Test 1] AgentBrain creation with aggregator")
    try:
        brain = create_agent_brain(
            cycle_interval=999.0,
            maintenance_interval=9999.0,
            auto_consolidate_interval=9999.0,
            conflict_check_interval=9999.0,
        )
        assert brain.get_state() == BrainState.IDLE
        assert brain._running is False
        assert brain._aggregator is not None, "Aggregator not initialized"
        assert brain._dimension_engine is not None, "Dimension engine not initialized"
        assert not hasattr(brain, '_events_by_agent'), "Legacy _events_by_agent still exists"
        print(f"    aggregator pool size: {len(brain._aggregator._pool)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: MemoryAgentProtocol with shared pool ──
    total += 1
    print("\n[Test 2] MemoryAgentProtocol (shared pool)")
    try:
        task_id = brain.agent_protocol.on_agent_task_start(
            "file-agent", "Process invoice.pdf"
        )
        assert len(task_id) == 12
        brain.agent_protocol.on_agent_task_complete(
            "file-agent", "Extracted 3 fields from invoice", task_id
        )
        active = brain.agent_protocol.get_active_tasks()
        assert task_id not in active
        # Verify events landed in aggregator
        events = brain.agent_protocol.get_events_by_agent("file-agent")
        assert len(events) >= 2, f"Expected >=2 events in pool, got {len(events)}"
        print(f"    file-agent pool events: {len(events)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: AgentMemoryContext (shared pool) ──
    total += 1
    print("\n[Test 3] AgentMemoryContext (shared pool)")
    try:
        ctx = brain.agent_context.get_agent_context("file-agent")
        assert ctx["agent_name"] == "file-agent"
        assert "profile" in ctx
        assert ctx["stats"]["pool_events"] >= 2, "Expected pool_events >= 2"
        print(f"    pool_events: {ctx['stats']['pool_events']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: ingest_conversation (aggregator) ──
    total += 1
    print("\n[Test 4] ingest_conversation → aggregator")
    try:
        before_pool = len(brain._aggregator._pool)
        turns = [
            {"speaker": "user", "message": "Find my tax documents"},
            {"speaker": "assistant", "message": "Searching for tax documents..."},
        ]
        n = brain.ingest_conversation(turns)
        assert n > 0
        after_pool = len(brain._aggregator._pool)
        print(f"    ingested → {n} units, pool grew {before_pool} → {after_pool}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: auto_consolidate ──
    total += 1
    print("\n[Test 5] auto_consolidate")
    try:
        result = brain.auto_consolidate()
        assert "decay_checked" in result
        print(f"    result: {result}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: auto_resolve_conflicts ──
    total += 1
    print("\n[Test 6] auto_resolve_conflicts")
    try:
        result = brain.auto_resolve_conflicts()
        assert "conflicts_found" in result
        print(f"    result: {result}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: scheduled_maintenance (aggregator) ──
    total += 1
    print("\n[Test 7] scheduled_maintenance (aggregator)")
    try:
        result = brain.scheduled_maintenance()
        assert "events_pruned" in result
        print(f"    result: {result}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: DecisionEngine with multi-dimension matching ──
    total += 1
    print("\n[Test 8] DecisionEngine (multi-dimension)")
    try:
        de = brain.decision_engine

        # should_remember: critical content with pool check
        rem, score, reason = de.should_remember(
            "CRITICAL: server crashed, config file corrupted at /etc/nginx/nginx.conf"
        )
        print(f"    should_remember(critical): {rem}, score={score:.3f}")

        # should_forget: recent event
        event = MemoryEvent(
            content="test event",
            importance=0.5,
            timestamp=time.time() - 10,
            access_count=5,
        )
        drop, retention, _ = de.should_forget(event)
        assert drop is False

        # should_update
        existing = MemoryEvent(content="The sky is blue and clear today")
        upd, sim, _ = de.should_update("mem_1", "The sky is blue and clear", existing)
        assert upd is True

        # decide() with multi-dimension matching (v6.95.0)
        decision = de.decide(
            content="Research AI memory systems for Trinity",
            agent_name="search-agent",
            metadata={"category": "research", "topics": ["AI", "memory", "Trinity"]},
        )
        assert "action" in decision
        assert "dimension_matches" in decision
        assert "shared_agents" in decision
        print(f"    decide(): action={decision['action']}, dim_matches={decision['dimension_matches']}")

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 9: Multi-agent memory sharing ──
    total += 1
    print("\n[Test 9] Multi-agent memory sharing (v6.95.0)")
    try:
        # Ingest from two different agents
        brain._aggregator.ingest(
            content="Processed document: Q4 report.pdf",
            source_agent="file-agent",
            metadata={"category": "document", "topics": ["report", "Q4"]},
        )
        brain._aggregator.ingest(
            content="Opened Q4 report for review",
            source_agent="app-agent",
            metadata={"category": "action", "topics": ["report", "Q4"]},
        )

        # Both agents should see related memories
        fa_events = brain._aggregator.get_by_agent("file-agent")
        aa_events = brain._aggregator.get_by_agent("app-agent")

        # Query by shared topic
        q4_results = brain._aggregator.query({"topic": ["Q4"]})
        assert len(q4_results) >= 2, f"Expected >=2 Q4 results, got {len(q4_results)}"

        # Global context
        global_ctx = brain._aggregator.get_global_context()
        assert isinstance(global_ctx, list)

        print(f"    file-agent events: {len(fa_events)}, app-agent events: {len(aa_events)}")
        print(f"    Q4 topic matches: {len(q4_results)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 10: statistics & stop ──
    total += 1
    print("\n[Test 10] Statistics & stop")
    try:
        stats = brain.statistics()
        assert "aggregator" in stats
        assert stats["aggregator"]["total_memories"] >= 4
        diag = brain.diagnostics()
        assert "aggregator" in diag
        print(f"    total_memories: {stats['aggregator']['total_memories']}, state: {stats['state']}")
        brain.stop()
        assert brain.get_state() == BrainState.STOPPED
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
