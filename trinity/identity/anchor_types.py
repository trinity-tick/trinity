"""
Trinity Identity — Anchor Types (Dataclasses)
===============================================
Multi-Anchor Identity data models for arXiv 2604.09588-style
decentralized agent identity persistence.

Author: Trinity Team
Version: v1.0.0
"""

from dataclasses import dataclass, field, asdict
import uuid as _uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
import json


@dataclass
class IdentityAnchor:
    """A single identity anchor — one pillar of an agent's identity.

    Attributes:
        id: UUID v4 unique identifier for this anchor.
        agent_id: The agent this anchor belongs to.
        anchor_type: One of 'identity_files', 'procedural_patterns',
                     'episodic_keys', 'value_specifications'.
        content: JSON-serializable content of the anchor.
        version: Monotonic version number for this anchor.
        checksum: SHA-256 hash of content for integrity verification.
        created_at: ISO 8601 creation timestamp.
        updated_at: ISO 8601 last update timestamp.
    """
    id: str
    agent_id: str
    anchor_type: str
    content: Dict[str, Any] = field(default_factory=dict)
    version: int = 1
    checksum: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IdentityAnchor":
        content = data.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content) if content.strip() else {}
            except json.JSONDecodeError:
                content = {"_raw": content}
        elif not isinstance(content, dict):
            content = {}
        return cls(
            id=data.get("id", ""),
            agent_id=data.get("agent_id", ""),
            anchor_type=data.get("anchor_type", ""),
            content=content,
            version=int(data.get("version", 1)),
            checksum=data.get("checksum", ""),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )


@dataclass
class IdentityProfile:
    """Complete identity profile reconstructed from all anchors.

    Attributes:
        agent_id: The agent this profile belongs to.
        anchors: List of all identity anchors.
        consistency_score: 0.0–1.0 score indicating anchor coherence.
        anchor_counts: Per-type count of anchors.
        drift_flags: List of detected drift warnings.
        last_reconstructed_at: ISO 8601 timestamp of last reconstruction.
    """
    agent_id: str
    anchors: List[IdentityAnchor] = field(default_factory=list)
    consistency_score: float = 0.0
    anchor_counts: Dict[str, int] = field(default_factory=dict)
    drift_flags: List[str] = field(default_factory=list)
    last_reconstructed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "anchors": [a.to_dict() for a in self.anchors],
            "consistency_score": self.consistency_score,
            "anchor_counts": self.anchor_counts,
            "drift_flags": self.drift_flags,
            "last_reconstructed_at": self.last_reconstructed_at,
        }


