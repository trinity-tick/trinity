"""Feedback Collector — agent feedback aggregation and quality analysis.

Collects explicit and implicit feedback on memory quality, aggregates
ratings, and detects quality issues.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from collections import defaultdict


# ═══════════════════════════════════════════════════════════════════════════
# Data Structures
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class FeedbackEntry:
    """Single feedback record."""
    feedback_id: str
    memory_id: str
    agent_id: str
    rating: float           # 1.0–5.0
    comment: str = ""
    context: str = ""       # query or trigger
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class FeedbackAggregate:
    """Aggregated feedback for a single memory."""
    memory_id: str
    avg_rating: float
    rating_count: int
    rating_distribution: Dict[int, int]  # rating → count
    recent_trend: float                  # last 3 vs overall delta
    top_comments: List[str]


@dataclass
class QualityIssue:
    """Detected memory quality problem."""
    memory_id: str
    issue_type: str         # low_quality | outdated | contradictory | fragmented
    severity: float         # 0.0–1.0
    description: str
    suggestion: str


@dataclass
class QualityTrend:
    """Memory quality trend over time."""
    overall_avg: float
    daily_averages: Dict[str, float]  # YYYY-MM-DD → avg rating
    improving: bool
    declining: bool
    stale_count: int
    total_rated: int


# ═══════════════════════════════════════════════════════════════════════════
# Collector
# ═══════════════════════════════════════════════════════════════════════════

class FeedbackCollector:
    """Collect and analyse agent feedback on memory quality."""

    def __init__(self):
        self._feedback: List[FeedbackEntry] = []
        self._counter = 0

    # ── Recording ───────────────────────────────────────────────────────

    def record_feedback(
        self,
        memory_id: str,
        agent_id: str,
        rating: float,
        comment: str = "",
        context: str = "",
    ) -> FeedbackEntry:
        """Submit a feedback rating for a memory."""
        self._counter += 1
        entry = FeedbackEntry(
            feedback_id=f"fb_{self._counter}_{int(datetime.now(timezone.utc).timestamp())}",
            memory_id=memory_id,
            agent_id=agent_id,
            rating=max(1.0, min(5.0, rating)),
            comment=comment,
            context=context,
        )
        self._feedback.append(entry)
        return entry

    # ── Aggregation ─────────────────────────────────────────────────────

    def aggregate_feedback(self, memory_id: str) -> FeedbackAggregate:
        """Aggregate all feedback for a memory into summary statistics."""
        entries = [e for e in self._feedback if e.memory_id == memory_id]
        if not entries:
            return FeedbackAggregate(
                memory_id=memory_id,
                avg_rating=0.0,
                rating_count=0,
                rating_distribution={},
                recent_trend=0.0,
                top_comments=[],
            )

        avg = sum(e.rating for e in entries) / len(entries)
        distribution: Dict[int, int] = defaultdict(int)
        for e in entries:
            distribution[int(e.rating)] += 1

        # Recent trend: last 3 vs overall
        sorted_entries = sorted(entries, key=lambda e: e.timestamp)
        recent = sorted_entries[-3:] if len(sorted_entries) >= 3 else sorted_entries
        recent_avg = sum(e.rating for e in recent) / len(recent) if recent else avg
        trend = round(recent_avg - avg, 2)

        # Top comments (non-empty, most recent)
        comments = [e.comment for e in entries if e.comment.strip()]
        comments.reverse()

        return FeedbackAggregate(
            memory_id=memory_id,
            avg_rating=round(avg, 2),
            rating_count=len(entries),
            rating_distribution=dict(sorted(distribution.items())),
            recent_trend=trend,
            top_comments=comments[:5],
        )

    # ── Quality Detection ───────────────────────────────────────────────

    def detect_quality_issues(self) -> List[QualityIssue]:
        """Scan feedback and metadata to detect quality problems."""
        issues: List[QualityIssue] = []
        per_mem: Dict[str, List[FeedbackEntry]] = defaultdict(list)
        for e in self._feedback:
            per_mem[e.memory_id].append(e)

        for mem_id, entries in per_mem.items():
            avg = sum(e.rating for e in entries) / len(entries)
            recent_3 = sorted(entries, key=lambda e: e.timestamp)[-3:]
            recent_avg = sum(e.rating for e in recent_3) / len(recent_3) if recent_3 else avg

            # 1. Low quality: consistently low ratings
            if avg < 2.5 and len(entries) >= 2:
                issues.append(QualityIssue(
                    memory_id=mem_id,
                    issue_type="low_quality",
                    severity=round((2.5 - avg) / 2.5, 2),
                    description=f"Avg rating {avg:.1f} over {len(entries)} feedbacks",
                    suggestion="Consider pruning or deprioritising this memory",
                ))

            # 2. Outdated: declining trend
            if recent_avg < avg - 0.5 and len(entries) >= 3:
                issues.append(QualityIssue(
                    memory_id=mem_id,
                    issue_type="outdated",
                    severity=round(min((avg - recent_avg) / 2, 1.0), 2),
                    description=f"Rating declining: {avg:.1f} → {recent_avg:.1f}",
                    suggestion="Memory may contain outdated information; consider refreshing",
                ))

            # 3. Fragmented: many low-rating entries suggest split needed
            if len(entries) >= 4 and avg < 3.5 and recent_avg < 3.5:
                issues.append(QualityIssue(
                    memory_id=mem_id,
                    issue_type="fragmented",
                    severity=round((3.5 - min(avg, recent_avg)) / 3.5, 2),
                    description=f"Consistent mediocre ratings suggest over-fragmentation",
                    suggestion="Consider merging with related memories or splitting",
                ))

        return sorted(issues, key=lambda i: i.severity, reverse=True)

    # ── Quality Trend ───────────────────────────────────────────────────

    def get_quality_trend(self, days: int = 30) -> QualityTrend:
        """Analyse overall quality trend."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        in_window = [
            e for e in self._feedback
            if datetime.fromisoformat(e.timestamp) > cutoff
        ]
        if not in_window:
            return QualityTrend(
                overall_avg=0.0,
                daily_averages={},
                improving=False,
                declining=False,
                stale_count=0,
                total_rated=0,
            )

        overall_avg = sum(e.rating for e in in_window) / len(in_window)

        # Daily averages
        daily: Dict[str, List[float]] = defaultdict(list)
        for e in in_window:
            day = e.timestamp[:10]  # YYYY-MM-DD
            daily[day].append(e.rating)

        daily_avgs = {day: round(sum(rs) / len(rs), 2) for day, rs in sorted(daily.items())}

        # Trend direction
        days_sorted = sorted(daily.keys())
        if len(days_sorted) >= 3:
            first_half = days_sorted[:len(days_sorted) // 2]
            second_half = days_sorted[len(days_sorted) // 2:]
            first_avg = sum(daily_avgs[d] for d in first_half) / len(first_half)
            second_avg = sum(daily_avgs[d] for d in second_half) / len(second_half)
            improving = second_avg > first_avg + 0.2
            declining = first_avg > second_avg + 0.2
        else:
            improving = False
            declining = False

        return QualityTrend(
            overall_avg=round(overall_avg, 2),
            daily_averages=daily_avgs,
            improving=improving,
            declining=declining,
            stale_count=sum(1 for e in in_window if e.rating < 2.5),
            total_rated=len(in_window),
        )
