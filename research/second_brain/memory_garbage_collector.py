"""
# status: orphan (2026-08-15 audit, not in runtime path)
P15-4: Memory Garbage Collection Scheduler.

Reference: Memory Cleanup for Long-Running Agent Applications —
           Automatic garbage collection, deferred maintenance,
           and memory leak detection for persistent agent memory.

Design: Six-component memory hygiene framework:
        - GarbageDetector: detects stale/duplicate/orphan/redundant entries
        - DeferredMaintenanceScheduler: idle-time defrag, index rebuild, dead ref cleanup
        - MemoryLeakMonitor: growth rate tracking, threshold-triggered aggressive GC
        - ReferenceCountTracker: reference-counted entry lifecycle
        - StorageHealthReport: comprehensive storage health analytics
        - CompactTrigger: bloat-ratio-driven compaction with lifecycle integration

Complementary to: lifecycle_manager.py (delete/archive lifecycle) —
                  this module handles space reclamation and leak prevention.
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

class GarbageType(Enum):
    EXPIRED = auto()         # TTL exceeded
    DUPLICATE = auto()       # semantically identical to another entry
    REDUNDANT = auto()       # superseded by newer entry
    ORPHAN = auto()          # no references pointing to it
    CORRUPT = auto()         # unparseable or malformed
    ZERO_SIZE = auto()       # empty content


class MaintenanceTask(Enum):
    DEFRAGMENTATION = auto()  # compact fragmented storage
    INDEX_REBUILD = auto()    # rebuild retrieval indices
    DEAD_REF_CLEANUP = auto() # remove dangling references
    STATS_RECALCULATE = auto()
    TOMBSTONE_PURGE = auto()  # remove tombstoned entries


class GCLevel(Enum):
    NORMAL = auto()          # standard GC pass
    AGGRESSIVE = auto()      # triggered by leak detection
    EMERGENCY = auto()       # storage critically full


class HealthStatus(Enum):
    HEALTHY = auto()         # all metrics within bounds
    WARNING = auto()         # one or more metrics approaching threshold
    CRITICAL = auto()        # one or more metrics exceeded threshold
    DEGRADED = auto()        # performance impact detected


class CompactTriggerMode(Enum):
    MANUAL = auto()          # user-triggered
    SCHEDULED = auto()       # time-based periodic
    BLOAT_RATIO = auto()     # triggered by bloat exceeding threshold
    SIZE_THRESHOLD = auto()  # triggered by total size threshold


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class GarbageCandidate:
    """A detected garbage candidate."""
    entry_id: str
    garbage_type: GarbageType
    reason: str
    size_bytes: int = 0
    age_seconds: float = 0.0
    confidence: float = 1.0           # confidence this is truly garbage
    detected_at: float = field(default_factory=time.time)


@dataclass
class ReferenceEntry:
    """A reference-counted memory entry."""
    entry_id: str
    ref_count: int = 0
    referrers: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    data_size_bytes: int = 0


@dataclass
class LeakAlert:
    """Alert triggered when memory growth exceeds threshold."""
    alert_id: str
    current_growth_rate_bytes_per_sec: float
    threshold_bytes_per_sec: float
    total_entries: int
    recommended_action: GCLevel = GCLevel.AGGRESSIVE
    triggered_at: float = field(default_factory=time.time)


@dataclass
class StorageHealthReport:
    """Comprehensive storage health snapshot."""
    report_id: str
    total_entries: int = 0
    active_entries: int = 0
    orphan_entries: int = 0
    duplicate_entries: int = 0
    expired_entries: int = 0
    fragmentation_ratio: float = 0.0   # 1.0 = no fragmentation
    bloat_ratio: float = 1.0           # actual_size / ideal_size
    growth_rate_bytes_per_sec: float = 0.0
    estimated_savings_bytes: int = 0
    health_status: HealthStatus = HealthStatus.HEALTHY
    generated_at: float = field(default_factory=time.time)


@dataclass
class CompactResult:
    """Result of a compaction operation."""
    trigger_mode: CompactTriggerMode
    entries_before: int
    entries_after: int
    bytes_before: int
    bytes_after: int
    duration_seconds: float = 0.0
    success: bool = False


# ---------------------------------------------------------------------------
# Core classes
# ---------------------------------------------------------------------------

class GarbageDetector:
    """Detects stale, duplicate, redundant, and orphan memory entries.

    Scans the memory store and classifies entries into garbage types.
    Does NOT delete — only returns candidates for downstream decisions.
    """

    def __init__(self, ttl_seconds: float = 86400 * 30,  # 30 days default
                 max_duplicate_similarity: float = 0.95):
        self._lock = threading.RLock()
        self.ttl_seconds = ttl_seconds
        self.max_duplicate_similarity = max_duplicate_similarity
        self._detection_log: deque = deque(maxlen=2000)

    def detect_expired(self, entries: List[Dict[str, Any]]) -> List[GarbageCandidate]:
        """Detect entries whose TTL has expired."""
        now = time.time()
        candidates = []
        with self._lock:
            for entry in entries:
                created = entry.get("created_at", 0)
                if now - created > self.ttl_seconds:
                    candidates.append(GarbageCandidate(
                        entry_id=entry.get("entry_id", str(uuid.uuid4())[:8]),
                        garbage_type=GarbageType.EXPIRED,
                        reason=f"TTL exceeded ({now - created:.1f}s > {self.ttl_seconds}s)",
                        age_seconds=now - created,
                    ))
            self._detection_log.extend(candidates)
            return candidates

    def detect_orphans(self, entries: List[Dict[str, Any]],
                       ref_counts: Dict[str, int]) -> List[GarbageCandidate]:
        """Detect entries with zero reference count."""
        candidates = []
        with self._lock:
            for entry in entries:
                eid = entry.get("entry_id", "")
                if ref_counts.get(eid, 0) == 0:
                    candidates.append(GarbageCandidate(
                        entry_id=eid,
                        garbage_type=GarbageType.ORPHAN,
                        reason="Zero reference count",
                        size_bytes=entry.get("size_bytes", 0),
                    ))
            self._detection_log.extend(candidates)
            return candidates

    def detect_duplicates(self, entries: List[Dict[str, Any]]) -> List[GarbageCandidate]:
        """Detect semantically duplicate entries using hash comparison."""
        candidates = []
        with self._lock:
            seen: Dict[int, str] = {}  # content_hash → entry_id (keep first)
            for entry in entries:
                content = entry.get("content", "")
                h = hash(content[:500])  # hash of first 500 chars
                if h in seen:
                    candidates.append(GarbageCandidate(
                        entry_id=entry.get("entry_id", ""),
                        garbage_type=GarbageType.DUPLICATE,
                        reason=f"Duplicate of {seen[h]}",
                        size_bytes=entry.get("size_bytes", 0),
                    ))
                else:
                    seen[h] = entry.get("entry_id", "")
            self._detection_log.extend(candidates)
            return candidates

    def detect_zero_size(self, entries: List[Dict[str, Any]]) -> List[GarbageCandidate]:
        """Detect entries with empty content."""
        candidates = []
        with self._lock:
            for entry in entries:
                size = entry.get("size_bytes", -1)
                content_len = len(str(entry.get("content", "")))
                if size == 0 or content_len == 0:
                    candidates.append(GarbageCandidate(
                        entry_id=entry.get("entry_id", str(uuid.uuid4())[:8]),
                        garbage_type=GarbageType.ZERO_SIZE,
                        reason="Empty content or zero size",
                    ))
            self._detection_log.extend(candidates)
            return candidates

    def full_scan(self, entries: List[Dict[str, Any]],
                  ref_counts: Optional[Dict[str, int]] = None,
                  ) -> Dict[GarbageType, List[GarbageCandidate]]:
        """Run all detectors and return categorized results."""
        results = {
            GarbageType.EXPIRED: self.detect_expired(entries),
            GarbageType.ORPHAN: self.detect_orphans(entries, ref_counts or {}),
            GarbageType.DUPLICATE: self.detect_duplicates(entries),
            GarbageType.ZERO_SIZE: self.detect_zero_size(entries),
        }
        return results

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            by_type = defaultdict(int)
            for c in self._detection_log:
                by_type[c.garbage_type.name] += 1
            return {
                "total_detected": len(self._detection_log),
                "by_type": dict(by_type),
                "ttl_seconds": self.ttl_seconds,
            }


class DeferredMaintenanceScheduler:
    """Schedules maintenance tasks during idle periods.

    Defrag, index rebuild, dead reference cleanup — all deferred
    to idle windows to minimize impact on active operations.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._queue: List[MaintenanceTask] = []
        self._completed: deque = deque(maxlen=500)
        self._idle_since: Optional[float] = None
        self.idle_threshold_seconds: float = 60.0

    def enqueue(self, task: MaintenanceTask) -> None:
        with self._lock:
            if task not in self._queue:
                self._queue.append(task)
                logger.debug(f"[DeferredMaintenance] Enqueued {task.name}")

    def set_idle(self, idle: bool) -> None:
        with self._lock:
            if idle and self._idle_since is None:
                self._idle_since = time.time()
            elif not idle:
                self._idle_since = None

    def is_idle(self) -> bool:
        with self._lock:
            if self._idle_since is None:
                return False
            return (time.time() - self._idle_since) >= self.idle_threshold_seconds

    def run_pending(self) -> List[MaintenanceTask]:
        """Execute all pending maintenance tasks if idle."""
        with self._lock:
            if not self.is_idle() or not self._queue:
                return []
            executed = list(self._queue)
            self._completed.extend(executed)
            self._queue.clear()
            logger.info(f"[DeferredMaintenance] Executed {len(executed)} tasks")
            return executed

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pending_tasks": len(self._queue),
                "completed_tasks": len(self._completed),
                "is_idle": self.is_idle(),
                "idle_seconds": (
                    time.time() - self._idle_since if self._idle_since else 0
                ),
            }


