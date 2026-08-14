"""
AgentBridge — Main↔Trinity Standardized Interface (v6.96.0)
=============================================================
Implements the Embedded Context Pattern (Microsoft ISE A2A 2026.06):
coordinator retrieves memories → embeds in message payload → agents stay stateless.
Also integrates Oracle 5-type memory classification (Policy/Preference/Fact/Episodic/Trace)
and the Promotion Gate pattern (observation → gate → persistence).

v6.95.0 upgrades from per-agent context building to a shared memory pool
architecture — all agent states flow through the aggregator.

v6.96.0 adds Active Collection integration: EventDrivenCollector hooks are
triggered at key lifecycle points (prepare_context, after_task, errors),
enabling automatic, passive-to-active memory capture without requiring
manual write calls.

Alignments:
  - Microsoft ISE A2A Embedded Context Pattern (2026.06)
  - Oracle Memory System Guide: 5-type classification + Promotion Gate (2026.05)
  - Innoflexion Enterprise Multi-Agent Orchestration (2026): MCP+A2A dual protocol
  - agentmemory 12-hooks event-driven capture (2026.06)

Classes:
  - AgentBridge: Main Agent ↔ Trinity standardized bridging layer
  - PromotionGate: observation → decision → persistence pipeline
"""

from __future__ import annotations

__version__ = "6.96.0"

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING

logger = logging.getLogger(__name__)

# ── Configuration constants ──────────────────────────────────────────────

BRIDGE_DEFAULT_CONTEXT_SIZE = 2048
BRIDGE_DEFAULT_TOP_K = 10
BRIDGE_PROMOTION_THRESHOLD = 0.35
BRIDGE_MAX_MEMORIES_PER_CONTEXT = 5


# ── Enums ─────────────────────────────────────────────────────────────────

class MemoryCategory(Enum):
    """Oracle 5-type memory classification."""
    POLICY = "policy"       # Rules, constraints, system-wide behavior
    PREFERENCE = "preference"  # User/agent stylistic preferences
    FACT = "fact"           # Ground-truth factual knowledge
    EPISODIC = "episodic"   # Time-indexed interaction records
    TRACE = "trace"         # Raw observation logs (short-lived)


class PromotionDecision(Enum):
    """Decision from the Promotion Gate."""
    PERSIST = "persist"
    DISCARD = "discard"
    DEFER = "defer"


# ── Data Classes ──────────────────────────────────────────────────────────

@dataclass
class BridgeContext:
    """Assembled context block for embedding in dispatch_task payload."""
    agent_name: str
    relevant_memories: List[Dict[str, Any]] = field(default_factory=list)
    policy_hints: List[str] = field(default_factory=list)
    preference_hints: List[str] = field(default_factory=list)
    fact_snippets: List[str] = field(default_factory=list)
    context_id: str = ""
    assembled_text: str = ""
    total_tokens_estimate: int = 0
    dimension_tags: Dict[str, Any] = field(default_factory=dict)  # v6.95.0


@dataclass
class PromotionGateResult:
    """Result of running an observation through the promotion gate."""
    decision: PromotionDecision = PromotionDecision.DEFER
    importance_score: float = 0.0
    memory_category: MemoryCategory = MemoryCategory.EPISODIC
    reason: str = ""
    dimension_vector: Optional[Any] = None  # v6.95.0: DimensionVector from shared pool


# ── PromotionGate ─────────────────────────────────────────────────────────

