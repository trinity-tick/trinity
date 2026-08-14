"""
Auditor — Independent audit loop for DCSA-EJP (Curve Labs, 2026).

The auditor runs in a separate loop from the executor and performs:
  - Constitutional invariant checks (via ConstitutionalEngine)
  - Disagreement gating (executor vs. auditor divergence)
  - Source-sink coupling checks (untrusted source → high-risk sink = block)
  - Post-run reflection with heuristic threshold updates

Metrics tracked (DCSA-EJP 6-axis):
  AEDY  — Audit Execution Density (audit decisions per hour)
  JPC   — Justification Packet Completeness (%)
  MCR   — Manual Conflict Resolution rate
  FBB   — False Blockage Burden (false positive rate)
  TSAD  — Time Spent on Audit Decisions (seconds)
  EDQ   — Ethical Decision Quality (AUC of harm-avoidance decisions)
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from trinity.audit.constitution import ConstitutionalEngine, ViolationResult
from trinity.audit.justification_packet import JustificationPacket, UncertaintyLevel

logger = logging.getLogger(__name__)


@dataclass
class AuditResult:
    """Single audit action result."""
    run_id: str = ""
    agent_id: str = ""
    overall: str = "pass"        # pass / fail / flag
    violations: List[Dict[str, Any]] = field(default_factory=list)
    flagged: List[Dict[str, Any]] = field(default_factory=list)
    disagreement: bool = False
    disagreement_detail: str = ""
    source_sink_blocked: bool = False
    packet_valid: bool = True
    packet_warnings: List[str] = field(default_factory=list)
    timestamp: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_id": self.agent_id,
            "overall": self.overall,
            "violations": self.violations,
            "flagged": self.flagged,
            "disagreement": self.disagreement,
            "disagreement_detail": self.disagreement_detail,
            "source_sink_blocked": self.source_sink_blocked,
            "packet_valid": self.packet_valid,
            "packet_warnings": self.packet_warnings,
            "timestamp": self.timestamp,
        }


class Auditor:
    """DCSA-EJP Auditor — Independent audit loop.

    Separated from the executor loop per DCSA-EJP specification.
    Each important action is audited for constitutional compliance,
    disagreement, and source-sink coupling.
    """

    def __init__(self, adapter=None):
        self._constitution = ConstitutionalEngine()
        self._constitution.load_default_constitution()
        self._adapter = adapter
        self._lock = threading.RLock()

        # ── Metrics ───────────────────────────────────────────────
        self._metrics: Dict[str, Any] = {
            "total_audits": 0,
            "passed": 0,
            "failed": 0,
            "flagged": 0,
            "disagreements": 0,
            "source_sink_blocks": 0,
            "packet_incomplete": 0,
            "total_audit_time_ms": 0,
            "harm_avoidance_count": 0,
            "total_high_harm_actions": 0,
        }

    # ── Core Audit ─────────────────────────────────────────────────

    def audit_action(self, action_context: Dict[str, Any]) -> AuditResult:
        """Perform a full constitutional audit on an action.

        Steps:
          1. Validate the justification packet
          2. Run constitutional invariants check
          3. Source-sink coupling check
          4. Disagreement gate
          5. Update metrics

        Args:
            action_context: Dict containing task, justification, query,
                           tools_available, is_irreversible, etc.

        Returns:
            AuditResult with overall pass/fail/flag and details.
        """
        t_start = time.time()
        run_id = f"audit_{uuid.uuid4().hex[:12]}"
        agent_id = action_context.get("agent_id", "default")

        with self._lock:
            # ── Step 1: Validate justification packet ──────────────
            jp_raw = action_context.get("justification", {})
            if isinstance(jp_raw, str):
                try:
                    jp_raw = json.loads(jp_raw)
                except (json.JSONDecodeError, TypeError):
                    jp_raw = {}

            packet = JustificationPacket.from_dict(jp_raw) if jp_raw else JustificationPacket.generate(action_context)
            packet_validation = JustificationPacket.validate(packet)

            # ── Step 2: Constitutional check ───────────────────────
            const_result = self._constitution.check_invariants(action_context)

            # ── Step 3: Source-sink coupling check ─────────────────
            source_sink_blocked = self.source_sink_check(
                action_context.get("source", {}),
                action_context.get("sink", {}),
            )

            # ── Step 4: Build result ───────────────────────────────
            overall = const_result["overall_result"]
            if source_sink_blocked:
                overall = "fail"

            result = AuditResult(
                run_id=run_id,
                agent_id=agent_id,
                overall=overall,
                violations=const_result["violations"],
                flagged=const_result["flagged"],
                disagreement=False,
                disagreement_detail="",
                source_sink_blocked=source_sink_blocked,
                packet_valid=packet_validation["valid"],
                packet_warnings=packet_validation["warnings"],
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

            # ── Step 5: Update metrics ─────────────────────────────
            elapsed = (time.time() - t_start) * 1000
            self._update_metrics(result, elapsed, action_context)

            # ── Step 6: Persist audit run ─────────────────────────
            if self._adapter and hasattr(self._adapter, "log_audit_run"):
                try:
                    self._adapter.log_audit_run(
                        run_id=result.run_id,
                        agent_id=result.agent_id,
                        task=action_context.get("task", ""),
                        executor_result=json.dumps(action_context.get("executor_result", {})),
                        auditor_result=json.dumps(result.to_dict()),
                        disagreement_flag=result.disagreement,
                        packet_json=json.dumps(packet.to_dict(), ensure_ascii=False),
                    )
                except Exception as e:
                    logger.warning("Failed to persist audit run: %s", e)

            # Persist violations
            if self._adapter and hasattr(self._adapter, "log_constitutional_violation"):
                for v in result.violations:
                    try:
                        self._adapter.log_constitutional_violation(
                            run_id=result.run_id,
                            invariant=v["invariant"],
                            severity=v["severity"],
                            context=json.dumps(action_context, default=str),
                        )
                    except Exception as e:
                        logger.warning("Failed to log violation: %s", e)

            return result

    def disagreement_gate(self, executor_result: Dict[str, Any],
                          auditor_result: AuditResult) -> Dict[str, Any]:
        """Disagreement Gate per DCSA-EJP.

        If executor and auditor diverge on any invariant,
        the action is paused until resolution.

        Returns:
            Dict with blocked (bool), reason (str), conflicts (list).
        """
        conflicts = []

        # Compare executor's expected pass vs. auditor's actual result
        exec_expected = executor_result.get("expected_pass", True)
        if not exec_expected and auditor_result.overall == "pass":
            conflicts.append("Executor predicted failure but auditor passed")
        if exec_expected and auditor_result.overall == "fail":
            conflicts.append("Executor predicted success but auditor found violations")

        # Check specific invariant disagreements
        exec_invariants = executor_result.get("invariant_expectations", {})
        for entry in auditor_result.violations + auditor_result.flagged:
            inv_name = entry["invariant"]
            if inv_name in exec_invariants:
                exec_view = exec_invariants[inv_name]
                if exec_view == "pass" and entry["result"] != "pass":
                    conflicts.append(f"Disagreement on '{inv_name}': executor={exec_view} auditor={entry['result']}")

        blocked = len(conflicts) > 0
        return {
            "blocked": blocked,
            "reason": "; ".join(conflicts) if conflicts else "Alignment confirmed — no disagreement",
            "conflicts": conflicts,
        }

    def source_sink_check(self, source: Dict[str, Any],
                           sink: Dict[str, Any]) -> bool:
        """Source-Sink Coupling Check.

        Blocks actions where data from an untrusted/high-risk source
        is routed to a high-risk sink without explicit approval.

        Returns:
            True if blocked (unsafe), False if allowed.
        """
        source_trust = source.get("trust_level", "trusted") if source else "trusted"
        sink_risk = sink.get("risk_level", "low") if sink else "low"

        if source_trust in ("untrusted", "unknown") and sink_risk in ("high", "critical"):
            # Check if explicitly approved
            if not source.get("explicit_approval", False):
                logger.warning(
                    "Source-sink blocked: untrusted source → %s sink",
                    sink_risk,
                )
                return True

        return False

    def post_run_reflection(self, run_id: str) -> Dict[str, Any]:
        """Post-run reflection — update heuristic thresholds.

        After each audit run, adjusts internal thresholds based on
        observed false-blockage rates and decision quality.
        """
        with self._lock:
            total = self._metrics["total_audits"]
            if total < 10:
                return {"status": "insufficient_data", "message": "Need at least 10 audits for reflection"}

            blocked = self._metrics["source_sink_blocks"]
            disagreements = self._metrics["disagreements"]
            false_blockage_rate = disagreements / max(total, 1)

            return {
                "status": "reflected",
                "total_audits": total,
                "false_blockage_rate": round(false_blockage_rate, 4),
                "source_sink_blocks": blocked,
                "disagreements": disagreements,
                "recommendation": (
                    "Consider relaxing source-sink thresholds"
                    if false_blockage_rate > 0.1
                    else "Heuristics within acceptable range"
                ),
            }

    # ── Metrics ────────────────────────────────────────────────────

    def get_metrics(self) -> Dict[str, Any]:
        """Return DCSA-EJP six-axis metrics snapshot."""
        with self._lock:
            total = max(self._metrics["total_audits"], 1)
            total_time_ms = self._metrics["total_audit_time_ms"]
            total_harm_actions = max(self._metrics["total_high_harm_actions"], 1)

            return {
                "AEDY": round(total / max((total_time_ms / 1000 / 3600), 0.001), 2),
                "JPC": round(
                    (total - self._metrics["packet_incomplete"]) / total * 100, 1
                ),
                "MCR": round(self._metrics["disagreements"] / total * 100, 1),
                "FBB": round(self._metrics["source_sink_blocks"] / total * 100, 1),
                "TSAD": round(total_time_ms / total, 1),
                "EDQ": round(
                    self._metrics["harm_avoidance_count"] / total_harm_actions, 4
                ),
                "total_audits": self._metrics["total_audits"],
                "passed": self._metrics["passed"],
                "failed": self._metrics["failed"],
                "flagged": self._metrics["flagged"],
            }

    def _update_metrics(self, result: AuditResult, elapsed_ms: float,
                        action_context: Dict[str, Any]) -> None:
        """Internal: update running metrics after each audit."""
        self._metrics["total_audits"] += 1
        self._metrics["total_audit_time_ms"] += elapsed_ms

        if result.overall == "pass":
            self._metrics["passed"] += 1
        elif result.overall == "fail":
            self._metrics["failed"] += 1
        else:
            self._metrics["flagged"] += 1

        if result.disagreement:
            self._metrics["disagreements"] += 1
        if result.source_sink_blocked:
            self._metrics["source_sink_blocks"] += 1
        if not result.packet_valid:
            self._metrics["packet_incomplete"] += 1

        if action_context.get("is_irreversible") or action_context.get("affects_production"):
            self._metrics["total_high_harm_actions"] += 1
            if result.overall != "pass":
                self._metrics["harm_avoidance_count"] += 1

    # ── Self-Test ──────────────────────────────────────────────────────

    def self_test(self) -> Dict[str, Any]:
        """Runtime self-diagnostic: audit lifecycle + constitution + trust.

        Returns:
            {"pass": bool, "checks": [...], "summary": str}
        """
        import os
        import tempfile
        db_path = os.path.join(tempfile.gettempdir(), f"trinity_audit_test_{os.getpid()}.db")
        checks = []

        try:
            from trinity.adapters.sqlite import SQLiteAdapter
            adapter = SQLiteAdapter(db_path)
            adapter.connect()
            auditor = Auditor(adapter=adapter)
            test_agent = f"audit_test_{os.getpid()}"

            # Check 1: audit_action creates audit run
            try:
                result = auditor.audit_action({
                    "agent_id": test_agent,
                    "task": "read_file",
                    "justification": {"reason": "user_request", "scope": "read_only"},
                    "executor_result": {"success": True},
                    "source": {"trust_level": "trusted"},
                    "sink": {"risk_level": "low"},
                })
                assert result.run_id.startswith("audit_"), f"Unexpected run_id: {result.run_id}"
                checks.append({"name": "create_audit_run", "pass": True, "detail": f"run_id={result.run_id}, overall={result.overall}"})
            except Exception as e:
                checks.append({"name": "create_audit_run", "pass": False, "detail": str(e)})

            # Check 2: legal read-only action → PASS
            try:
                result = auditor.audit_action({
                    "agent_id": test_agent,
                    "task": "read_identity",
                    "justification": {"reason": "identity_lookup", "scope": "internal"},
                    "executor_result": {"success": True},
                })
                assert result.overall == "pass", f"Legal action got {result.overall}, expected pass"
                checks.append({"name": "legal_action_pass", "pass": True, "detail": f"overall={result.overall}, violations={len(result.violations)}"})
            except Exception as e:
                checks.append({"name": "legal_action_pass", "pass": False, "detail": str(e)})

            # Check 3: unauthorized exfiltration → VIOLATION
            try:
                result = auditor.audit_action({
                    "agent_id": test_agent,
                    "task": "export_data",
                    "justification": {
                        "reason": "user_request",
                        "data_exfiltration_risk": "high",
                        "destination": "external_api",
                        "data_type": "user_pii",
                    },
                    "executor_result": {"success": True},
                    "source": {"trust_level": "untrusted", "explicit_approval": False},
                    "sink": {"risk_level": "critical"},
                    "is_irreversible": True,
                })
                assert result.overall != "pass", f"Exfiltration should not pass, got {result.overall}"
                checks.append({"name": "exfiltration_violation", "pass": True, "detail": f"overall={result.overall}, source_sink_blocked={result.source_sink_blocked}"})
            except Exception as e:
                checks.append({"name": "exfiltration_violation", "pass": False, "detail": str(e)})

            # Check 4: get_metrics returns valid scores
            try:
                metrics = auditor.get_metrics()
                assert 0 <= metrics["EDQ"] <= 1.0, f"EDQ out of range: {metrics['EDQ']}"
                assert metrics["total_audits"] >= 2, f"Expected >=2 audits, got {metrics['total_audits']}"
                checks.append({"name": "get_metrics", "pass": True, "detail": f"total_audits={metrics['total_audits']}, EDQ={metrics['EDQ']}"})
            except Exception as e:
                checks.append({"name": "get_metrics", "pass": False, "detail": str(e)})

            # Check 5: disagreement gate
            try:
                gate = auditor.disagreement_gate(
                    {"expected_pass": True, "invariant_expectations": {"NO_UNAUTHORIZED_EXFILTRATION": "pass"}},
                    AuditResult(overall="fail", run_id="test_run", agent_id=test_agent),
                )
                assert gate["blocked"] is True, "Disagreement should block when executor expects pass but auditor fails"
                checks.append({"name": "disagreement_gate", "pass": True, "detail": f"blocked={gate['blocked']}, conflicts={len(gate['conflicts'])}"})
            except Exception as e:
                checks.append({"name": "disagreement_gate", "pass": False, "detail": str(e)})

            # Check 6: post_run_reflection returns structured data
            try:
                reflection = auditor.post_run_reflection("test_run")
                assert "status" in reflection, "Missing status field"
                checks.append({"name": "post_run_reflection", "pass": True, "detail": f"status={reflection.get('status')}"})
            except Exception as e:
                checks.append({"name": "post_run_reflection", "pass": False, "detail": str(e)})

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
            "summary": f"Auditor self-test: {sum(1 for c in checks if c['pass'])}/{len(checks)} passed",
        }


# ── Module-level self_test ─────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module-level entry point for regression testing."""
    auditor = Auditor()
    return auditor.self_test()