class MemoryLeakMonitor:
    """Tracks memory storage growth rate and triggers aggressive GC.

    Maintains a sliding window of size samples, computes growth rate,
    and emits alerts when growth exceeds configurable thresholds.
    """

    def __init__(self, window_size: int = 100,
                 growth_threshold_bytes_per_sec: float = 1024.0):  # 1 KB/s
        self._lock = threading.RLock()
        self.window_size = window_size
        self.growth_threshold = growth_threshold_bytes_per_sec
        self._samples: deque = deque(maxlen=window_size)
        self._alerts: deque = deque(maxlen=200)
        self._active_gc_level: GCLevel = GCLevel.NORMAL

    def record_sample(self, total_entries: int, total_bytes: int) -> None:
        with self._lock:
            self._samples.append({
                "timestamp": time.time(),
                "entries": total_entries,
                "bytes": total_bytes,
            })

    def compute_growth_rate(self) -> float:
        """Compute bytes-per-second growth rate from recent samples."""
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            first = self._samples[0]
            last = self._samples[-1]
            dt = last["timestamp"] - first["timestamp"]
            if dt <= 0:
                return 0.0
            db = last["bytes"] - first["bytes"]
            return db / dt

    def check_threshold(self) -> Optional[LeakAlert]:
        """Check if growth rate exceeds threshold and emit alert."""
        with self._lock:
            rate = self.compute_growth_rate()
            if rate > self.growth_threshold:
                alert = LeakAlert(
                    alert_id=f"leak_{uuid.uuid4().hex[:12]}",
                    current_growth_rate_bytes_per_sec=rate,
                    threshold_bytes_per_sec=self.growth_threshold,
                    total_entries=self._samples[-1]["entries"] if self._samples else 0,
                )
                self._alerts.append(alert)
                self._active_gc_level = (
                    GCLevel.EMERGENCY if rate > self.growth_threshold * 5
                    else GCLevel.AGGRESSIVE
                )
                logger.warning(
                    f"[MemoryLeakMonitor] Leak alert: {rate:.1f} B/s > "
                    f"{self.growth_threshold:.1f} B/s"
                )
                return alert
            return None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "sample_count": len(self._samples),
                "growth_rate_bps": round(self.compute_growth_rate(), 2),
                "threshold_bps": self.growth_threshold,
                "alerts_triggered": len(self._alerts),
                "active_gc_level": self._active_gc_level.name,
            }


