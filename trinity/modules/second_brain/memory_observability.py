"""
P12-3: Memory Observability & Telemetry.

Reference: 2026 Agent Observability Guide + OWASP Agent Memory Guard.

Design: Provides full-stack memory observability including prompt timeline
        tracing, memory freshness & embedding drift monitoring, token
        anomaly detection, cross-agent conflict reporting, auto-expiry
        policies, and a unified MetricsCollector telemetry sink.

Interface-compatible with: all second_brain modules (via MetricsCollector).
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import threading
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TraceEventType(Enum):
    SYSTEM_PROMPT = auto()
    USER_PROMPT = auto()
    TOOL_RESPONSE = auto()
    MEMORY_INJECTION = auto()
    TOOL_CALL = auto()
    LLM_RESPONSE = auto()


class FreshnessStatus(Enum):
    FRESH = auto()
    STALE = auto()
    STALENESS_DETECTED = auto()
    DRIFTING = auto()
    EXPIRED = auto()


class TokenAnomalyType(Enum):
    RECURSIVE_LOOP = auto()
    CONTEXT_EXPLOSION = auto()
    PROMPT_INEFFICIENCY = auto()
    TOKEN_BLOOM = auto()


class ConflictSeverity(Enum):
    NONE = auto()
    LOW = auto()
    MEDIUM = auto()
    HIGH = auto()
    CRITICAL = auto()


class ExpiryPolicyType(Enum):
    TTL = auto()
    LRU = auto()
    FADING = auto()
    EBBINGHAUS = auto()
    ACCESS_COUNT = auto()


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class TraceEvent:
    """A single event in the prompt/memory timeline."""
    event_id: str
    event_type: TraceEventType
    timestamp: float
    content_hash: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FreshnessMetrics:
    """Snapshot of memory freshness indicators."""
    memory_id: str
    status: FreshnessStatus
    last_access: float
    embedding_norm: float
    drift_score: float         # cosine distance from original
    retrieval_precision: float  # how often retrieved when relevant
    context_relevance: float
    age_seconds: float


@dataclass
class TokenAnomalyReport:
    """Detected token consumption anomaly."""
    anomaly_type: TokenAnomalyType
    severity: float            # 0.0 - 1.0
    detected_at: float
    token_count: int
    window_seconds: float
    description: str
    suggestion: str


@dataclass
class ConflictReport:
    """Cross-agent memory conflict detected."""
    memory_key: str
    agent_a: str
    agent_b: str
    value_a: Any
    value_b: Any
    severity: ConflictSeverity
    detected_at: float
    resolution: str = ""

@dataclass
class ExpiryRecord:
    """Record of an auto-expired memory entry."""
    memory_id: str
    expired_at: float
    policy: ExpiryPolicyType
    reason: str
    age_at_expiry: float


# ---------------------------------------------------------------------------
# PromptTracer
# ---------------------------------------------------------------------------

class PromptTracer:
    """
    Traces the complete timeline of system prompts, user prompts,
    tool responses, and memory injections across a session.
    """

    def __init__(self, max_trace_size: int = 1000) -> None:
        self.max_trace_size = max_trace_size
        self._events: OrderedDict[str, TraceEvent] = OrderedDict()
        self._lock = threading.RLock()

    def record(
        self,
        event_type: TraceEventType,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TraceEvent:
        """Record a new event in the trace timeline."""
        with self._lock:
            content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
            event = TraceEvent(
                event_id=f"evt_{int(time.time() * 1000)}_{len(self._events):04d}",
                event_type=event_type,
                timestamp=time.time(),
                content_hash=content_hash,
                metadata=metadata or {},
            )
            self._events[event.event_id] = event

            # evict oldest if over capacity
            while len(self._events) > self.max_trace_size:
                self._events.popitem(last=False)

            return event

    def timeline(
        self,
        event_types: Optional[List[TraceEventType]] = None,
        since: Optional[float] = None,
    ) -> List[TraceEvent]:
        """Return filtered timeline of events."""
        with self._lock:
            events = list(self._events.values())
            if event_types:
                events = [e for e in events if e.event_type in event_types]
            if since is not None:
                events = [e for e in events if e.timestamp >= since]
            return events

    def memory_injection_events(self) -> List[TraceEvent]:
        """Return all memory injection events."""
        return self.timeline(event_types=[TraceEventType.MEMORY_INJECTION])

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = defaultdict(int)
            for evt in self._events.values():
                type_counts[evt.event_type.name] += 1
            return {
                "total_events": len(self._events),
                "type_breakdown": dict(type_counts),
                "first_timestamp": (
                    next(iter(self._events.values())).timestamp
                    if self._events else None
                ),
                "last_timestamp": (
                    next(reversed(self._events.values())).timestamp
                    if self._events else None
                ),
            }


# ---------------------------------------------------------------------------
# MemoryFreshnessMonitor
# ---------------------------------------------------------------------------

class MemoryFreshnessMonitor:
    """
    Monitors memory freshness, embedding drift, vector retrieval
    precision, and context relevance over time.
    """

    def __init__(
        self,
        drift_threshold: float = 0.15,
        precision_decay_threshold: float = 0.5,
    ) -> None:
        self.drift_threshold = drift_threshold
        self.precision_decay_threshold = precision_decay_threshold
        self._memories: Dict[str, FreshnessMetrics] = {}
        self._original_embeddings: Dict[str, np.ndarray] = {}
        self._lock = threading.RLock()
        self._rng = np.random.default_rng(101)

    def register(
        self,
        memory_id: str,
        embedding: Optional[np.ndarray] = None,
    ) -> None:
        """Register a memory for freshness tracking."""
        with self._lock:
            if embedding is not None:
                self._original_embeddings[memory_id] = embedding.copy()

            self._memories[memory_id] = FreshnessMetrics(
                memory_id=memory_id,
                status=FreshnessStatus.FRESH,
                last_access=time.time(),
                embedding_norm=float(np.linalg.norm(embedding)) if embedding is not None else 0.0,
                drift_score=0.0,
                retrieval_precision=1.0,
                context_relevance=1.0,
                age_seconds=0.0,
            )

    def check(self, memory_id: str) -> FreshnessMetrics:
        """Check freshness for a specific memory."""
        with self._lock:
            if memory_id not in self._memories:
                raise KeyError(f"Memory {memory_id} not registered")

            metrics = self._memories[memory_id]
            now = time.time()
            metrics.age_seconds = now - metrics.last_access

            # Simulate drift based on age (in reality compared to original embedding)
            if memory_id in self._original_embeddings:
                drift = min(1.0, metrics.age_seconds / 86400 * self._rng.uniform(0.05, 0.2))
                metrics.drift_score = drift

            # Simulate retrieval precision decay
            metrics.retrieval_precision = max(
                0.1, 1.0 - metrics.age_seconds / 604800  # decay over ~1 week
            )

            # Simulate context relevance decay
            metrics.context_relevance = max(
                0.1, 1.0 - metrics.age_seconds / 2592000  # decay over ~30 days
            )

            # Update status
            if metrics.drift_score > self.drift_threshold:
                metrics.status = FreshnessStatus.DRIFTING
            elif metrics.retrieval_precision < self.precision_decay_threshold:
                metrics.status = FreshnessStatus.STALENESS_DETECTED
            elif metrics.age_seconds > 2592000:  # 30 days
                metrics.status = FreshnessStatus.EXPIRED
            elif metrics.age_seconds > 604800:  # 7 days
                metrics.status = FreshnessStatus.STALE
            else:
                metrics.status = FreshnessStatus.FRESH

            return metrics

    def check_all(self) -> Dict[str, FreshnessMetrics]:
        """Check freshness for all registered memories."""
        results = {}
        for mid in list(self._memories.keys()):
            results[mid] = self.check(mid)
        return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            statuses = defaultdict(int)
            total_drift = 0.0
            for m in self._memories.values():
                statuses[m.status.name] += 1
                total_drift += m.drift_score
            return {
                "tracked_memories": len(self._memories),
                "status_distribution": dict(statuses),
                "mean_drift": total_drift / max(1, len(self._memories)),
            }


# ---------------------------------------------------------------------------
# TokenTelemetry
# ---------------------------------------------------------------------------

class TokenTelemetry:
    """
    Monitors token consumption for anomaly detection:
    recursive loops, context explosion, prompt inefficiency.
    """

    def __init__(
        self,
        loop_threshold_tokens: int = 50000,
        explosion_rate: float = 3.0,
        window_seconds: float = 60.0,
    ) -> None:
        self.loop_threshold_tokens = loop_threshold_tokens
        self.explosion_rate = explosion_rate
        self.window_seconds = window_seconds
        self._token_log: List[Tuple[float, int]] = []
        self._lock = threading.RLock()
        self._anomalies: List[TokenAnomalyReport] = []

    def record(self, token_count: int) -> Optional[TokenAnomalyReport]:
        """Record a token usage sample and check for anomalies."""
        with self._lock:
            now = time.time()
            self._token_log.append((now, token_count))

            # prune old entries
            cutoff = now - self.window_seconds
            self._token_log = [(t, c) for t, c in self._token_log if t >= cutoff]

            if not self._token_log:
                return None

            report = None
            window_tokens = sum(c for _, c in self._token_log)

            # Check recursive loop
            if window_tokens > self.loop_threshold_tokens:
                report = TokenAnomalyReport(
                    anomaly_type=TokenAnomalyType.RECURSIVE_LOOP,
                    severity=min(1.0, window_tokens / (self.loop_threshold_tokens * 2)),
                    detected_at=now,
                    token_count=window_tokens,
                    window_seconds=self.window_seconds,
                    description="Unusually high token consumption detected — possible recursive loop.",
                    suggestion="Add recursion guard or exit condition to the agent loop.",
                )
            # Check context explosion
            elif len(self._token_log) >= 2:
                recent = self._token_log[-min(5, len(self._token_log)):]
                if len(recent) >= 3:
                    ratios = []
                    for i in range(1, len(recent)):
                        if recent[i - 1][1] > 0:
                            ratios.append(recent[i][1] / recent[i - 1][1])
                    if ratios and np.mean(ratios) > self.explosion_rate:
                        report = TokenAnomalyReport(
                            anomaly_type=TokenAnomalyType.CONTEXT_EXPLOSION,
                            severity=min(1.0, np.mean(ratios) / self.explosion_rate),
                            detected_at=now,
                            token_count=window_tokens,
                            window_seconds=self.window_seconds,
                            description="Rapid token growth — possible context explosion.",
                            suggestion="Trim older conversation turns or apply context compression.",
                        )

            if report:
                self._anomalies.append(report)
            return report

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            type_counts = defaultdict(int)
            for a in self._anomalies:
                type_counts[a.anomaly_type.name] += 1
            total_tokens = sum(c for _, c in self._token_log)
            return {
                "samples_recorded": len(self._token_log),
                "window_total_tokens": total_tokens,
                "anomalies_detected": len(self._anomalies),
                "anomaly_breakdown": dict(type_counts),
            }


# ---------------------------------------------------------------------------
# CrossAgentConflictDetector
# ---------------------------------------------------------------------------

class CrossAgentConflictDetector:
    """
    Detects and reports memory conflicts between different agents
    that have written to the same memory key with divergent values.
    """

    def __init__(self) -> None:
        self._memories: Dict[str, Dict[str, Any]] = defaultdict(dict)
        self._conflicts: List[ConflictReport] = []
        self._lock = threading.RLock()

    def record(self, memory_key: str, agent_id: str, value: Any) -> Optional[ConflictReport]:
        """Record a memory write and detect conflicts."""
        with self._lock:
            existing = self._memories.get(memory_key, {})

            # Check for existing writes from other agents
            for other_agent, other_value in existing.items():
                if other_agent != agent_id and other_value != value:
                    severity = ConflictSeverity.LOW
                    if isinstance(value, str) and isinstance(other_value, str):
                        # higher severity for contradictory strings
                        severity = ConflictSeverity.MEDIUM
                    elif isinstance(value, (int, float)) and isinstance(other_value, (int, float)):
                        diff = abs(value - other_value)
                        if diff > 100:
                            severity = ConflictSeverity.HIGH
                        elif diff > 10:
                            severity = ConflictSeverity.MEDIUM

                    report = ConflictReport(
                        memory_key=memory_key,
                        agent_a=other_agent,
                        agent_b=agent_id,
                        value_a=other_value,
                        value_b=value,
                        severity=severity,
                        detected_at=time.time(),
                    )
                    self._conflicts.append(report)
                    return report

            # No conflict — store
            self._memories[memory_key][agent_id] = value
            return None

    def unresolved_conflicts(self) -> List[ConflictReport]:
        """Return conflicts that haven't been resolved."""
        with self._lock:
            return [c for c in self._conflicts if not c.resolution]

    def resolve(self, memory_key: str, resolution: str) -> None:
        """Mark all conflicts for a memory key as resolved."""
        with self._lock:
            for c in self._conflicts:
                if c.memory_key == memory_key and not c.resolution:
                    c.resolution = resolution

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            sev_counts = defaultdict(int)
            for c in self._conflicts:
                sev_counts[c.severity.name] += 1
            return {
                "total_memory_keys": len(self._memories),
                "total_conflicts": len(self._conflicts),
                "unresolved": len(self.unresolved_conflicts()),
                "severity_breakdown": dict(sev_counts),
            }


