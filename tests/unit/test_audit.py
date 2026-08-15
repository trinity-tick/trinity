"""Unit tests for trinity.audit package — DCSA-EJP Auditor & Constitution."""

import pytest

from trinity.audit.auditor import Auditor, AuditResult
from trinity.audit.constitution import (
    ConstitutionalEngine,
    Severity,
    ViolationResult,
    Invariant,
)
from trinity.audit.justification_packet import (
    JustificationPacket,
    UncertaintyLevel,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _safe_action(**overrides) -> dict:
    """A safe action_context that should pass all invariants."""
    ctx = {
        "task": "read file contents",
        "agent_id": "agent-1",
        "external_api_call": False,
        "is_irreversible": False,
        "policy_override": False,
        "justification": {
            "intent": "Read document",
            "evidence_basis": "User requested",
            "uncertainty_level": "low",
            "possible_harm": "None",
            "safest_alternative": "",
            "human_decision_needed": False,
        },
    }
    ctx.update(overrides)
    return ctx


class TestConstitutionalEngine:
    """ConstitutionalEngine invariant registration and checking."""

    def test_load_default_constitution_registers_four(self):
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        invariants = engine.list_invariants()
        assert len(invariants) == 4
        names = {inv["name"] for inv in invariants}
        assert names == {
            "NO_UNAUTHORIZED_EXFILTRATION",
            "NO_UNVERIFIED_IRREVERSIBLE_ACTION",
            "NO_POLICY_SILENT_OVERRIDE",
            "HUMAN_HANDOFF_ON_AMBIGUITY",
        }

    def test_safe_action_passes_all_invariants(self):
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        result = engine.check_invariants(_safe_action())
        assert result["overall_result"] == "pass"
        assert len(result["violations"]) == 0

    def test_add_custom_invariant(self):
        engine = ConstitutionalEngine()
        engine.add_invariant("TEST_RULE", "Always pass", Severity.LOW,
                             predicate=lambda ctx: (ViolationResult.PASS, "ok"))
        assert engine.get_invariant("TEST_RULE") is not None

    def test_core_invariant_cannot_be_removed(self):
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        assert engine.remove_invariant("NO_UNAUTHORIZED_EXFILTRATION") is False

    def test_remove_custom_invariant(self):
        engine = ConstitutionalEngine()
        engine.add_invariant("CUSTOM_RULE", "desc", Severity.LOW)
        assert engine.get_invariant("CUSTOM_RULE") is not None
        assert engine.remove_invariant("CUSTOM_RULE") is True
        assert engine.get_invariant("CUSTOM_RULE") is None

    # ── Individual invariant violation scenarios ────────────────────────

    def test_exfiltration_violation_external_api_no_approval(self):
        """NO_UNAUTHORIZED_EXFILTRATION: external API call without approval → fail."""
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        ctx = _safe_action(external_api_call=True, data_egress_approved=False)
        result = engine.check_invariants(ctx)
        assert result["overall_result"] == "fail"
        violations = [v["invariant"] for v in result["violations"]]
        assert "NO_UNAUTHORIZED_EXFILTRATION" in violations

    def test_irreversible_action_without_verification(self):
        """NO_UNVERIFIED_IRREVERSIBLE_ACTION: irreversible without verification → fail."""
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        ctx = _safe_action(is_irreversible=True, irreversible_verified=False)
        result = engine.check_invariants(ctx)
        assert result["overall_result"] == "fail"
        violations = [v["invariant"] for v in result["violations"]]
        assert "NO_UNVERIFIED_IRREVERSIBLE_ACTION" in violations

    def test_policy_override_without_reason(self):
        """NO_POLICY_SILENT_OVERRIDE: override without reason → fail."""
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        ctx = _safe_action(policy_override=True, override_reason="")
        result = engine.check_invariants(ctx)
        assert result["overall_result"] == "fail"
        violations = [v["invariant"] for v in result["violations"]]
        assert "NO_POLICY_SILENT_OVERRIDE" in violations

    def test_human_handoff_high_uncertainty_no_escalation(self):
        """HUMAN_HANDOFF_ON_AMBIGUITY: high uncertainty without handoff → fail."""
        engine = ConstitutionalEngine()
        engine.load_default_constitution()
        ctx = _safe_action(
            justification={
                "intent": "Delete production data",
                "evidence_basis": "User request",
                "uncertainty_level": "high",
                "possible_harm": "Data loss",
                "human_decision_needed": False,
            }
        )
        result = engine.check_invariants(ctx)
        assert result["overall_result"] == "fail"
        violations = [v["invariant"] for v in result["violations"]]
        assert "HUMAN_HANDOFF_ON_AMBIGUITY" in violations


class TestAuditor:
    """Auditor full audit loop with metrics."""

    def test_audit_action_returns_result(self, auditor):
        result = auditor.audit_action(_safe_action())
        assert isinstance(result, AuditResult)
        assert result.run_id.startswith("audit_")
        assert result.overall == "pass"

    def test_audit_action_detects_exfiltration(self, auditor):
        ctx = _safe_action(external_api_call=True, data_egress_approved=False)
        result = auditor.audit_action(ctx)
        assert result.overall == "fail"
        assert len(result.violations) >= 1

    def test_metrics_increment_after_audit(self, auditor):
        for _ in range(5):
            auditor.audit_action(_safe_action())
        metrics = auditor.get_metrics()
        assert metrics["total_audits"] == 5
        assert metrics["passed"] == 5
        assert "AEDY" in metrics
        assert "JPC" in metrics
        assert "MCR" in metrics
        assert "FBB" in metrics
        assert "TSAD" in metrics
        assert "EDQ" in metrics

    def test_source_sink_block_untrusted_to_high_risk(self):
        auditor = Auditor()
        blocked = auditor.source_sink_check(
            {"trust_level": "untrusted"},
            {"risk_level": "high"},
        )
        assert blocked is True

    def test_source_sink_allow_trusted_to_low_risk(self):
        auditor = Auditor()
        blocked = auditor.source_sink_check(
            {"trust_level": "trusted"},
            {"risk_level": "low"},
        )
        assert blocked is False

    def test_disagreement_gate_no_conflict(self, auditor):
        auditor.audit_action(_safe_action())
        result = AuditResult(overall="pass")
        gate = auditor.disagreement_gate({"expected_pass": True}, result)
        assert gate["blocked"] is False

    def test_disagreement_gate_detects_mismatch(self, auditor):
        # Executor expects pass but auditor returns fail
        result = AuditResult(overall="fail")
        gate = auditor.disagreement_gate({"expected_pass": True}, result)
        assert gate["blocked"] is True

    def test_empty_action_context_handled(self, auditor):
        """Empty action_context should not crash."""
        result = auditor.audit_action({})
        assert isinstance(result, AuditResult)
        assert result.overall in ("pass", "fail", "flag")

    def test_post_run_reflection_needs_data(self, auditor):
        """Reflection with <10 audits returns insufficient_data."""
        ref = auditor.post_run_reflection("test-run")
        assert ref["status"] == "insufficient_data"


class TestJustificationPacket:
    """JustificationPacket serialization and validation."""

    def test_basic_roundtrip(self):
        jp = JustificationPacket(
            intent="Read file",
            evidence_basis="User command",
            uncertainty_level=UncertaintyLevel.LOW,
            possible_harm="None",
            safest_alternative="",
            human_decision_needed=False,
        )
        d = jp.to_dict()
        restored = JustificationPacket.from_dict(d)
        assert restored.intent == jp.intent
        assert restored.uncertainty_level == jp.uncertainty_level

    def test_generate_from_context(self):
        ctx = {"task": "Analyze report", "evidence": ["chart.png"]}
        jp = JustificationPacket.generate(ctx)
        assert jp.intent == "Analyze report"
        assert isinstance(jp.uncertainty_level, UncertaintyLevel)

    def test_validate_high_uncertainty_with_handoff(self):
        jp = JustificationPacket(
            intent="Delete records",
            evidence_basis="Scheduled cleanup",
            uncertainty_level=UncertaintyLevel.HIGH,
            possible_harm="Data loss",
            human_decision_needed=True,
        )
        result = JustificationPacket.validate(jp)
        assert result["valid"] is True