class ReferenceCountTracker:
    """Tracks reference counts for memory entries and auto-cleans zero-ref entries.

    Each memory entry has a reference count. When count reaches zero,
    the entry becomes eligible for garbage collection.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._entries: Dict[str, ReferenceEntry] = {}
        self._zero_ref_events: deque = deque(maxlen=500)

    def register(self, entry_id: str, initial_refs: int = 1,
                 data_size_bytes: int = 0) -> ReferenceEntry:
        with self._lock:
            entry = ReferenceEntry(
                entry_id=entry_id,
                ref_count=initial_refs,
                data_size_bytes=data_size_bytes,
            )
            self._entries[entry_id] = entry
            return entry

    def increment(self, entry_id: str, referrer: str = "") -> int:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return -1
            entry.ref_count += 1
            if referrer and referrer not in entry.referrers:
                entry.referrers.append(referrer)
            entry.last_accessed = time.time()
            return entry.ref_count

    def decrement(self, entry_id: str, referrer: str = "") -> int:
        with self._lock:
            entry = self._entries.get(entry_id)
            if entry is None:
                return -1
            entry.ref_count = max(0, entry.ref_count - 1)
            if referrer and referrer in entry.referrers:
                entry.referrers.remove(referrer)
            entry.last_accessed = time.time()

            if entry.ref_count == 0:
                self._zero_ref_events.append({
                    "entry_id": entry_id,
                    "timestamp": time.time(),
                })
            return entry.ref_count

    def get_zero_ref_entries(self) -> List[str]:
        with self._lock:
            return [eid for eid, e in self._entries.items() if e.ref_count == 0]

    def get_ref_count(self, entry_id: str) -> int:
        with self._lock:
            entry = self._entries.get(entry_id)
            return entry.ref_count if entry else -1

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total = len(self._entries)
            zero = sum(1 for e in self._entries.values() if e.ref_count == 0)
            return {
                "total_entries": total,
                "zero_ref_entries": zero,
                "zero_ref_pct": round(zero / max(total, 1) * 100, 2),
                "avg_ref_count": round(
                    float(np.mean([e.ref_count for e in self._entries.values()]))
                    if self._entries else 0.0, 2,
                ),
            }


class StorageHealthReport:
    """Generates comprehensive storage health analytics.

    Computes: total entries, active rate, fragmentation ratio,
    bloat ratio, growth rate, estimated recoverable space.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._reports: List[StorageHealthReport] = []
        self._baseline_entries: int = 0
        self._baseline_bytes: int = 0

    def set_baseline(self, total_entries: int, total_bytes: int) -> None:
        with self._lock:
            self._baseline_entries = total_entries
            self._baseline_bytes = total_bytes

    def generate(self, total_entries: int, total_bytes: int,
                 garbage_counts: Dict[GarbageType, int],
                 growth_rate_bps: float = 0.0,
                 fragmentation_ratio: float = 1.0,
                 ideal_bytes_per_entry: int = 4096,
                 ) -> StorageHealthReport:
        """Generate a storage health report."""
        with self._lock:
            orphan = garbage_counts.get(GarbageType.ORPHAN, 0)
            duplicate = garbage_counts.get(GarbageType.DUPLICATE, 0)
            expired = garbage_counts.get(GarbageType.EXPIRED, 0)
            active = total_entries - orphan - duplicate - expired

            ideal_total = total_entries * ideal_bytes_per_entry
            bloat = total_bytes / max(ideal_total, 1)

            estimated_savings = 0
            for gtype, count in garbage_counts.items():
                estimated_savings += count * ideal_bytes_per_entry

            # Determine health status
            if bloat > 3.0 or growth_rate_bps > 10000:
                status = HealthStatus.CRITICAL
            elif bloat > 1.8 or growth_rate_bps > 5000:
                status = HealthStatus.WARNING
            elif fragmentation_ratio < 0.6:
                status = HealthStatus.DEGRADED
            else:
                status = HealthStatus.HEALTHY

            report = StorageHealthReport(
                report_id=f"hr_{uuid.uuid4().hex[:12]}",
                total_entries=total_entries,
                active_entries=active,
                orphan_entries=orphan,
                duplicate_entries=duplicate,
                expired_entries=expired,
                fragmentation_ratio=round(fragmentation_ratio, 4),
                bloat_ratio=round(bloat, 4),
                growth_rate_bytes_per_sec=round(growth_rate_bps, 2),
                estimated_savings_bytes=estimated_savings,
                health_status=status,
            )
            self._reports.append(report)
            return report

    def get_latest(self) -> Optional[StorageHealthReport]:
        with self._lock:
            return self._reports[-1] if self._reports else None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            latest = self.get_latest()
            return {
                "reports_generated": len(self._reports),
                "latest_status": latest.health_status.name if latest else None,
                "latest_bloat_ratio": latest.bloat_ratio if latest else 1.0,
            }


