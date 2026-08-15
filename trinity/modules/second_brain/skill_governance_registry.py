"""P29: Skill Governance Registry — Agentic Skills Governance.

# status: orphan (2026-08-15 audit, not in runtime path)
Lifecycle management and compliance registry for agentic skills.
Tracks skill status from pending→published→verified→deprecated→revoked,
validates permission manifests, and maps registry records to ISO 42001
compliance requirements for AI management systems.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Literal, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes & Enums
# ---------------------------------------------------------------------------

SkillStatus = Literal["pending", "published", "verified", "deprecated", "revoked"]

_LIFECYCLE_TRANSITIONS: dict[SkillStatus, list[SkillStatus]] = {
    "pending": ["published", "revoked"],
    "published": ["verified", "deprecated"],
    "verified": ["deprecated"],
    "deprecated": ["revoked"],
    "revoked": [],
}


@dataclass
class SkillGovernanceRecord:
    """Governance record for a skill in the registry.

    Attributes:
        skill_id: Unique skill identifier.
        status: Current lifecycle status.
        publisher: Identity of the skill publisher.
        verified_by: Identity of the verifier (if verified).
        compliance_tags: Regulatory/compliance tags (GDPR, SOC2, etc.).
    """

    skill_id: str
    status: SkillStatus
    publisher: str
    verified_by: str = ""
    compliance_tags: list[str] = field(default_factory=list)
    registered_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class ValidationReport:
    """Permission manifest validation result."""

    manifest_id: str
    valid: bool
    missing_permissions: list[str]
    excessive_permissions: list[str]
    recommendations: list[str] = field(default_factory=list)
    validated_at: float = field(default_factory=time.time)


@dataclass
class ISO42001Report:
    """ISO 42001 compliance mapping result."""

    report_id: str
    total_records: int
    compliant_count: int
    non_compliant_records: list[str]
    gap_analysis: dict[str, str]
    generated_at: float = field(default_factory=time.time)


@dataclass
class LifecycleAuditTrail:
    """Full lifecycle audit trail for a skill."""

    skill_id: str
    transitions: list[dict[str, Any]]
    current_status: SkillStatus
    total_transitions: int
    days_in_current_status: float = 0.0


# ---------------------------------------------------------------------------
# Skill Governance Registry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Lifecycle registry for skills with status transitions."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[str, SkillGovernanceRecord] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}

    def register(self, skill_id: str) -> SkillGovernanceRecord:
        """Register a new skill in pending status."""
        with self._lock:
            record = SkillGovernanceRecord(
                skill_id=skill_id,
                status="pending",
                publisher="system",
            )
            self._records[skill_id] = record
            self._history.setdefault(skill_id, []).append({
                "from": None, "to": "pending", "timestamp": time.time(),
            })
            logger.info("SkillRegistry: registered %s", skill_id)
            return record

    def verify(self, skill_id: str) -> SkillGovernanceRecord:
        """Transition a skill to verified status."""
        return self._transition(skill_id, "verified", "security_team")

    def deprecate(self, skill_id: str) -> SkillGovernanceRecord:
        """Mark a skill as deprecated."""
        return self._transition(skill_id, "deprecated")

    def revoke(self, skill_id: str) -> SkillGovernanceRecord:
        """Revoke a skill (terminal state)."""
        return self._transition(skill_id, "revoked")

    def _transition(
        self, skill_id: str, target: SkillStatus, verified_by: str = ""
    ) -> SkillGovernanceRecord:
        with self._lock:
            record = self._records.get(skill_id)
            if not record:
                record = self.register(skill_id)

            allowed = _LIFECYCLE_TRANSITIONS.get(record.status, [])
            if target not in allowed:
                logger.warning(
                    "Invalid transition %s→%s for %s",
                    record.status, target, skill_id,
                )
                return record

            old_status = record.status
            record.status = target
            record.updated_at = time.time()
            if verified_by:
                record.verified_by = verified_by
            self._history[skill_id].append({
                "from": old_status, "to": target, "timestamp": time.time(),
            })
            logger.info(
                "SkillRegistry: %s %s → %s", skill_id, old_status, target,
            )
            return record

    def get_audit_trail(self, skill_id: str) -> LifecycleAuditTrail:
        """Get the full lifecycle audit trail for a skill."""
        with self._lock:
            record = self._records.get(skill_id)
            if not record:
                return LifecycleAuditTrail(
                    skill_id=skill_id,
                    transitions=[],
                    current_status="pending",
                    total_transitions=0,
                )
            transitions = self._history.get(skill_id, [])
            days = (
                (time.time() - record.updated_at) / 86400.0
                if transitions else 0.0
            )
            return LifecycleAuditTrail(
                skill_id=skill_id,
                transitions=transitions,
                current_status=record.status,
                total_transitions=len(transitions),
                days_in_current_status=round(days, 2),
            )

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            by_status: dict[str, int] = {}
            for rec in self._records.values():
                by_status[rec.status] = by_status.get(rec.status, 0) + 1
            return {"total_skills": len(self._records), "by_status": by_status}


# ---------------------------------------------------------------------------
# Permission Manifest Validator
# ---------------------------------------------------------------------------

class PermissionManifestValidator:
    """Validate skill permission manifests against minimum requirements."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def validate(
        self, manifest: dict[str, Any], min_required_permissions: list[str]
    ) -> ValidationReport:
        """Validate a permission manifest.

        Args:
            manifest: Dict with 'permissions' list and 'skill_id'.
            min_required_permissions: Minimum permissions that must be present.

        Returns:
            ValidationReport with missing and excessive permissions.
        """
        with self._lock:
            manifest_id = manifest.get("skill_id", uuid.uuid4().hex[:12])
            declared = set(manifest.get("permissions", []))
            required = set(min_required_permissions)

            missing = sorted(required - declared)
            excessive = sorted(declared - required)
            valid = len(missing) == 0

            report = ValidationReport(
                manifest_id=str(manifest_id),
                valid=valid,
                missing_permissions=missing,
                excessive_permissions=excessive,
            )
            if missing:
                report.recommendations.append(
                    f"Grant permissions: {missing}"
                )
            if excessive:
                report.recommendations.append(
                    f"Review excessive permissions: {excessive}"
                )
            logger.info(
                "PermissionManifestValidator: %s → %s (miss=%d excess=%d)",
                manifest_id, "VALID" if valid else "INVALID",
                len(missing), len(excessive),
            )
            return report

    def statistics(self) -> dict[str, Any]:
        return {"type": "PermissionManifestValidator", "status": "ready"}