class PromotionGate:
    """Oracle-style promotion gate: observation → gate → persistence.

    Evaluates each observation/task-result against configurable thresholds
    to determine whether it should be persisted to Trinity memory.

    v6.95.0: When an aggregator is attached, evaluation results are
    automatically ingested into the shared memory pool.
    """

    def __init__(
        self,
        promotion_threshold: float = BRIDGE_PROMOTION_THRESHOLD,
        aggregator: Any = None,
    ):
        self.promotion_threshold = promotion_threshold
        self._lock = threading.RLock()
        self._promotion_log: List[Dict[str, Any]] = []
        self._aggregator = aggregator  # v6.95.0: shared pool
        logger.info("PromotionGate initialized (threshold=%.2f, aggregator=%s)",
                     promotion_threshold, "attached" if aggregator else "none")

    def evaluate(
        self,
        task_result: str,
        agent_name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> PromotionGateResult:
        """Evaluate whether a task result should be promoted to memory."""
        with self._lock:
            score = self._compute_importance(task_result, agent_name)

            if score >= self.promotion_threshold + 0.15:
                decision = PromotionDecision.PERSIST
                reason = f"High importance {score:.3f} >= {self.promotion_threshold + 0.15}"
            elif score >= self.promotion_threshold:
                decision = PromotionDecision.DEFER
                reason = f"Borderline {score:.3f}, defer to DecisionEngine"
            else:
                decision = PromotionDecision.DISCARD
                reason = f"Low importance {score:.3f} < {self.promotion_threshold}"

            category = self._classify_memory(task_result)

            result = PromotionGateResult(
                decision=decision,
                importance_score=score,
                memory_category=category,
                reason=reason,
            )

            # v6.95.0: ingest into shared memory pool
            if self._aggregator is not None and decision == PromotionDecision.PERSIST:
                try:
                    dv = self._aggregator.ingest(
                        content=f"[{agent_name}] {task_result}",
                        source_agent=agent_name,
                        metadata={
                            **(metadata or {}),
                            "category": category.value,
                            "importance": score,
                            "source": "promotion_gate",
                        },
                    )
                    result.dimension_vector = dv
                except Exception as e:
                    logger.warning("PromotionGate: aggregator ingest failed: %s", e)

            self._promotion_log.append({
                "agent": agent_name,
                "decision": decision.value,
                "category": category.value,
                "score": score,
                "timestamp": time.time(),
            })
            return result

    def _compute_importance(self, content: str, agent_name: str) -> float:
        """Heuristic importance scoring for promotion gate."""
        score = 0.3
        lower = content.lower()

        high_signal = ["error", "fail", "critical", "important", "urgent",
                       "crash", "config", "decision", "break", "fix"]
        med_signal = ["result", "complete", "finish", "summary", "report",
                      "update", "change", "modify"]
        low_signal = ["log", "debug", "ping", "heartbeat", "poll", "idle"]

        score += sum(0.10 for kw in high_signal if kw in lower)
        score += sum(0.05 for kw in med_signal if kw in lower)
        score -= sum(0.03 for kw in low_signal if kw in lower)

        if len(content) > 200:
            score += 0.08
        if len(content) > 500:
            score += 0.05

        return max(0.0, min(1.0, score))

    def _classify_memory(self, content: str) -> MemoryCategory:
        """Classify memory into Oracle 5-type taxonomy."""
        lower = content.lower()

        policy_signals = ["must", "should", "always", "never", "rule",
                          "policy", "constraint", "require", "forbidden"]
        preference_signals = ["prefer", "like", "dislike", "usually",
                              "style", "format", "template"]
        fact_signals = ["is", "was", "version", "path", "config",
                        "located", "defined", "equals"]
        trace_signals = ["log", "debug", "trace", "heartbeat", "poll"]

        if any(s in lower for s in policy_signals):
            return MemoryCategory.POLICY
        if any(s in lower for s in preference_signals):
            return MemoryCategory.PREFERENCE
        if any(s in lower for s in trace_signals):
            return MemoryCategory.TRACE
        if any(s in lower for s in fact_signals):
            return MemoryCategory.FACT
        return MemoryCategory.EPISODIC

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "promotions_evaluated": len(self._promotion_log),
                "promotion_threshold": self.promotion_threshold,
            }


# ── AgentBridge ───────────────────────────────────────────────────────────