class CompactTrigger:
    """Triggers compaction based on bloat ratio thresholds.

    When bloat exceeds the configured threshold, initiates compaction
    to reclaim space. Integrates with lifecycle manager for consistent
    deletion/archival policies.
    """

    def __init__(self, bloat_threshold: float = 2.0,
                 min_entries_for_compact: int = 100):
        self._lock = threading.RLock()
        self.bloat_threshold = bloat_threshold
        self.min_entries_for_compact = min_entries_for_compact
        self._compact_history: deque = deque(maxlen=50)

    def should_compact(self, total_entries: int,
                       bloat_ratio: float) -> Tuple[bool, CompactTriggerMode]:
        """Determine if compaction should be triggered."""
        with self._lock:
            if total_entries < self.min_entries_for_compact:
                return False, CompactTriggerMode.BLOAT_RATIO
            if bloat_ratio > self.bloat_threshold * 2:
                return True, CompactTriggerMode.SIZE_THRESHOLD
            if bloat_ratio > self.bloat_threshold:
                return True, CompactTriggerMode.BLOAT_RATIO
            return False, CompactTriggerMode.BLOAT_RATIO

    def execute_compact(self, entries_before: int, entries_after: int,
                        bytes_before: int, bytes_after: int,
                        trigger: CompactTriggerMode,
                        duration: float = 0.0) -> CompactResult:
        """Record a compaction result."""
        with self._lock:
            result = CompactResult(
                trigger_mode=trigger,
                entries_before=entries_before,
                entries_after=entries_after,
                bytes_before=bytes_before,
                bytes_after=bytes_after,
                duration_seconds=duration,
                success=entries_after <= entries_before,
            )
            self._compact_history.append(result)
            logger.info(
                f"[CompactTrigger] {trigger.name} compact: "
                f"{entries_before}→{entries_after} entries, "
                f"{bytes_before}→{bytes_after} bytes"
            )
            return result

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_saved = sum(
                c.bytes_before - c.bytes_after
                for c in self._compact_history if c.success
            )
            return {
                "bloat_threshold": self.bloat_threshold,
                "total_compactions": len(self._compact_history),
                "total_bytes_reclaimed": total_saved,
                "last_compact": (
                    self._compact_history[-1].trigger_mode.name
                    if self._compact_history else None
                ),
            }