@dataclass
class TemporalAnchor:
    """Temporal identity anchor — time-based behavioral patterns.

    Captures an agent's temporal rhythm: when it is active, how long
    sessions last, and what timezone it operates in.  Used by the
    ``_detect_temporal_drift()`` method in IdentityManager to flag
    anomalous access patterns.

    Attributes:
        anchor_id: UUID v4 unique identifier for this anchor.
        agent_id: The agent this temporal anchor belongs to.
        temporal_pattern: 'daily' / 'weekly' / 'irregular'.
        active_windows: List of (start_hour, end_hour) tuples in local time.
        last_seen: ISO 8601 timestamp of last activity.
        session_duration_avg: Average session duration in seconds.
        timezone: IANA timezone string, e.g. 'Asia/Shanghai'.
        created_at: ISO 8601 creation timestamp.
        updated_at: ISO 8601 last update timestamp.
    """
    anchor_id: str = ""
    agent_id: str = ""
    temporal_pattern: str = "irregular"   # daily / weekly / irregular
    active_windows: List[Dict[str, int]] = field(default_factory=list)
    last_seen: str = ""
    session_duration_avg: float = 0.0
    timezone: str = "UTC"
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TemporalAnchor":
        return cls(
            anchor_id=data.get("anchor_id", ""),
            agent_id=data.get("agent_id", ""),
            temporal_pattern=data.get("temporal_pattern", "irregular"),
            active_windows=data.get("active_windows", []),
            last_seen=data.get("last_seen", ""),
            session_duration_avg=float(data.get("session_duration_avg", 0.0)),
            timezone=data.get("timezone", "UTC"),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    @classmethod
    def from_access_log(
        cls,
        agent_id: str,
        access_times: List[str],
        iana_tz: str = "UTC",
    ) -> "TemporalAnchor":
        """Auto-extract temporal patterns from a list of ISO 8601 access timestamps.

        Parameters
        ----------
        agent_id: Target agent identifier.
        access_times: List of ISO 8601 datetime strings.
        timezone: IANA timezone, default 'UTC'.

        Returns
        -------
        TemporalAnchor with extracted active_windows and pattern classification.
        """
        from collections import Counter

        if not access_times:
            return cls(agent_id=agent_id, timezone=timezone)

        # Parse hours
        hours = []
        for ts in access_times:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                hours.append(dt.hour)
            except (ValueError, AttributeError):
                continue

        if not hours:
            return cls(agent_id=agent_id, timezone=timezone)

        # Build hour histogram
        hour_counter = Counter(hours)
        total = len(hours)
        threshold = max(1, int(total * 0.05))  # 5% threshold

        # Find contiguous active windows
        active_hours = sorted(h for h, cnt in hour_counter.items() if cnt >= threshold)
        if not active_hours:
            # Fallback: 25th-75th percentile
            sorted_hours = sorted(hours)
            p25 = sorted_hours[int(len(sorted_hours) * 0.25)]
            p75 = sorted_hours[int(len(sorted_hours) * 0.75)]
            active_hours = list(range(p25, p75 + 1))

        # Build windows by clustering contiguous hours
        windows = []
        if active_hours:
            start = active_hours[0]
            prev = active_hours[0]
            for h in active_hours[1:] + [None]:
                if h is None or h > prev + 2:  # gap > 2h → new window
                    windows.append({"start_hour": start, "end_hour": min(prev + 1, 24)})
                    if h is not None:
                        start = h
                prev = h if h is not None else prev

        # Classify pattern
        if len(windows) <= 2 and all(w["end_hour"] - w["start_hour"] <= 4 for w in windows):
            pattern = "daily"
        elif len(windows) <= 5:
            pattern = "weekly"
        else:
            pattern = "irregular"

        # Compute average session duration (simple heuristic: gap between consecutive)
        dts = []
        for ts in access_times:
            try:
                dts.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
            except (ValueError, AttributeError):
                continue
        dts.sort()
        gaps = []
        for i in range(1, len(dts)):
            gap = (dts[i] - dts[i - 1]).total_seconds()
            if gap < 3600 * 4:  # same session if < 4h gap
                gaps.append(gap)
        avg_duration = sum(gaps) / len(gaps) if gaps else 0.0

        now = datetime.now(timezone.utc).isoformat()
        return cls(
            anchor_id=str(_uuid.uuid4()),
            agent_id=agent_id,
            temporal_pattern=pattern,
            active_windows=windows,
            last_seen=dts[-1].isoformat() if dts else "",
            session_duration_avg=round(avg_duration, 1),
            timezone=iana_tz,
            created_at=now,
            updated_at=now,
        )


@dataclass
class IdentityBundle:
    """Exportable identity bundle for agent migration/backup.

    Attributes:
        agent_id: Source agent.
        exported_at: ISO 8601 export timestamp.
        version: Bundle format version.
        anchors: All anchors as dicts.
        checksum: SHA-256 hash of the entire bundle.
    """
    agent_id: str
    exported_at: str
    version: str = "1.0"
    anchors: List[Dict[str, Any]] = field(default_factory=list)
    checksum: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "IdentityBundle":
        return cls(
            agent_id=data.get("agent_id", ""),
            exported_at=data.get("exported_at", ""),
            version=data.get("version", "1.0"),
            anchors=data.get("anchors", []),
            checksum=data.get("checksum", ""),
        )
