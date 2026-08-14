"""Usage Analyzer — memory access pattern analysis engine.

Tracks memory accesses and detects usage patterns:
- Cyclic access (periodic revisits)
- Burst hotspots (sudden activity spikes)
- Co-reference (frequently accessed together)
- Forgetting curves (declining access frequency)
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import defaultdict
import math


# ═══════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class AccessEntry:
    """Single access record."""
    memory_id: str
    agent_id: str
    action: str            # search | read | ingest | reference
    context: str = ""      # query or trigger context
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class Hotspot:
    """A memory or memory cluster with elevated access frequency."""
    memory_id: str
    access_count: int
    avg_interval_seconds: float
    burst_factor: float      # multiplier over baseline
    co_referenced: List[str] = field(default_factory=list)
    pattern: str = ""        # cyclic | burst | co_ref


@dataclass
class UsagePattern:
    """Detected usage macro-pattern."""
    pattern_type: str        # cyclic | burst | co_ref | forgetting
    memory_ids: List[str]
    confidence: float        # 0.0–1.0
    description: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Heatmap:
    """7-day access heatmap for a single memory."""
    memory_id: str
    hourly_buckets: Dict[int, int]   # hour-since-start → access count
    total_accesses: int
    peak_hour: int
    peak_count: int


# ═══════════════════════════════════════════════════════════════════════════
# Analyzer
# ═══════════════════════════════════════════════════════════════════════════

class UsageAnalyzer:
    """Tracks and analyses memory access patterns.

    Parameters
    ----------
    baseline_window_hours : float
        Baseline is established from the oldest window of this duration.
    """

    def __init__(self, baseline_window_hours: float = 168.0):
        self._access_log: List[AccessEntry] = []
        self.baseline_window_hours = baseline_window_hours

    # ── Tracking ────────────────────────────────────────────────────────

    def track_access(
        self,
        memory_id: str,
        agent_id: str,
        action: str,
        context: str = "",
    ) -> AccessEntry:
        """Record a memory access event."""
        entry = AccessEntry(
            memory_id=memory_id,
            agent_id=agent_id,
            action=action,
            context=context,
        )
        self._access_log.append(entry)
        return entry

    def _access_log_in_window(self, hours: float) -> List[AccessEntry]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [
            e for e in self._access_log
            if datetime.fromisoformat(e.timestamp) > cutoff
        ]

    # ── Hotspots ────────────────────────────────────────────────────────

    def analyze_hotspots(self, time_window_hours: float = 24) -> List[Hotspot]:
        """Detect memory hotspots — items with elevated access frequency."""
        recent = self._access_log_in_window(time_window_hours)
        if not recent:
            return []

        # Per-memory access counts
        counts: Dict[str, List[AccessEntry]] = defaultdict(list)
        for e in recent:
            counts[e.memory_id].append(e)

        # Baseline: average accesses per memory over full log window
        all_entries = self._access_log_in_window(self.baseline_window_hours)
        baseline_counts: Dict[str, int] = defaultdict(int)
        for e in all_entries:
            baseline_counts[e.memory_id] += 1
        avg_baseline = sum(baseline_counts.values()) / max(len(baseline_counts), 1)

        hotspots: List[Hotspot] = []
        for mem_id, entries in counts.items():
            n = len(entries)
            baseline_count = baseline_counts.get(mem_id, avg_baseline)
            # burst = short-window density / long-window density
            rate_recent = n / max(time_window_hours, 0.01)
            rate_baseline = baseline_count / max(self.baseline_window_hours, 0.01)
            burst = rate_recent / max(rate_baseline, 0.001)

            # Average interval between accesses in the window
            timestamps = sorted(
                datetime.fromisoformat(e.timestamp) for e in entries
            )
            intervals = [
                (timestamps[i + 1] - timestamps[i]).total_seconds()
                for i in range(len(timestamps) - 1)
            ]
            avg_interval = sum(intervals) / len(intervals) if intervals else time_window_hours * 3600

            # Co-reference: memories accessed in the same minute as this one
            co_refs: List[str] = []
            if n >= 2:
                by_minute: Dict[str, List[str]] = defaultdict(list)
                for e in recent:
                    minute_key = e.timestamp[:16]  # "YYYY-MM-DDTHH:MM"
                    by_minute[minute_key].append(e.memory_id)
                for minute_ids in by_minute.values():
                    if mem_id in minute_ids:
                        co_refs.extend(
                            mid for mid in minute_ids if mid != mem_id
                        )

            # Classify pattern
            if burst >= 3.0:
                pattern = "burst"
            elif avg_interval < time_window_hours * 3600 / max(n, 1) and n >= 3:
                pattern = "cyclic"
            elif len(co_refs) >= 2:
                pattern = "co_ref"
            else:
                pattern = "cyclic"

            hotspots.append(Hotspot(
                memory_id=mem_id,
                access_count=n,
                avg_interval_seconds=avg_interval,
                burst_factor=round(burst, 2),
                co_referenced=list(set(co_refs))[:10],
                pattern=pattern,
            ))

        # Sort by burst factor descending
        hotspots.sort(key=lambda h: h.burst_factor, reverse=True)
        return hotspots

    # ── Patterns ────────────────────────────────────────────────────────

    def detect_patterns(self) -> List[UsagePattern]:
        """Detect macro usage patterns across all tracked accesses."""
        if not self._access_log:
            return []
        patterns: List[UsagePattern] = []

        # 1. Cyclic: memories accessed at regular intervals
        per_mem: Dict[str, List[datetime]] = defaultdict(list)
        for e in self._access_log:
            per_mem[e.memory_id].append(datetime.fromisoformat(e.timestamp))

        cyclic_candidates: List[str] = []
        for mem_id, times in per_mem.items():
            if len(times) < 3:
                continue
            times.sort()  # ensure chronological order
            intervals = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
            if not intervals:
                continue
            mean_interval = sum(intervals) / len(intervals)
            # Check low variance in intervals → cyclic
            variance = sum((i - mean_interval) ** 2 for i in intervals) / len(intervals)
            cv = math.sqrt(variance) / mean_interval if mean_interval > 0 else float("inf")
            if cv < 0.5 and mean_interval > 3600:  # CV < 50%, interval > 1hr
                cyclic_candidates.append(mem_id)

        if cyclic_candidates:
            patterns.append(UsagePattern(
                pattern_type="cyclic",
                memory_ids=cyclic_candidates[:20],
                confidence=round(0.7 + 0.2 * min(len(cyclic_candidates) / 5, 1), 2),
                description=f"Detected {len(cyclic_candidates)} memories with periodic access patterns",
            ))

        # 2. Burst: sudden spike in access frequency
        recent = self._access_log_in_window(24)
        older = self._access_log_in_window(self.baseline_window_hours)
        recent_ids = set(e.memory_id for e in recent)
        older_counts: Dict[str, int] = defaultdict(int)
        for e in older:
            older_counts[e.memory_id] += 1

        burst_ids: List[str] = []
        for mem_id in recent_ids:
            recent_count = sum(1 for e in recent if e.memory_id == mem_id)
            old_count = older_counts.get(mem_id, 0)
            if old_count > 0 and recent_count / old_count > 5:
                burst_ids.append(mem_id)

        if burst_ids:
            patterns.append(UsagePattern(
                pattern_type="burst",
                memory_ids=burst_ids[:20],
                confidence=round(0.6 + 0.3 * min(len(burst_ids) / 3, 1), 2),
                description=f"Detected {len(burst_ids)} burst hotspot memories",
            ))

        # 3. Co-reference: memories frequently accessed together
        co_ref_pairs: Dict[Tuple[str, str], int] = defaultdict(int)
        by_session: Dict[str, List[str]] = defaultdict(list)
        for e in self._access_log:
            key = f"{e.agent_id}_{e.timestamp[:13]}"  # hour-level grouping
            by_session[key].append(e.memory_id)
        for session_ids in by_session.values():
            unique = list(set(session_ids))
            for i in range(len(unique)):
                for j in range(i + 1, len(unique)):
                    pair = tuple(sorted([unique[i], unique[j]]))
                    co_ref_pairs[pair] += 1

        frequent_pairs = [(p, c) for p, c in co_ref_pairs.items() if c >= 3]
        co_ref_ids = list(set(
            mid for pair, _ in frequent_pairs for mid in pair
        ))
        if frequent_pairs:
            patterns.append(UsagePattern(
                pattern_type="co_ref",
                memory_ids=co_ref_ids[:20],
                confidence=round(0.5 + 0.4 * min(len(frequent_pairs) / 5, 1), 2),
                description=f"Detected {len(frequent_pairs)} co-reference pairs",
                metadata={"pairs": [list(p) for p, _ in frequent_pairs[:10]]},
            ))

        # 4. Forgetting curve: declining access frequency
        forgetting: List[str] = []
        cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
        cutoff_14d = datetime.now(timezone.utc) - timedelta(days=14)

        for mem_id, times in per_mem.items():
            if len(times) < 2:
                continue
            recent_7d = sum(1 for t in times if t > cutoff_7d)
            older_7d = sum(1 for t in times if cutoff_14d < t <= cutoff_7d)
            if recent_7d == 0 and older_7d >= 2:
                forgetting.append(mem_id)

        if forgetting:
            patterns.append(UsagePattern(
                pattern_type="forgetting",
                memory_ids=forgetting[:20],
                confidence=0.75,
                description=f"Detected {len(forgetting)} memories entering forgetting curve",
            ))

        return patterns

    # ── Heatmap ─────────────────────────────────────────────────────────

    def get_heatmap(self, hours: int = 168) -> List[Heatmap]:
        """Generate access heatmaps for the top-N most-accessed memories."""
        window = self._access_log_in_window(hours)
        if not window:
            return []

        per_mem: Dict[str, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
        now = datetime.now(timezone.utc)
        for e in window:
            dt = datetime.fromisoformat(e.timestamp)
            hour_bucket = int((now - dt).total_seconds() / 3600)
            per_mem[e.memory_id][hour_bucket] += 1

        heatmaps: List[Heatmap] = []
        for mem_id, buckets in per_mem.items():
            total = sum(buckets.values())
            peak_hour = max(buckets, key=buckets.get) if buckets else 0
            peak_count = buckets.get(peak_hour, 0)
            heatmaps.append(Heatmap(
                memory_id=mem_id,
                hourly_buckets=dict(sorted(buckets.items())),
                total_accesses=total,
                peak_hour=peak_hour,
                peak_count=peak_count,
            ))

        heatmaps.sort(key=lambda h: h.total_accesses, reverse=True)
        return heatmaps[:50]

    # ── Prediction ──────────────────────────────────────────────────────

    def predict_next_access(self, memory_id: str) -> Optional[str]:
        """Predict the next likely access time for a memory."""
        times = [
            datetime.fromisoformat(e.timestamp)
            for e in self._access_log
            if e.memory_id == memory_id
        ]
        if len(times) < 2:
            return None

        times.sort()
        intervals = [(times[i + 1] - times[i]).total_seconds() for i in range(len(times) - 1)]
        avg_interval = sum(intervals) / len(intervals)
        predicted = times[-1] + timedelta(seconds=avg_interval)
        return predicted.isoformat()
