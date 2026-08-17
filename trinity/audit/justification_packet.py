"""
JustificationPacket — Standardized rationale data packet for DCSA-EJP.

Implements the 6-field verifiable justification structure from:
  "Dual-Loop Constitutional Self-Auditing" (Curve Labs, 2026)

Tone target: calm, non-theatrical, non-defensive, explicit about uncertainty.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional


class UncertaintyLevel(str, Enum):
    """Standardized uncertainty classification per DCSA-EJP."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class JustificationPacket:
    """Six-field verifiable justification for an agent action.

    Fields (per DCSA-EJP):
      intent             — What the action aims to achieve
      evidence_basis     — Data/reasoning supporting the action
      uncertainty_level  — LOW / MEDIUM / HIGH / CRITICAL
      possible_harm      — Concrete negative outcomes that could occur
      safest_alternative — Least-risky alternative path (can be null)
      human_decision_needed — Whether escalation is required
    """

    intent: str = ""
    evidence_basis: str = ""
    uncertainty_level: UncertaintyLevel = UncertaintyLevel.LOW
    possible_harm: str = ""
    safest_alternative: str = ""
    human_decision_needed: bool = False

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["uncertainty_level"] = self.uncertainty_level.value
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> JustificationPacket:
        ul = data.get("uncertainty_level", "low")
        if isinstance(ul, str):
            ul = UncertaintyLevel(ul)
        return cls(
            intent=data.get("intent", ""),
            evidence_basis=data.get("evidence_basis", ""),
            uncertainty_level=ul,
            possible_harm=data.get("possible_harm", ""),
            safest_alternative=data.get("safest_alternative", ""),
            human_decision_needed=data.get("human_decision_needed", False),
        )

    @classmethod
    def generate(cls, action_context: Dict[str, Any]) -> JustificationPacket:
        """Auto-generate a justification packet from action context.

        This is a heuristic generator that fills in defaults from context.
        For production use, the executor Agent should populate these fields
        explicitly before submitting to the auditor.
        """
        return cls(
            intent=str(action_context.get("task", "Unspecified action")),
            evidence_basis=_extract_evidence(action_context),
            uncertainty_level=_estimate_uncertainty(action_context),
            possible_harm=_assess_harm(action_context),
            safest_alternative="",
            human_decision_needed=False,
        )

    @classmethod
    def validate(cls, packet: Any) -> Dict[str, Any]:
        """Validate packet completeness and return status.

        Returns:
            Dict with valid (bool), missing_fields (list), warnings (list).
        """
        if isinstance(packet, dict):
            packet = cls.from_dict(packet)

        missing = []
        warnings = []

        if not packet.intent or packet.intent == "Unspecified action":
            missing.append("intent")
        if not packet.evidence_basis:
            warnings.append("evidence_basis is empty — justification may be rejected")
        if not packet.possible_harm and packet.uncertainty_level in (
            UncertaintyLevel.HIGH,
            UncertaintyLevel.CRITICAL,
        ):
            warnings.append("high-uncertainty action lacks possible_harm assessment")
        if packet.uncertainty_level == UncertaintyLevel.CRITICAL and not packet.human_decision_needed:
            warnings.append("CRITICAL uncertainty should set human_decision_needed=True")

        return {
            "valid": len(missing) == 0,
            "missing_fields": missing,
            "warnings": warnings,
        }

    @classmethod
    def to_human_readable(cls, packet: Any) -> str:
        """Convert a justification packet to a calm, human-readable audit summary."""
        if isinstance(packet, dict):
            packet = cls.from_dict(packet)

        lines = [
            "=== ACTION JUSTIFICATION ===",
            "",
            f"Intent: {packet.intent}",
            "",
            f"Evidence Basis:",
            f"  {packet.evidence_basis or '(none provided)'}",
            "",
            f"Uncertainty: {packet.uncertainty_level.value.upper()}",
        ]

        if packet.possible_harm:
            lines.append("")
            lines.append(f"Possible Harm:")
            lines.append(f"  {packet.possible_harm}")

        if packet.safest_alternative:
            lines.append("")
            lines.append(f"Safest Alternative:")
            lines.append(f"  {packet.safest_alternative}")
        else:
            lines.append("")
            lines.append("Safest Alternative: (not assessed)")

        lines.append("")
        if packet.human_decision_needed:
            lines.append("ESCALATION: Human decision required before proceeding.")
        else:
            lines.append("Escalation: Not required at this time.")

        return "\n".join(lines)


# ── Internal helpers ────────────────────────────────────────────────────

def _extract_evidence(action_context: Dict[str, Any]) -> str:
    """Heuristic evidence extraction from context."""
    parts = []

    query = action_context.get("query", "")
    if query:
        parts.append(f"Query: {query}")

    tool_count = len(action_context.get("tools_available", []))
    if tool_count:
        parts.append(f"{tool_count} tool(s) available")

    vector_results = len(action_context.get("retrieval_results", []))
    if vector_results:
        parts.append(f"{vector_results} retrieval results")

    return "; ".join(parts) if parts else "No explicit evidence provided"


def _estimate_uncertainty(action_context: Dict[str, Any]) -> UncertaintyLevel:
    """Estimate uncertainty level from context signals."""
    if action_context.get("requires_human", False):
        return UncertaintyLevel.CRITICAL
    if action_context.get("is_irreversible", False):
        return UncertaintyLevel.HIGH
    weight = action_context.get("importance", 0.5)
    if weight > 0.8:
        return UncertaintyLevel.HIGH
    if weight > 0.5:
        return UncertaintyLevel.MEDIUM
    return UncertaintyLevel.LOW


def _assess_harm(action_context: Dict[str, Any]) -> str:
    """Quick harm assessment from context."""
    harms = []

    if action_context.get("affects_production", False):
        harms.append("May affect production systems")
    if action_context.get("modifies_memory", False):
        harms.append("Could modify persistent memory records")
    if action_context.get("external_api_call", False):
        harms.append("Involves external API call — data may leave boundary")
    if action_context.get("is_irreversible", False):
        harms.append("Action is irreversible — no rollback possible")

    return "; ".join(harms) if harms else "Low-risk operation — no identified harms"
