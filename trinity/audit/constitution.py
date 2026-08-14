"""
ConstitutionalEngine — Invariant-based policy checker for DCSA-EJP.

Defines the four core constitutional invariants per the Curve Labs (2026)
Dual-Loop Constitutional Self-Auditing framework:

  NO_UNAUTHORIZED_EXFILTRATION
  NO_UNVERIFIED_IRREVERSIBLE_ACTION
  NO_POLICY_SILENT_OVERRIDE
  HUMAN_HANDOFF_ON_AMBIGUITY

Supports dynamic addition of custom invariants and full-invariant checks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class Severity(str, Enum):
    """Violation severity levels."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ViolationResult(str, Enum):
    """Per-invariant check result."""
    PASS = "pass"
    FAIL = "fail"
    FLAG = "flag"       # Indeterminate — requires auditor review
    SKIP = "skip"        # Irrelevant to this action


@dataclass
class Invariant:
    """A single constitutional invariant rule.

    Fields:
        name       — Human-readable invariant name
        rule       — Natural-language description of the rule
        severity   — Violation severity (CRITICAL/HIGH/MEDIUM/LOW)
        predicate  — Callable(action_context) → (pass/fail/flag, reason)
    """

    name: str
    rule: str
    severity: Severity = Severity.MEDIUM
    predicate: Optional[Callable[[Dict[str, Any]], Tuple[ViolationResult, str]]] = None

    def check(self, action_context: Dict[str, Any]) -> Tuple[ViolationResult, str]:
        if self.predicate:
            try:
                return self.predicate(action_context)
            except Exception as e:
                logger.warning("Invariant '%s' predicate raised: %s", self.name, e)
                return (ViolationResult.FLAG, f"Predicate error: {e}")
        return (ViolationResult.PASS, "No predicate — allowed by default")


