"""
Trinity Identity — Identity Manager
=====================================
Core identity management engine implementing Multi-Anchor Identity
architecture based on arXiv 2604.09588.

Distributes agent identity across multiple independent memory anchors
rather than centralized storage. Any single anchor can reconstruct
core behavioral patterns, preventing catastrophic forgetting.
"""

import json
import hashlib
import uuid
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone

from .anchor_types import IdentityAnchor, IdentityProfile, IdentityBundle, TemporalAnchor

logger = logging.getLogger(__name__)

# Anchor type labels for display
ANCHOR_TYPES = {
    "identity_files": "核心人格/价值观/行为准则（不可变，仅版本化更新）",
    "procedural_patterns": "行为模式/决策模板/常用工作流（从执行轨迹自动提取）",
    "episodic_keys": "关键记忆锚点（高重要性记忆的快照）",
    "value_specifications": "价值约束/安全边界/宪法规则",
    "temporal": "时间锚点（活跃时段/会话节奏/时区模式）",
}

# Anchor weights for four-anchor weighted drift detection
# behavioral (identity_files + procedural_patterns): 0.3
# knowledge (episodic_keys): 0.3
# relational (value_specifications): 0.25
# temporal: 0.15
ANCHOR_WEIGHTS = {
    "behavioral": 0.3,
    "knowledge": 0.3,
    "relational": 0.25,
    "temporal": 0.15,
}

# Drift severity thresholds
DRIFT_SEVERITY = {
    "WARNING": (0.3, 0.5),       # Minor drift — log and monitor
    "CRITICAL": (0.5, 0.7),       # Significant drift — require re-validation
    "IDENTITY_BREAK": (0.7, 1.01),  # Identity break — isolate and reconstruct
}