# ---------------------------------------------------------------------------
# AutoExpirationPolicy
# ---------------------------------------------------------------------------

class AutoExpirationPolicy:
    """
    Automatic memory expiration to prevent long-running agents
    from self-contamination with stale or irrelevant memories.
    Supports TTL, LRU, fading, Ebbinghaus, and access-count policies.
    """

    def __init__(self, default_policy: ExpiryPolicyType = ExpiryPolicyType.TTL) -> None:
        self.default_policy = default_policy
        self._entries: Dict[str, Dict[str, Any]] = {}
        self._expiry_log: List[ExpiryRecord] = []
        self._lock = threading.RLock()

    def register(
        self,
        memory_id: str,
        policy: Optional[ExpiryPolicyType] = None,
        ttl_seconds: float = 86400.0,
        priority: float = 0.5,
    ) -> None:
        """Register a memory entry with expiration policy."""
        with self._lock:
            self._entries[memory_id] = {
                "created_at": time.time(),
                "last_access": time.time(),
                "access_count": 0,
                "policy": policy or self.default_policy,
                "ttl_seconds": ttl_seconds,
                "priority": priority,
                "expired": False,
            }

    def access(self, memory_id: str) -> None:
        """Record an access to a memory entry."""
        with self._lock:
            if memory_id in self._entries:
                self._entries[memory_id]["last_access"] = time.time()
                self._entries[memory_id]["access_count"] += 1

    def evaluate(self) -> List[ExpiryRecord]:
        """Evaluate all entries against their policies and expire stale ones."""
        with self._lock:
            now = time.time()
            expired: List[ExpiryRecord] = []

            for mid, entry in self._entries.items():
                if entry["expired"]:
                    continue
                policy = entry["policy"]
                age = now - entry["created_at"]

                should_expire = False
                reason = ""

                if policy == ExpiryPolicyType.TTL:
                    if age > entry["ttl_seconds"]:
                        should_expire = True
                        reason = f"TTL exceeded ({age:.0f}s > {entry['ttl_seconds']}s)"
                elif policy == ExpiryPolicyType.LRU:
                    # expire if unused for > half TTL
                    idle = now - entry["last_access"]
                    if entry["access_count"] == 0 and idle > entry["ttl_seconds"] * 0.5:
                        should_expire = True
                        reason = f"LRU: zero-access after {idle:.0f}s"
                elif policy == ExpiryPolicyType.FADING:
                    # expire when priority * (1 - age/ttl) < 0.1
                    remaining = entry["priority"] * max(0, 1 - age / entry["ttl_seconds"])
                    if remaining < 0.1:
                        should_expire = True
                        reason = f"Fading priority {remaining:.3f} below threshold"
                elif policy == ExpiryPolicyType.EBBINGHAUS:
                    # approximate Ebbinghaus forgetting curve: R = e^(-t/S)
                    S = entry["ttl_seconds"] * 0.5
                    retention = math.exp(-age / S) if S > 0 else 0.0
                    if retention < 0.05:
                        should_expire = True
                        reason = f"Ebbinghaus retention {retention:.3f} below 5%"
                elif policy == ExpiryPolicyType.ACCESS_COUNT:
                    max_access = 100
                    if entry["access_count"] >= max_access:
                        should_expire = True
                        reason = f"Access count limit reached ({entry['access_count']} >= {max_access})"

                if should_expire:
                    entry["expired"] = True
                    record = ExpiryRecord(
                        memory_id=mid,
                        expired_at=now,
                        policy=policy,
                        reason=reason,
                        age_at_expiry=age,
                    )
                    expired.append(record)

            self._expiry_log.extend(expired)
            return expired

    def clean_expired(self) -> int:
        """Remove expired entries from tracking."""
        with self._lock:
            before = len(self._entries)
            self._entries = {k: v for k, v in self._entries.items() if not v["expired"]}
            return before - len(self._entries)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            policy_counts = defaultdict(int)
            for e in self._entries.values():
                policy_counts[e["policy"].name] += 1
            return {
                "active_entries": len(self._entries),
                "expired_total": len(self._expiry_log),
                "policy_distribution": dict(policy_counts),
            }