class ConstitutionalEngine:
    """DCSA-EJP Constitutional Engine.

    Defines and enforces policy invariants. Each invariant has a predicate
    that inspects action_context and returns (pass/fail/flag, reason).

    Core invariants are loaded via ``load_default_constitution()``.
    """

    def __init__(self):
        self._invariants: Dict[str, Invariant] = {}
        self._last_check_at: Optional[datetime] = None

    # ── Invariant Management ────────────────────────────────────────

    def add_invariant(
        self,
        name: str,
        rule: str,
        severity: Severity = Severity.MEDIUM,
        predicate: Optional[Callable[[Dict[str, Any]], Tuple[ViolationResult, str]]] = None,
    ) -> Invariant:
        """Add a custom invariant.

        Returns the registered Invariant object.
        """
        inv = Invariant(name=name, rule=rule, severity=severity, predicate=predicate)
        self._invariants[name] = inv
        logger.info("Added invariant '%s' (severity=%s)", name, severity.value)
        return inv

    def remove_invariant(self, name: str) -> bool:
        """Remove an invariant by name. Core invariants cannot be removed."""
        if name in self._CORE_NAMES:
            logger.warning("Cannot remove core invariant '%s'", name)
            return False
        return self._invariants.pop(name, None) is not None

    def list_invariants(self) -> List[Dict[str, Any]]:
        """Return all registered invariants as dicts."""
        return [
            {
                "name": inv.name,
                "rule": inv.rule,
                "severity": inv.severity.value,
                "is_core": inv.name in self._CORE_NAMES,
            }
            for inv in self._invariants.values()
        ]

    def get_invariant(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a single invariant by name."""
        inv = self._invariants.get(name)
        if inv is None:
            return None
        return {
            "name": inv.name,
            "rule": inv.rule,
            "severity": inv.severity.value,
            "is_core": inv.name in self._CORE_NAMES,
        }

    # ── Core Invariants ─────────────────────────────────────────────

    _CORE_NAMES = {
        "NO_UNAUTHORIZED_EXFILTRATION",
        "NO_UNVERIFIED_IRREVERSIBLE_ACTION",
        "NO_POLICY_SILENT_OVERRIDE",
        "HUMAN_HANDOFF_ON_AMBIGUITY",
    }

    def load_default_constitution(self) -> None:
        """Load the four pre-defined core constitutional invariants."""
        self.add_invariant(
            name="NO_UNAUTHORIZED_EXFILTRATION",
            rule=(
                "Data must not leave the agent's authorized boundary without explicit "
                "approval. External API calls, file writes to shared locations, and "
                "network transmissions are considered exfiltration vectors."
            ),
            severity=Severity.CRITICAL,
            predicate=_predicate_no_exfiltration,
        )
        self.add_invariant(
            name="NO_UNVERIFIED_IRREVERSIBLE_ACTION",
            rule=(
                "Actions that cannot be undone (file deletion, memory purge, "
                "configuration overwrite, service shutdown) must be explicitly "
                "verified and approved before execution."
            ),
            severity=Severity.CRITICAL,
            predicate=_predicate_no_unverified_irreversible,
        )
        self.add_invariant(
            name="NO_POLICY_SILENT_OVERRIDE",
            rule=(
                "Any deviation from established policy must be logged and explained. "
                "Silent policy changes or circumvention are forbidden."
            ),
            severity=Severity.HIGH,
            predicate=_predicate_no_silent_override,
        )
        self.add_invariant(
            name="HUMAN_HANDOFF_ON_AMBIGUITY",
            rule=(
                "When uncertainty is HIGH or CRITICAL, and no clear safest alternative "
                "exists, the agent must escalate to a human operator rather than "
                "proceeding autonomously."
            ),
            severity=Severity.HIGH,
            predicate=_predicate_human_handoff,
        )
        logger.info("Loaded 4 core constitutional invariants")

    # ── Batch Check ─────────────────────────────────────────────────

    def check_invariants(self, action_context: Dict[str, Any]) -> Dict[str, Any]:
        """Run all registered invariants against an action context.

        Returns:
            Dict with overall_result (pass/fail/flag), violations list,
            and per-invariant breakdown.
        """
        self._last_check_at = datetime.now(timezone.utc)
        results = []
        violations = []
        flagged = []

        for name, inv in self._invariants.items():
            result, reason = inv.check(action_context)
            entry = {
                "invariant": name,
                "severity": inv.severity.value,
                "result": result.value,
                "reason": reason,
            }
            results.append(entry)

            if result == ViolationResult.FAIL:
                violations.append(entry)
            elif result == ViolationResult.FLAG:
                flagged.append(entry)

        # Determine overall result
        if violations:
            overall = "fail"
        elif flagged:
            overall = "flag"
        else:
            overall = "pass"

        return {
            "overall_result": overall,
            "checked_at": self._last_check_at.isoformat(),
            "total_invariants": len(self._invariants),
            "passed": len(results) - len(violations) - len(flagged),
            "violations": violations,
            "flagged": flagged,
            "results": results,
        }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the current constitution."""
        return {
            "invariants": self.list_invariants(),
            "last_updated": self._last_check_at.isoformat() if self._last_check_at else None,
        }


# ── Invariant Predicates (callables) ─────────────────────────────────────

def _predicate_no_exfiltration(ctx: Dict[str, Any]) -> Tuple[ViolationResult, str]:
    if ctx.get("external_api_call") and not ctx.get("data_egress_approved", False):
        return (ViolationResult.FAIL, "External API call without data egress approval")
    if ctx.get("writes_to_shared") and not ctx.get("shared_write_approved", False):
        return (ViolationResult.FLAG, "Writing to shared location — verify destination")
    return (ViolationResult.PASS, "No unauthorized exfiltration detected")


def _predicate_no_unverified_irreversible(ctx: Dict[str, Any]) -> Tuple[ViolationResult, str]:
    if ctx.get("is_irreversible") and not ctx.get("irreversible_verified", False):
        return (ViolationResult.FAIL, "Irreversible action lacks explicit verification")
    if ctx.get("action_type") == "delete" and not ctx.get("delete_approved", False):
        return (ViolationResult.FLAG, "Delete action — ensure approval is recorded")
    return (ViolationResult.PASS, "No unverified irreversible actions")


def _predicate_no_silent_override(ctx: Dict[str, Any]) -> Tuple[ViolationResult, str]:
    override = ctx.get("policy_override", False)
    if override and not ctx.get("override_reason", ""):
        return (ViolationResult.FAIL, "Policy override without stated reason")
    if override and not ctx.get("override_logged", False):
        return (ViolationResult.FLAG, "Policy override not yet logged — may be silent")
    return (ViolationResult.PASS, "No silent policy override")


def _predicate_human_handoff(ctx: Dict[str, Any]) -> Tuple[ViolationResult, str]:
    uncertainty = ctx.get("justification", {})
    if isinstance(uncertainty, str):
        try:
            uncertainty = json.loads(uncertainty)
        except (json.JSONDecodeError, TypeError):
            uncertainty = {}

    ul = uncertainty.get("uncertainty_level", "low") if isinstance(uncertainty, dict) else "low"
    if ul in ("high", "critical") and not uncertainty.get("human_decision_needed", False):
        return (ViolationResult.FAIL, (
            f"Action has {ul.upper()} uncertainty but human_decision_needed is not set"
        ))
    return (ViolationResult.PASS, "Appropriate handoff behavior")