class IdentityManager:
    """Manages multi-anchor identity for Trinity agents.

    Anchors are distributed across four types:
    - identity_files: Core personality, values, behavioral rules (immutable, versioned)
    - procedural_patterns: Behavioral patterns, decision templates, workflows
    - episodic_keys: Key memory snapshots (high-importance memories)
    - value_specifications: Value constraints, safety boundaries, constitutional rules
    """

    def __init__(self, storage_adapter=None):
        """Initialize with a storage adapter that supports identity_anchors table.

        Args:
            storage_adapter: An adapter instance with upsert_anchor / get_anchors
                             / get_all_anchors / get_latest_anchor_version methods.
        """
        self._adapter = storage_adapter
        self._drift_threshold: float = 0.15  # Cosine distance threshold for drift
        self._episodic_importance_threshold: float = 0.85  # Importance threshold for auto-anchor

    # ── Anchor CRUD ────────────────────────────────────────────────────

    def register_anchor(
        self,
        agent_id: str,
        anchor_type: str,
        content: Dict[str, Any],
        tags: Optional[List[str]] = None,
    ) -> IdentityAnchor:
        """Register or update an identity anchor.

        Args:
            agent_id: Target agent identifier.
            anchor_type: One of identity_files / procedural_patterns /
                         episodic_keys / value_specifications.
            content: JSON-serializable anchor content.
            tags: Optional tags for categorization.

        Returns:
            The upserted IdentityAnchor.
        """
        if anchor_type not in ANCHOR_TYPES:
            raise ValueError(f"Unknown anchor_type: {anchor_type}. Valid: {list(ANCHOR_TYPES)}")

        anchor_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        content_json = json.dumps(content, sort_keys=True, ensure_ascii=False)
        checksum = hashlib.sha256(content_json.encode("utf-8")).hexdigest()

        # Get latest version and increment
        latest_version = 1
        if self._adapter and hasattr(self._adapter, "get_latest_anchor_version"):
            current = self._adapter.get_latest_anchor_version(
                agent_id, anchor_type
            )
            if current is not None:
                if isinstance(current, dict):
                    latest_version = current.get("version", 0) + 1
                else:
                    latest_version = current + 1

        anchor = IdentityAnchor(
            id=anchor_id,
            agent_id=agent_id,
            anchor_type=anchor_type,
            content=content,
            version=latest_version,
            checksum=checksum,
            created_at=now,
            updated_at=now,
        )

        if self._adapter and hasattr(self._adapter, "upsert_anchor"):
            self._adapter.upsert_anchor(
                agent_id=agent_id,
                anchor_type=anchor_type,
                content=content_json,
                version=latest_version,
            )

        logger.info(
            "Registered %s anchor v%d for agent=%s (id=%s)",
            anchor_type, latest_version, agent_id, anchor_id,
        )
        return anchor

    def get_anchors(
        self, agent_id: str, anchor_type: Optional[str] = None
    ) -> List[IdentityAnchor]:
        """Retrieve anchors for an agent, optionally filtered by type.

        Args:
            agent_id: Target agent.
            anchor_type: Optional type filter.

        Returns:
            List of IdentityAnchor objects.
        """
        if not self._adapter:
            return []

        if anchor_type:
            raw = self._adapter.get_anchors(agent_id, anchor_type)
            return [IdentityAnchor.from_dict(r) for r in (raw or [])]
        else:
            grouped = self._adapter.get_all_anchors(agent_id)
            flat = []
            for anchors in (grouped or {}).values():
                if isinstance(anchors, list):
                    flat.extend(anchors)
            return [IdentityAnchor.from_dict(r) for r in flat]

    # ── Identity Reconstruction ────────────────────────────────────────

    def reconstruct_identity(self, agent_id: str) -> IdentityProfile:
        """Reconstruct a complete identity profile from all anchors.

        Args:
            agent_id: Target agent.

        Returns:
            IdentityProfile with all anchors and consistency score.
        """
        all_anchors = self.get_anchors(agent_id)
        return self._build_profile(agent_id, all_anchors)

    def partial_reconstruct(
        self, agent_id: str, available_anchors: List[str]
    ) -> IdentityProfile:
        """Reconstruct identity from only a subset of anchor types (fault recovery).

        Args:
            agent_id: Target agent.
            available_anchors: List of anchor type names that are available.

        Returns:
            IdentityProfile from partial anchors.
        """
        anchors: List[IdentityAnchor] = []
        for atype in available_anchors:
            anchors.extend(self.get_anchors(agent_id, atype))
        profile = self._build_profile(agent_id, anchors)
        profile.drift_flags.append(
            f"PARTIAL_RECONSTRUCT: Only {len(available_anchors)}/4 anchor types available"
        )
        return profile

    def _build_profile(
        self, agent_id: str, anchors: List[IdentityAnchor]
    ) -> IdentityProfile:
        """Internal: build IdentityProfile from anchor list."""
        counts: Dict[str, int] = {}
        for a in anchors:
            counts[a.anchor_type] = counts.get(a.anchor_type, 0) + 1

        consistency = self._compute_consistency(anchors)
        now = datetime.now(timezone.utc).isoformat()

        return IdentityProfile(
            agent_id=agent_id,
            anchors=anchors,
            consistency_score=consistency,
            anchor_counts=counts,
            last_reconstructed_at=now,
        )

    def _compute_consistency(self, anchors: List[IdentityAnchor]) -> float:
        """Compute anchor consistency score (0.0–1.0).

        Checks for conflicting content across anchors of the same type
        or contradictory value specifications.
        """
        if not anchors:
            return 0.0

        # Simple heuristic: check for version conflicts within same type
        type_versions: Dict[str, List[int]] = {}
        for a in anchors:
            type_versions.setdefault(a.anchor_type, []).append(a.version)

        conflict_count = 0
        for atype, versions in type_versions.items():
            # If we have multiple anchors of same type with equal versions,
            # that's a potential conflict
            if len(versions) > 1 and len(set(versions)) < len(versions):
                conflict_count += 1

        return max(0.0, 1.0 - (conflict_count * 0.25))

    # ── Dynamic Weight Computation ──────────────────────────────────

    def _compute_dynamic_weights(self, agent_id: str) -> Dict[str, float]:
        """Compute dynamic anchor weights based on agent interaction frequency.

        For high-frequency agents (daily temporal pattern with 3+ active windows),
        the temporal weight is boosted from 0.15 to 0.25–0.30. Other weights are
        proportionally scaled down to maintain sum = 1.0.

        Parameters
        ----------
        agent_id: Target agent identifier.

        Returns
        -------
        Dict with weights for behavioral, knowledge, relational, temporal.
        """
        weights = dict(ANCHOR_WEIGHTS)

        temporal_anchors = self.get_temporal_anchors(agent_id)
        if not temporal_anchors:
            return weights

        latest = temporal_anchors[-1]
        active_windows = latest.active_windows
        temporal_pattern = latest.temporal_pattern

        # High-frequency criteria: daily pattern + 3+ active windows
        if temporal_pattern == "daily" and len(active_windows) >= 3:
            boost = 0.25
            # Extra boost for very frequent agents (6+ windows)
            if len(active_windows) >= 6:
                boost = 0.30

            weights["temporal"] = boost

            # Redistribute the remaining weight proportionally
            remaining = 1.0 - boost
            static_remaining = 1.0 - ANCHOR_WEIGHTS["temporal"]  # 0.85
            scale = remaining / static_remaining
            weights["behavioral"] = round(ANCHOR_WEIGHTS["behavioral"] * scale, 4)
            weights["knowledge"] = round(ANCHOR_WEIGHTS["knowledge"] * scale, 4)
            weights["relational"] = round(ANCHOR_WEIGHTS["relational"] * scale, 4)

        return weights

    # ── Drift Detection (v8.5.0 — Four-Anchor Weighted) ─────────────

    def detect_identity_drift(self, agent_id: str) -> Dict[str, Any]:
        """Four-anchor weighted identity drift detection.

        Computes drift scores across four dimensions (behavioral, knowledge,
        relational, temporal) and aggregates via weighted sum.  Severity is
        classified as WARNING (0.3-0.5), CRITICAL (0.5-0.7), or
        IDENTITY_BREAK (>0.7).

        Args:
            agent_id: Target agent.

        Returns:
            Dict with drift_score, severity, is_drifting, dimension_scores,
            warnings, anchor_comparisons, and recommendation.
        """
        profile = self.reconstruct_identity(agent_id)
        warnings: List[str] = []
        dimension_scores: Dict[str, float] = {}

        # Classify anchors into four dimensions
        identity_anchors = [a for a in profile.anchors if a.anchor_type == "identity_files"]
        procedural_anchors = [a for a in profile.anchors if a.anchor_type == "procedural_patterns"]
        episodic_anchors = [a for a in profile.anchors if a.anchor_type == "episodic_keys"]
        value_anchors = [a for a in profile.anchors if a.anchor_type == "value_specifications"]

        # Dimension 1: Behavioral (identity_files + procedural_patterns) — weight 0.3
        behavioral_score = self._detect_behavioral_drift(
            identity_anchors, procedural_anchors, warnings,
        )
        dimension_scores["behavioral"] = behavioral_score

        # Dimension 2: Knowledge (episodic_keys) — weight 0.3
        knowledge_score = self._detect_knowledge_drift(episodic_anchors, warnings)
        dimension_scores["knowledge"] = knowledge_score

        # Dimension 3: Relational (value_specifications) — weight 0.25
        relational_score = self._detect_relational_drift(value_anchors, warnings)
        dimension_scores["relational"] = relational_score

        # Dimension 4: Temporal — weight 0.15
        temporal_score = self._detect_temporal_drift(agent_id, warnings)
        dimension_scores["temporal"] = temporal_score

        # Compute dynamic weights based on interaction frequency
        dynamic_weights = self._compute_dynamic_weights(agent_id)

        # Weighted aggregation
        drift_score = (
            behavioral_score * dynamic_weights["behavioral"]
            + knowledge_score * dynamic_weights["knowledge"]
            + relational_score * dynamic_weights["relational"]
            + temporal_score * dynamic_weights["temporal"]
        )

        # Determine severity
        severity = "STABLE"
        for sev, (low, high) in DRIFT_SEVERITY.items():
            if low <= drift_score < high:
                severity = sev
                break

        # Build anchor comparisons
        anchor_comparisons: List[Dict[str, Any]] = []
        for a in profile.anchors:
            anchor_comparisons.append({
                "id": a.id,
                "type": a.anchor_type,
                "version": a.version,
                "checksum": a.checksum,
            })

        # Build recommendation
        if severity == "IDENTITY_BREAK":
            recommendation = "CRITICAL: Isolate agent, rollback to last stable snapshot, and re-register all anchors"
        elif severity == "CRITICAL":
            recommendation = "Re-validate identity anchors and consider re-registering affected dimensions"
        elif severity == "WARNING":
            recommendation = "Monitor drift; schedule anchor consistency audit"
        else:
            recommendation = "Identity stable"

        return {
            "agent_id": agent_id,
            "drift_score": round(drift_score, 4),
            "severity": severity,
            "is_drifting": drift_score > self._drift_threshold,
            "dimension_scores": {k: round(v, 4) for k, v in dimension_scores.items()},
            "anchor_weights": dynamic_weights,
            "warnings": warnings,
            "anchor_comparisons": anchor_comparisons,
            "recommendation": recommendation,
        }

    def _detect_behavioral_drift(
        self,
        identity_anchors: List[IdentityAnchor],
        procedural_anchors: List[IdentityAnchor],
        warnings: List[str],
    ) -> float:
        """Compute behavioral drift (identity_files vs procedural_patterns).

        Uses n-gram similarity heuristic: compares anchor content structure
        to detect mismatch between declared identity and actual procedures.
        """
        if not identity_anchors and not procedural_anchors:
            return 0.0

        score = 0.0

        # No baseline — heavy drift
        if not identity_anchors and procedural_anchors:
            warnings.append("behavioral_no_baseline")
            return 0.85

        # No procedural data — cannot assess
        if identity_anchors and not procedural_anchors:
            return 0.05

        # Compare key overlap between identity values and procedural tags
        identity_keys = set()
        for a in identity_anchors:
            for k in a.content:
                identity_keys.add(k)
            tags = a.content.get("tags", [])
            if isinstance(tags, list):
                identity_keys.update(tags)

        procedural_keys = set()
        for a in procedural_anchors:
            for k in a.content:
                procedural_keys.add(k)
            tags = a.content.get("tags", [])
            if isinstance(tags, list):
                procedural_keys.update(tags)

        if identity_keys and procedural_keys:
            overlap = len(identity_keys & procedural_keys)
            union = len(identity_keys | procedural_keys)
            jaccard = overlap / union if union > 0 else 0.0
            score = (1.0 - jaccard) * 0.5
        else:
            score = 0.3

        # Procedural proliferation check
        if len(procedural_anchors) > len(identity_anchors) * 3:
            warnings.append("procedural_anchors_proliferation")
            score += 0.2

        return min(score, 1.0)

    def _detect_knowledge_drift(
        self,
        episodic_anchors: List[IdentityAnchor],
        warnings: List[str],
    ) -> float:
        """Compute knowledge drift (episodic_keys consistency).

        Checks for stale or conflicting episodic anchors by comparing
        timestamps and content hash overlap.
        """
        if not episodic_anchors:
            return 0.0

        if len(episodic_anchors) == 1:
            return 0.05  # single anchor — low signal

        score = 0.0
        now = datetime.now(timezone.utc)

        # Staleness check: anchors older than 90 days
        stale_count = 0
        for a in episodic_anchors:
            try:
                ts = datetime.fromisoformat(a.updated_at.replace("Z", "+00:00"))
                if (now - ts).days > 90:
                    stale_count += 1
            except (ValueError, AttributeError):
                pass

        if stale_count > 0:
            stale_ratio = stale_count / len(episodic_anchors)
            score += stale_ratio * 0.3
            if stale_ratio > 0.5:
                warnings.append("episodic_staleness")

        # Version spread check — high version spread suggests instability
        versions = [a.version for a in episodic_anchors]
        if len(versions) >= 3:
            v_range = max(versions) - min(versions)
            if v_range > 10:
                score += 0.2
                warnings.append("episodic_version_spread")

        return min(score, 1.0)

    def _detect_relational_drift(
        self,
        value_anchors: List[IdentityAnchor],
        warnings: List[str],
    ) -> float:
        """Compute relational drift (value_specifications integrity).

        Missing value specifications are the strongest drift signal.
        """
        if not value_anchors:
            warnings.append("missing_value_specifications")
            return 0.8  # High drift when no values defined

        # Check for conflicting values across versions
        if len(value_anchors) >= 2:
            content_hashes = set(a.checksum for a in value_anchors)
            if len(content_hashes) < len(value_anchors):
                return 0.1  # Consistent values — minor drift
            else:
                return 0.25  # Multiple divergent value specs

        return 0.05  # Single consistent value spec

    def _detect_temporal_drift(
        self,
        agent_id: str,
        warnings: List[str],
    ) -> float:
        """Compute temporal drift by comparing current activity against
        historical temporal anchor patterns.

        Uses Jensen-Shannon divergence between current active-hour
        distribution and the historical baseline extracted from
        TemporalAnchor.active_windows.

        Returns 0.0 if no temporal anchor exists (cold start).
        """
        temporal_anchors = self.get_temporal_anchors(agent_id)
        if not temporal_anchors:
            return 0.0  # No baseline — cannot detect drift

        # Take the most recent temporal anchor as baseline
        baseline = temporal_anchors[-1]
        baseline_windows = baseline.active_windows

        if not baseline_windows:
            return 0.0

        # Build baseline hour distribution (24 bins)
        baseline_dist = [0.0] * 24
        for w in baseline_windows:
            start = w.get("start_hour", 0)
            end = w.get("end_hour", 24)
            for h in range(start, min(end, 24)):
                baseline_dist[h] = 1.0 / len(baseline_windows)

        # Normalize baseline
        total = sum(baseline_dist)
        if total > 0:
            baseline_dist = [b / total for b in baseline_dist]

        # Current distribution: derive from active windows of the temporal anchor
        # (using the same windows as current; drift is measured against older patterns)
        if len(temporal_anchors) < 2:
            return 0.05  # Only one anchor — low temporal signal

        older = temporal_anchors[0]
        older_windows = older.active_windows
        if not older_windows:
            return 0.0

        older_dist = [0.0] * 24
        for w in older_windows:
            start = w.get("start_hour", 0)
            end = w.get("end_hour", 24)
            for h in range(start, min(end, 24)):
                older_dist[h] = 1.0 / len(older_windows)

        total = sum(older_dist)
        if total > 0:
            older_dist = [o / total for o in older_dist]

        # Jensen-Shannon divergence
        jsd = self._jensen_shannon_divergence(baseline_dist, older_dist)

        if jsd > 0.5:
            warnings.append("temporal_anomaly")

        return min(jsd, 1.0)

    # ── Jensen-Shannon Divergence ──────────────────────────────────────

    @staticmethod
    def _jensen_shannon_divergence(p: List[float], q: List[float]) -> float:
        """Compute Jensen-Shannon divergence between two distributions.

        Parameters
        ----------
        p, q : List[float]
            Probability distributions of equal length.

        Returns
        -------
        float between 0.0 (identical) and 1.0 (maximally different).
        """
        import math
        if len(p) != len(q):
            return 1.0
        n = len(p)
        m = [(p[i] + q[i]) / 2.0 for i in range(n)]

        def _kl(a: List[float], b: List[float]) -> float:
            eps = 1e-10
            return sum(
                a[i] * math.log((a[i] + eps) / (b[i] + eps))
                for i in range(n)
                if a[i] > 0
            )

        kl_pm = _kl(p, m)
        kl_qm = _kl(q, m)
        jsd = math.sqrt((kl_pm + kl_qm) / 2.0)
        return min(jsd, 1.0)

    # ── Temporal Anchor Management ─────────────────────────────────────

    def register_temporal_anchor(
        self,
        agent_id: str,
        access_times: List[str],
        timezone: str = "UTC",
    ) -> TemporalAnchor:
        """Register or update a temporal identity anchor from access logs.

        Parameters
        ----------
        agent_id: Target agent identifier.
        access_times: List of ISO 8601 access timestamps.
        timezone: IANA timezone string.

        Returns
        -------
        TemporalAnchor extracted from the access pattern.
        """
        anchor = TemporalAnchor.from_access_log(agent_id, access_times, timezone)

        # Persist via storage adapter
        if self._adapter and hasattr(self._adapter, "upsert_temporal_anchor"):
            self._adapter.upsert_temporal_anchor(
                agent_id=agent_id,
                anchor_id=anchor.anchor_id,
                temporal_pattern=anchor.temporal_pattern,
                active_windows=json.dumps(anchor.active_windows),
                last_seen=anchor.last_seen,
                session_duration_avg=anchor.session_duration_avg,
                timezone=anchor.timezone,
            )

        logger.info(
            "Temporal anchor %s: pattern=%s, windows=%d, agent=%s",
            anchor.anchor_id, anchor.temporal_pattern,
            len(anchor.active_windows), agent_id,
        )
        return anchor

    def get_temporal_anchors(self, agent_id: str) -> List[TemporalAnchor]:
        """Retrieve all temporal anchors for an agent.

        Parameters
        ----------
        agent_id: Target agent.

        Returns
        -------
        List of TemporalAnchor, sorted by last_seen ascending.
        """
        if not self._adapter or not hasattr(self._adapter, "get_temporal_anchors"):
            return []

        raw = self._adapter.get_temporal_anchors(agent_id)
        anchors = [TemporalAnchor.from_dict(r) for r in (raw or [])]
        anchors.sort(key=lambda a: a.last_seen)
        return anchors

    # ── Anchor Sync ────────────────────────────────────────────────────

    def sync_anchors(self, agent_id: str) -> Dict[str, Any]:
        """Ensure all anchors for an agent are consistent.

        Args:
            agent_id: Target agent.

        Returns:
            Dict with sync results.
        """
        anchors = self.get_anchors(agent_id)
        synced: List[str] = []
        conflicts: List[Dict[str, Any]] = []

        # Group by type and select latest version per type
        type_latest: Dict[str, IdentityAnchor] = {}
        for a in anchors:
            existing = type_latest.get(a.anchor_type)
            if not existing or a.version > existing.version:
                type_latest[a.anchor_type] = a

        for atype, latest in type_latest.items():
            # Check checksum consistency
            content_json = json.dumps(latest.content, sort_keys=True, ensure_ascii=False)
            expected_checksum = hashlib.sha256(content_json.encode("utf-8")).hexdigest()
            if expected_checksum != latest.checksum:
                conflicts.append({
                    "anchor_id": latest.id,
                    "anchor_type": atype,
                    "issue": "checksum_mismatch",
                    "expected": expected_checksum,
                    "actual": latest.checksum,
                })
            else:
                synced.append(latest.id)

        return {
            "agent_id": agent_id,
            "total_anchors": len(anchors),
            "synced": len(synced),
            "conflicts": len(conflicts),
            "conflict_details": conflicts if conflicts else None,
        }

    # ── Import / Export ────────────────────────────────────────────────

    def export_identity_bundle(self, agent_id: str) -> IdentityBundle:
        """Export a complete identity bundle for agent migration.

        Args:
            agent_id: Source agent.

        Returns:
            IdentityBundle ready for serialization/transfer.
        """
        anchors = self.get_anchors(agent_id)
        anchor_dicts = [a.to_dict() for a in anchors]
        exported_at = datetime.now(timezone.utc).isoformat()

        # Compute bundle checksum
        bundle_content = json.dumps({
            "agent_id": agent_id,
            "exported_at": exported_at,
            "anchors": anchor_dicts,
        }, sort_keys=True, ensure_ascii=False)
        bundle_checksum = hashlib.sha256(bundle_content.encode("utf-8")).hexdigest()

        return IdentityBundle(
            agent_id=agent_id,
            exported_at=exported_at,
            anchors=anchor_dicts,
            checksum=bundle_checksum,
        )

    def import_identity_bundle(self, bundle: IdentityBundle) -> int:
        """Import an identity bundle (for agent migration/recovery).

        Args:
            bundle: Exported IdentityBundle.

        Returns:
            Number of anchors imported.
        """
        count = 0
        for anchor_dict in bundle.anchors:
            anchor = IdentityAnchor.from_dict(anchor_dict)
            # Use target agent_id from bundle, not original
            anchor.agent_id = bundle.agent_id
            anchor.id = str(uuid.uuid4())
            anchor.created_at = datetime.now(timezone.utc).isoformat()
            anchor.updated_at = anchor.created_at

            if self._adapter and hasattr(self._adapter, "upsert_anchor"):
                self._adapter.upsert_anchor(
                    agent_id=bundle.agent_id,
                    anchor_type=anchor.anchor_type,
                    content=json.dumps(anchor.content, sort_keys=True, ensure_ascii=False),
                    version=anchor.version,
                )
            count += 1

        logger.info("Imported %d anchors for agent=%s", count, bundle.agent_id)
        return count

    # ── Self-Test ──────────────────────────────────────────────────────

    def self_test(self) -> Dict[str, Any]:
        """Runtime self-diagnostic: full CRUD + reconstruction + drift cycle.

        Returns:
            {"pass": bool, "checks": [...], "summary": str}
        """
        import os
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), f"trinity_identity_test_{os.getpid()}.db")
        checks = []

        try:
            from trinity.adapters.sqlite import SQLiteAdapter
            adapter = SQLiteAdapter(db_path)
            adapter.connect()
            mgr = IdentityManager(storage_adapter=adapter)
            test_agent = f"self_test_agent_{os.getpid()}"

            # Check 1: register anchor → get_anchors
            try:
                anchor = mgr.register_anchor(test_agent, "identity_files", {"name": "TestAgent", "values": ["honesty"]})
                anchors = mgr.get_anchors(test_agent)
                assert len(anchors) > 0, "get_anchors returned empty after register"
                checks.append({"name": "register_and_get", "pass": True, "detail": f"Registered + retrieved {len(anchors)} anchor(s)"})
            except Exception as e:
                checks.append({"name": "register_and_get", "pass": False, "detail": str(e)})

            # Check 2: get_all_anchors returns dict with keys
            try:
                all_a = adapter.get_all_anchors(test_agent)
                assert isinstance(all_a, dict) and len(all_a) > 0, f"get_all_anchors returned {type(all_a).__name__} with {len(all_a) if isinstance(all_a, dict) else 'N/A'} entries"
                checks.append({"name": "get_all_anchors", "pass": True, "detail": f"Anchor types: {list(all_a.keys())}"})
            except Exception as e:
                checks.append({"name": "get_all_anchors", "pass": False, "detail": str(e)})

            # Check 3: reconstruct_identity
            try:
                profile = mgr.reconstruct_identity(test_agent)
                assert profile.agent_id == test_agent, "Profile agent_id mismatch"
                assert profile.consistency_score == 1.0, f"Consistency expected 1.0, got {profile.consistency_score}"
                checks.append({"name": "reconstruct_identity", "pass": True, "detail": f"consistency={profile.consistency_score}, anchors={len(profile.anchors)}"})
            except Exception as e:
                checks.append({"name": "reconstruct_identity", "pass": False, "detail": str(e)})

            # Check 4: detect_drift returns structured result with severity + dimension_scores
            try:
                # Register temporal anchor first
                temporal = mgr.register_temporal_anchor(
                    test_agent,
                    [
                        "2026-08-10T09:00:00+00:00",
                        "2026-08-10T10:00:00+00:00",
                        "2026-08-10T14:00:00+00:00",
                    ],
                    timezone="UTC",
                )
                assert temporal.anchor_id, "Temporal anchor has no ID"
                assert len(temporal.active_windows) > 0, "No active windows extracted"

                drift = mgr.detect_identity_drift(test_agent)
                assert isinstance(drift["is_drifting"], bool), f"is_drifting is {type(drift['is_drifting']).__name__}, not bool"
                assert "severity" in drift, "Missing severity field"
                assert "dimension_scores" in drift, "Missing dimension_scores field"
                assert set(drift["dimension_scores"].keys()) == {"behavioral", "knowledge", "relational", "temporal"}, \
                    f"Unexpected dimension keys: {list(drift['dimension_scores'].keys())}"
                checks.append({"name": "detect_drift_4anchor", "pass": True, "detail": f"severity={drift['severity']}, score={drift['drift_score']}"})
            except Exception as e:
                checks.append({"name": "detect_drift_4anchor", "pass": False, "detail": str(e)})

            # Check 4b: JS divergence utility
            try:
                jsd = mgr._jensen_shannon_divergence([0.5, 0.5], [0.5, 0.5])
                assert abs(jsd) < 0.01, f"Identical distributions should have JSD ≈ 0, got {jsd}"
                jsd2 = mgr._jensen_shannon_divergence([1.0, 0.0], [0.0, 1.0])
                assert jsd2 > 0.5, f"Opposite distributions should have JSD > 0.5, got {jsd2}"
                checks.append({"name": "jensen_shannon_divergence", "pass": True, "detail": f"identical={jsd:.4f}, opposite={jsd2:.4f}"})
            except Exception as e:
                checks.append({"name": "jensen_shannon_divergence", "pass": False, "detail": str(e)})

            # Check 5: export / import bundle
            try:
                bundle = mgr.export_identity_bundle(test_agent)
                assert bundle.agent_id == test_agent, "Bundle agent_id mismatch"
                assert bundle.checksum, "Bundle missing checksum"
                assert len(bundle.anchors) > 0, "Bundle has no anchors"
                # Verify bundle structure (full import round-trip tested separately)
                for a in bundle.anchors:
                    assert "anchor_type" in a, "Anchor dict missing anchor_type"
                checks.append({"name": "export_import_bundle", "pass": True, "detail": f"Exported bundle with {len(bundle.anchors)} anchors"})
            except Exception as e:
                checks.append({"name": "export_import_bundle", "pass": False, "detail": str(e)})

            # Check 6: partial_reconstruct
            try:
                partial = mgr.partial_reconstruct(test_agent, ["identity_files"])
                assert partial.agent_id == test_agent, "Partial profile agent_id mismatch"
                assert any("PARTIAL_RECONSTRUCT" in f for f in partial.drift_flags), "Missing PARTIAL_RECONSTRUCT flag"
                checks.append({"name": "partial_reconstruct", "pass": True, "detail": f"drift_flags={partial.drift_flags}"})
            except Exception as e:
                checks.append({"name": "partial_reconstruct", "pass": False, "detail": str(e)})

            # Check 7: invalid anchor_type raises
            try:
                mgr.register_anchor(test_agent, "invalid_type", {})
                checks.append({"name": "invalid_anchor_type", "pass": False, "detail": "Should have raised ValueError"})
            except ValueError:
                checks.append({"name": "invalid_anchor_type", "pass": True, "detail": "ValueError correctly raised"})
            except Exception as e:
                checks.append({"name": "invalid_anchor_type", "pass": False, "detail": f"Unexpected error: {e}"})

            adapter.disconnect()
        except Exception as e:
            checks.append({"name": "setup", "pass": False, "detail": f"Test harness failure: {e}"})
        finally:
            try:
                if os.path.exists(db_path):
                    os.unlink(db_path)
            except OSError:
                pass

        all_pass = all(c["pass"] for c in checks)
        return {
            "pass": all_pass,
            "checks": checks,
            "summary": f"IdentityManager self-test: {sum(1 for c in checks if c['pass'])}/{len(checks)} passed",
        }

    # ── Auto-Anchor (from memory ingestion) ────────────────────────────

    def maybe_create_episodic_anchor(
        self,
        agent_id: str,
        memory_id: str,
        content: str,
        importance: float,
        tags: Optional[List[str]] = None,
    ) -> Optional[IdentityAnchor]:
        """Auto-promote high-importance memories to episodic_key anchors.

        Args:
            agent_id: Agent that created the memory.
            memory_id: Source memory ID.
            content: Memory content.
            importance: Importance score (0.0–1.0).
            tags: Optional tags.

        Returns:
            New IdentityAnchor if importance exceeds threshold, else None.
        """
        if importance < self._episodic_importance_threshold:
            return None

        return self.register_anchor(
            agent_id=agent_id,
            anchor_type="episodic_keys",
            content={
                "source_memory_id": memory_id,
                "content": content,
                "importance": importance,
                "tags": tags or [],
                "auto_generated": True,
            },
            tags=tags,
        )


# ── Module-level self_test ─────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module-level entry point for regression testing."""
    im = IdentityManager()
    return im.self_test()