class AgentBridge:
    """Standardized bridge between Main Agent and Trinity memory.

    Implements the Embedded Context Pattern:
      1. Main Agent calls prepare_context() before dispatching a sub-task
      2. Bridge retrieves relevant memories from Trinity
      3. Memories are embedded into the message payload as context
      4. Sub-agent receives context, completes task, returns result
      5. Main Agent calls after_task() → Promotion Gate → persist if warranted

    v6.95.0: Shared pool mode via aggregator — queries global context
    across all agents instead of per-agent isolation.
    """

    def __init__(self, brain=None, aggregator: Any = None, event_collector: Any = None):
        """
        Args:
            brain: AgentBrain instance.  If None, bridge operates in
                   standalone/offline mode.
            aggregator: MemoryAggregator instance for shared pool mode (v6.95.0).
            event_collector: EventDrivenCollector instance for active memory
                             capture (v6.96.0). When set, hooks are triggered
                             at key bridge lifecycle points.
        """
        self._brain = brain
        self._lock = threading.RLock()
        self._aggregator = aggregator  # v6.95.0
        self._event_collector = event_collector  # v6.96.0
        self._promotion_gate = PromotionGate(
            aggregator=aggregator if aggregator else None,
        )
        self._context_registry: Dict[str, BridgeContext] = {}
        self._bridge_stats: Dict[str, int] = {
            "contexts_prepared": 0,
            "tasks_promoted": 0,
            "tasks_discarded": 0,
            "contexts_served": 0,
        }
        logger.info("AgentBridge initialized (brain=%s, aggregator=%s, event_collector=%s)",
                     "attached" if brain else "standalone",
                     "attached" if aggregator else "none",
                     "attached" if event_collector else "none")

    # ── Core API ──────────────────────────────────────────────────────

    def prepare_context(
        self,
        agent_name: str,
        task_desc: str,
        recent_history: Optional[List[Dict[str, Any]]] = None,
        top_k: int = BRIDGE_DEFAULT_TOP_K,
    ) -> Dict[str, Any]:
        """Assemble an Embedded Context block for dispatch_task.

        Args:
            agent_name: Target sub-agent name (e.g., 'file-agent')
            task_desc: Description of the task being dispatched
            recent_history: Recent conversation turns for context
            top_k: Max number of relevant memories to retrieve

        Returns:
            Dict with keys: context_text, memories, policy_hints,
            preference_hints, context_id
        """
        with self._lock:
            self._bridge_stats["contexts_prepared"] += 1

            # v6.96.0: active collection — hook conversation_start
            if self._event_collector is not None:
                try:
                    self._event_collector.hook_conversation_start(
                        agent_name, task_desc,
                        metadata={"top_k": top_k, "has_history": bool(recent_history)},
                    )
                except Exception:
                    pass

            ctx = BridgeContext(agent_name=agent_name)
            ctx.context_id = f"ctx_{agent_name}_{int(time.time() * 1000)}"

            # 1. Retrieve relevant memories from Trinity
            memories: List[Dict[str, Any]] = []
            policy_hints: List[str] = []
            preference_hints: List[str] = []
            fact_snippets: List[str] = []
            dimension_tags: Dict[str, Any] = {}

            # v6.95.0: shared pool mode via aggregator
            if self._aggregator is not None:
                global_ctx = self._aggregator.get_global_context()
                for dv in global_ctx:
                    mem_data = {
                        "type": getattr(getattr(dv, "category", None), "value", "episodic"),
                        "content": dv.content,
                        "importance": dv.confidence,
                        "timestamp": dv.created_at,
                        "source_agents": list(dv.source_agents),
                    }
                    memories.append(mem_data)

                    lower = dv.content.lower()
                    if any(kw in lower for kw in ["must", "always", "never", "rule", "config"]):
                        policy_hints.append(dv.content[:200])
                    if any(kw in lower for kw in ["prefer", "like", "usually", "style"]):
                        preference_hints.append(dv.content[:200])
                    if any(kw in lower for kw in ["version", "path", "located", "is"]):
                        fact_snippets.append(dv.content[:200])

                # Dimension tags from aggregator
                try:
                    agg_stats = self._aggregator.statistics()
                    dimension_tags["total_pool_size"] = agg_stats.get("total_memories", 0)
                    dimension_tags["source_distribution"] = agg_stats.get("source_distribution", {})
                    dimension_tags["categories"] = agg_stats.get("category_distribution", {})
                except Exception:
                    pass

            elif self._brain:
                # Legacy fallback: query per-agent memories
                events = self._brain._query_relevant_memories(
                    f"{agent_name} {task_desc}", top_k=top_k
                )
                for evt in events:
                    mem_dict = {
                        "type": evt.event_type,
                        "content": evt.content,
                        "importance": evt.importance,
                        "timestamp": evt.timestamp,
                    }
                    memories.append(mem_dict)

                    lower = evt.content.lower()
                    if any(kw in lower for kw in ["must", "always", "never", "rule", "config"]):
                        policy_hints.append(evt.content[:200])
                    if any(kw in lower for kw in ["prefer", "like", "usually", "style"]):
                        preference_hints.append(evt.content[:200])
                    if any(kw in lower for kw in ["version", "path", "located", "is"]):
                        fact_snippets.append(evt.content[:200])

            ctx.dimension_tags = dimension_tags

            ctx.relevant_memories = memories[:BRIDGE_MAX_MEMORIES_PER_CONTEXT]
            ctx.policy_hints = policy_hints[:3]
            ctx.preference_hints = preference_hints[:3]
            ctx.fact_snippets = fact_snippets[:5]

            # 2. Assemble the context text (Embedded Context pattern)
            parts: List[str] = []
            if ctx.policy_hints:
                parts.append("[Policy Constraints]")
                for h in ctx.policy_hints:
                    parts.append(f"  - {h}")
            if ctx.preference_hints:
                parts.append("\n[User/Agent Preferences]")
                for h in ctx.preference_hints:
                    parts.append(f"  - {h}")
            if ctx.fact_snippets:
                parts.append("\n[Known Facts]")
                for f in ctx.fact_snippets:
                    parts.append(f"  - {f}")
            if ctx.relevant_memories:
                parts.append("\n[Relevant History]")
                for m in ctx.relevant_memories:
                    parts.append(f"  - [{m['type']}] {m['content'][:150]}")

            ctx.assembled_text = "\n".join(parts)
            ctx.total_tokens_estimate = len(ctx.assembled_text.split())

            self._context_registry[ctx.context_id] = ctx
            self._bridge_stats["contexts_served"] += 1

            return {
                "context_id": ctx.context_id,
                "context_text": ctx.assembled_text,
                "memories": ctx.relevant_memories,
                "policy_hints": ctx.policy_hints,
                "preference_hints": ctx.preference_hints,
                "token_estimate": ctx.total_tokens_estimate,
            }

    def after_task(
        self,
        agent_name: str,
        task_result: str,
        context_id: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Post-task hook: run Promotion Gate → persist if warranted.

        Args:
            agent_name: The sub-agent that completed the task
            task_result: Summary or result of the completed task
            context_id: Optional context ID from prepare_context
            metadata: Additional metadata

        Returns:
            Dict with: decision, category, importance, persisted
        """
        with self._lock:
            # v6.96.0: active collection — hook tool_call (after task)
            if self._event_collector is not None:
                try:
                    self._event_collector.hook_tool_call(
                        agent_name,
                        "bridge.after_task",
                        phase="after",
                        result_preview=task_result[:300],
                        metadata={
                            **(metadata or {}),
                            "context_id": context_id,
                        },
                    )
                except Exception:
                    pass

            # Run through Promotion Gate
            gate_result = self._promotion_gate.evaluate(
                task_result, agent_name, metadata
            )

            persisted = False
            memory_unit_id: Optional[str] = None

            if gate_result.decision == PromotionDecision.PERSIST:
                # v6.95.0: shared pool mode — write to aggregator
                if self._aggregator is not None:
                    try:
                        dv = self._aggregator.ingest(
                            content=f"[{agent_name}][{gate_result.memory_category.value}] "
                                    f"{task_result}",
                            source_agent=agent_name,
                            metadata={
                                **(metadata or {}),
                                "context_id": context_id,
                                "memory_category": gate_result.memory_category.value,
                                "source": "promotion_gate",
                            },
                        )
                        if dv is not None:
                            persisted = True
                            memory_unit_id = dv.memory_id
                            self._bridge_stats["tasks_promoted"] += 1
                    except Exception as e:
                        logger.warning("after_task: aggregator ingest failed: %s", e)
                elif self._brain:
                    # Legacy: persist via DecisionEngine
                    decision, score, _ = self._brain.decision_engine.should_remember(
                        content=f"[{agent_name}][{gate_result.memory_category.value}] "
                                f"{task_result}",
                    )
                    if decision:
                        self._brain._record_event(
                            agent_name=agent_name,
                            event_type=f"bridge_{gate_result.memory_category.value}",
                            content=task_result,
                            importance=gate_result.importance_score,
                            metadata={
                                **(metadata or {}),
                                "context_id": context_id,
                                "memory_category": gate_result.memory_category.value,
                                "source": "promotion_gate",
                            },
                        )
                        persisted = True
                        self._bridge_stats["tasks_promoted"] += 1

            if gate_result.decision == PromotionDecision.DISCARD:
                self._bridge_stats["tasks_discarded"] += 1

            # Clean up context registry
            if context_id and context_id in self._context_registry:
                del self._context_registry[context_id]

            return {
                "agent_name": agent_name,
                "decision": gate_result.decision.value,
                "category": gate_result.memory_category.value,
                "importance": gate_result.importance_score,
                "persisted": persisted,
                "reason": gate_result.reason,
            }

    def assemble_agent_prompt(
        self,
        agent_name: str,
        current_task: str,
    ) -> str:
        """Assemble a prompt context string using Oracle 4 memory types.

        Retrieves Policy/Preference/Fact/Episodic memories for the agent
        and formats them as a structured prompt prefix.
        v6.95.0: Queries shared pool via aggregator when available.
        """
        with self._lock:
            parts: List[str] = [f"[Trinity Memory Context — {agent_name}]"]

            # v6.95.0: shared pool mode
            if self._aggregator is not None:
                try:
                    all_memories = self._aggregator.get_by_agent(agent_name)
                except Exception:
                    all_memories = []

                policies = [m for m in all_memories
                            if "policy" in m.content.lower()
                            or "must" in m.content.lower()
                            or "rule" in m.content.lower()]
                if policies:
                    parts.append("\n## System Policies")
                    for p in policies[-3:]:
                        parts.append(f"- {p.content[:200]}")

                prefs = [m for m in all_memories
                         if "preference" in m.content.lower()
                         or "prefer" in m.content.lower()]
                if prefs:
                    parts.append("\n## User Preferences")
                    for p in prefs[-3:]:
                        parts.append(f"- {p.content[:200]}")

                facts = [m for m in all_memories
                         if "fact" in m.content.lower()
                         or "version" in m.content.lower()
                         or "config" in m.content.lower()]
                if facts:
                    parts.append("\n## Known Facts")
                    for f in facts[-5:]:
                        parts.append(f"- {f.content[:200]}")

                parts.append(f"\n## Current Task\n{current_task}")
                return "\n".join(parts)

            # Legacy fallback
            if self._brain:
                # Policy memories
                events = self._brain._get_agent_events(agent_name, limit=200)
                policies = [e for e in events if "policy" in e.event_type.lower()
                            or "must" in e.content.lower()
                            or "rule" in e.content.lower()]
                if policies:
                    parts.append("\n## System Policies")
                    for p in policies[-3:]:
                        parts.append(f"- {p.content[:200]}")

                # Preferences
                prefs = [e for e in events if "preference" in e.event_type.lower()
                         or "prefer" in e.content.lower()]
                if prefs:
                    parts.append("\n## User Preferences")
                    for p in prefs[-3:]:
                        parts.append(f"- {p.content[:200]}")

                # Facts
                facts = [e for e in events if "fact" in e.event_type.lower()
                         or "version" in e.content.lower()
                         or "config" in e.content.lower()]
                if facts:
                    parts.append("\n## Known Facts")
                    for f in facts[-5:]:
                        parts.append(f"- {f.content[:200]}")

                # Relevant episodic
                relevant = self._brain._query_relevant_memories(
                    f"{agent_name} {current_task}", top_k=5
                )
                if relevant:
                    parts.append("\n## Relevant History")
                    for r in relevant:
                        parts.append(f"- [{r.event_type}] {r.content[:200]}")

            parts.append(f"\n## Current Task\n{current_task}")
            return "\n".join(parts)

    def sync_agent_state(self, agent_name: str) -> Dict[str, Any]:
        """Get a complete memory state summary for an agent (v6.95.0: aggregator)."""
        with self._lock:
            # v6.95.0: shared pool mode
            if self._aggregator is not None:
                try:
                    agent_memories = self._aggregator.get_by_agent(agent_name)
                    category_counts: Dict[str, int] = {
                        "policy": 0, "preference": 0, "fact": 0,
                        "episodic": 0, "trace": 0,
                    }
                    for mem in agent_memories:
                        lower = mem.content.lower()
                        if any(kw in lower for kw in ["must", "rule", "policy"]):
                            category_counts["policy"] += 1
                        elif any(kw in lower for kw in ["prefer", "like", "style"]):
                            category_counts["preference"] += 1
                        elif any(kw in lower for kw in ["version", "config", "defined"]):
                            category_counts["fact"] += 1
                        elif any(kw in lower for kw in ["log", "trace", "heartbeat"]):
                            category_counts["trace"] += 1
                        else:
                            category_counts["episodic"] += 1

                    return {
                        "agent_name": agent_name,
                        "total_memories": len(agent_memories),
                        "memory_categories": category_counts,
                        "status": "ok",
                    }
                except Exception as e:
                    logger.warning("sync_agent_state: aggregator query failed: %s", e)

            if self._brain:
                ctx = self._brain.agent_context.get_agent_context(agent_name)
                events = self._brain._get_agent_events(agent_name, limit=100)

                category_counts: Dict[str, int] = {
                    "policy": 0, "preference": 0, "fact": 0,
                    "episodic": 0, "trace": 0,
                }
                for e in events:
                    lower = e.content.lower()
                    if any(kw in lower for kw in ["must", "rule", "policy"]):
                        category_counts["policy"] += 1
                    elif any(kw in lower for kw in ["prefer", "like", "style"]):
                        category_counts["preference"] += 1
                    elif any(kw in lower for kw in ["version", "config", "defined"]):
                        category_counts["fact"] += 1
                    elif any(kw in lower for kw in ["log", "trace", "heartbeat"]):
                        category_counts["trace"] += 1
                    else:
                        category_counts["episodic"] += 1

                return {
                    "agent_name": agent_name,
                    "profile": ctx.get("profile", {}),
                    "stats": ctx.get("stats", {}),
                    "total_memories": len(events),
                    "memory_categories": category_counts,
                    "recent_events_count": len(ctx.get("recent_events", [])),
                }
            return {
                "agent_name": agent_name,
                "status": "offline",
                "total_memories": 0,
            }

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                **self._bridge_stats,
                "promotion_gate": self._promotion_gate.statistics(),
                "context_registry_size": len(self._context_registry),
            }


# ── Factory ───────────────────────────────────────────────────────────────

def create_agent_bridge(brain=None, aggregator=None, event_collector=None) -> AgentBridge:
    """Factory function for AgentBridge (v6.96.0: aggregator + event_collector support)."""
    return AgentBridge(brain=brain, aggregator=aggregator, event_collector=event_collector)


# ── Self-Test ─────────────────────────────────────────────────────────────

def self_test() -> bool:
    """Comprehensive self-test for the AgentBridge module (v6.96.0)."""
    print("=" * 60)
    print("  Trinity Agent Bridge v6.96.0 — Self Test")
    print("=" * 60)
    passed = 0
    total = 0

    # ── Test 1: Bridge creation (standalone) ──
    total += 1
    print("\n[Test 1] AgentBridge creation (standalone)")
    try:
        bridge = create_agent_bridge()
        assert bridge._brain is None
        assert bridge._aggregator is None
        assert bridge._bridge_stats["contexts_prepared"] == 0
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 2: Bridge creation with aggregator (v6.95.0) ──
    total += 1
    print("\n[Test 2] Bridge creation with aggregator (v6.95.0)")
    try:
        from trinity.agents.aggregator import create_aggregator
        agg = create_aggregator()
        bridge_agg = create_agent_bridge(aggregator=agg)
        assert bridge_agg._aggregator is not None
        assert bridge_agg._promotion_gate._aggregator is not None
        print(f"    aggregator attached, pool={len(agg._pool)}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 3: PromotionGate evaluate ──
    total += 1
    print("\n[Test 3] PromotionGate evaluate")
    try:
        gate = PromotionGate(promotion_threshold=0.35)

        # High importance → PERSIST
        result = gate.evaluate(
            "CRITICAL: database connection failed during report generation",
            "file-agent",
        )
        assert result.decision == PromotionDecision.PERSIST, \
            f"Expected PERSIST, got {result.decision}"
        assert result.importance_score > 0.35
        print(f"    critical task → {result.decision.value} (score={result.importance_score:.3f})")

        # Low importance → DISCARD
        result2 = gate.evaluate("ping", "file-agent")
        assert result2.decision == PromotionDecision.DISCARD, \
            f"Expected DISCARD, got {result2.decision}"
        print(f"    ping → {result2.decision.value} (score={result2.importance_score:.3f})")

        # Borderline
        result3 = gate.evaluate("Task completed successfully with 3 files", "file-agent")
        print(f"    normal task → {result3.decision.value} (score={result3.importance_score:.3f})")

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 4: PromotionGate with aggregator ingest (v6.95.0) ──
    total += 1
    print("\n[Test 4] PromotionGate with aggregator ingest (v6.95.0)")
    try:
        from trinity.agents.aggregator import create_aggregator
        agg2 = create_aggregator()
        gate_agg = PromotionGate(promotion_threshold=0.20, aggregator=agg2)

        result = gate_agg.evaluate(
            "CRITICAL: security breach detected, immediate action required",
            "security-agent",
        )
        assert result.decision == PromotionDecision.PERSIST
        assert result.dimension_vector is not None, "dimension_vector should be set"
        assert result.dimension_vector.memory_id is not None
        print(f"    decision={result.decision.value}, "
              f"memory_id={result.dimension_vector.memory_id[:12]}...")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 5: Oracle 5-type classification ──
    total += 1
    print("\n[Test 5] Oracle 5-type memory classification")
    try:
        gate = PromotionGate()

        policy = gate._classify_memory("You must always back up before deleting")
        assert policy == MemoryCategory.POLICY, f"Expected POLICY, got {policy}"
        print(f"    policy statement → {policy.value}")

        pref = gate._classify_memory("User prefers dark mode")
        assert pref == MemoryCategory.PREFERENCE, f"Expected PREFERENCE, got {pref}"
        print(f"    preference → {pref.value}")

        fact = gate._classify_memory("The config file is located at /etc/trinity/config.yaml")
        assert fact == MemoryCategory.FACT, f"Expected FACT, got {fact}"
        print(f"    fact → {fact.value}")

        trace = gate._classify_memory("heartbeat log entry at 2026-08-10 12:00")
        assert trace == MemoryCategory.TRACE, f"Expected TRACE, got {trace}"
        print(f"    trace → {trace.value}")

        epi = gate._classify_memory("Processed invoice.pdf successfully")
        assert epi == MemoryCategory.EPISODIC, f"Expected EPISODIC, got {epi}"
        print(f"    episodic → {epi.value}")

        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 6: prepare_context (standalone) ──
    total += 1
    print("\n[Test 6] prepare_context (standalone)")
    try:
        ctx = bridge.prepare_context(
            agent_name="file-agent",
            task_desc="Process invoice PDF files",
            recent_history=[
                {"role": "user", "content": "Find my invoices"},
            ],
            top_k=5,
        )
        assert "context_id" in ctx
        assert "context_text" in ctx
        assert "memories" in ctx
        assert ctx["token_estimate"] >= 0
        print(f"    context_id: {ctx['context_id'][:40]}...")
        print(f"    token_estimate: {ctx['token_estimate']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 7: after_task (standalone) ──
    total += 1
    print("\n[Test 7] after_task (standalone)")
    try:
        result = bridge.after_task(
            agent_name="file-agent",
            task_result="Extracted 5 invoices, total $12,345.67",
            context_id=ctx["context_id"],
        )
        assert "decision" in result
        assert "category" in result
        assert result["persisted"] is False  # standalone, no brain/aggregator
        print(f"    decision: {result['decision']}, category: {result['category']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 8: assemble_agent_prompt (standalone) ──
    total += 1
    print("\n[Test 8] assemble_agent_prompt (standalone)")
    try:
        prompt = bridge.assemble_agent_prompt(
            agent_name="file-agent",
            current_task="Organize desktop files by type",
        )
        assert "Trinity Memory Context" in prompt
        assert "Current Task" in prompt
        print(f"    prompt length: {len(prompt)} chars")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 9: sync_agent_state (standalone) ──
    total += 1
    print("\n[Test 9] sync_agent_state (standalone)")
    try:
        state = bridge.sync_agent_state("file-agent")
        assert state["agent_name"] == "file-agent"
        assert state["total_memories"] == 0  # standalone
        print(f"    status: {state.get('status', 'ok')}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 10: statistics ──
    total += 1
    print("\n[Test 10] statistics")
    try:
        stats = bridge.statistics()
        assert "contexts_prepared" in stats
        assert stats["contexts_prepared"] >= 1
        print(f"    contexts_prepared: {stats['contexts_prepared']}")
        print("  PASS")
        passed += 1
    except Exception as e:
        print(f"  FAIL: {e}")

    # ── Test 11: Event-driven collection integration (v6.96.0) ──
    total += 1
    print("\n[Test 11] Event-driven collection integration (v6.96.0)")
    try:
        from trinity.memory.active_collector import EventDrivenCollector
        ec = EventDrivenCollector(importance_threshold=0.10)
        bridge_ec = create_agent_bridge(event_collector=ec)
        assert bridge_ec._event_collector is not None
        assert bridge_ec._event_collector is ec

        # prepare_context should trigger hook_conversation_start
        ctx2 = bridge_ec.prepare_context(
            agent_name="file-agent",
            task_desc="Test active collection",
        )
        assert bridge_ec._bridge_stats["contexts_prepared"] >= 1

        # after_task should trigger hook_tool_call (after)
        result = bridge_ec.after_task(
            agent_name="file-agent",
            task_result="Task completed: 3 files processed",
            context_id=ctx2["context_id"],
        )
        assert "decision" in result

        ec_stats = ec.statistics()
        print(f"    events captured: {ec_stats['events_captured']}")
        print(f"    events in buffer: {ec_stats['buffer_size']}")
        assert ec_stats["events_captured"] >= 2  # conversation_start + tool_call
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