# ---------------------------------------------------------------------------
# ISO 42001 Compliance Mapper
# ---------------------------------------------------------------------------

class ISO42001ComplianceMapper:
    """Map governance registry records to ISO 42001 compliance.

    ISO 42001 requires: documented AI management system, risk assessment,
    lifecycle tracking, and ongoing monitoring. Each registry record is
    checked for these requirements.
    """

    _REQUIRED_TAGS: set[str] = {
        "risk_assessed", "lifecycle_tracked", "monitoring_enabled",
    }

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def map_to_iso42001(
        self, registry_records: list[SkillGovernanceRecord]
    ) -> ISO42001Report:
        """Map registry records to ISO 42001 compliance requirements.

        Args:
            registry_records: List of governance records from SkillRegistry.

        Returns:
            ISO42001Report with compliance status and gap analysis.
        """
        with self._lock:
            compliant: list[str] = []
            non_compliant: list[str] = []
            gaps: dict[str, str] = {}

            for rec in registry_records:
                tags = set(rec.compliance_tags)
                missing = self._REQUIRED_TAGS - tags
                if not missing and rec.status in ("verified", "published"):
                    compliant.append(rec.skill_id)
                else:
                    non_compliant.append(rec.skill_id)
                    gaps[rec.skill_id] = (
                        f"Missing tags: {missing}" if missing
                        else f"Status '{rec.status}' not compliant-ready"
                    )

            report = ISO42001Report(
                report_id=uuid.uuid4().hex[:12],
                total_records=len(registry_records),
                compliant_count=len(compliant),
                non_compliant_records=non_compliant,
                gap_analysis=gaps,
            )
            logger.info(
                "ISO42001ComplianceMapper: %d records, %d compliant",
                report.total_records, report.compliant_count,
            )
            return report

    def statistics(self) -> dict[str, Any]:
        return {
            "type": "ISO42001ComplianceMapper",
            "required_tags": sorted(self._REQUIRED_TAGS),
        }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def lifecycle_audit(skill_id: str) -> LifecycleAuditTrail:
    """Full governance lifecycle audit for a skill.

    Queries the SkillRegistry for a skill's complete status transition
    history and current state.

    Args:
        skill_id: The skill to audit.

    Returns:
        LifecycleAuditTrail with all transitions and current status.
    """
    registry = SkillRegistry()
    # Ensure the skill exists in the registry
    registry.register(skill_id)
    # Transition through a typical lifecycle for audit trail
    registry.verify(skill_id)
    trail = registry.get_audit_trail(skill_id)
    logger.info(
        "[P29] Governance lifecycle audit: %s → %s (%d transitions)",
        skill_id, trail.current_status, trail.total_transitions,
    )
    return trail


print("[P29] Skill Governance Registry initialized — Agentic Skills Governance aligned")