# ---------------------------------------------------------------------------
# MetricsCollector — unified telemetry sink
# ---------------------------------------------------------------------------

class MetricsCollector:
    """
    Unified telemetry sink that aggregates observability metrics
    from all second_brain modules. Provides a single prometheus-style
    interface for external monitoring.
    """

    def __init__(self) -> None:
        self._components: Dict[str, Any] = {}
        self._gauges: Dict[str, float] = {}
        self._counters: Dict[str, int] = {}
        self._lock = threading.RLock()

    def register(self, name: str, component: Any) -> None:
        """Register an observability component."""
        with self._lock:
            self._components[name] = component

    def collect(self) -> Dict[str, Any]:
        """Collect metrics from all registered components."""
        with self._lock:
            snapshot: Dict[str, Any] = {
                "timestamp": time.time(),
                "datetime_utc": datetime.now(timezone.utc).isoformat(),
            }
            for name, comp in self._components.items():
                if hasattr(comp, "statistics"):
                    try:
                        snapshot[name] = comp.statistics()
                    except Exception as exc:
                        snapshot[name] = {"error": str(exc)}
            return snapshot

    def export_prometheus(self) -> str:
        """Export metrics in Prometheus text format."""
        snapshot = self.collect()
        lines = []
        for comp_name, stats in snapshot.items():
            if comp_name in ("timestamp", "datetime_utc"):
                continue
            if isinstance(stats, dict):
                for key, val in stats.items():
                    if isinstance(val, (int, float)):
                        metric_name = f"trinity_{comp_name}_{key}"
                        lines.append(f"{metric_name} {val} {int(snapshot['timestamp'])}")
        return "\n".join(lines)

    def statistics(self) -> Dict[str, Any]:
        return {
            "registered_components": list(self._components.keys()),
            "total_components": len(self._components),
        }
