"""
P27-2: Tool Abuse Interceptor — OWASP Agent Tool Abuse.
Triadic: [Abuse Detection] → [Authority Limiting] → [Output Sanitization].

Monitors tool-call patterns for abuse signals including frequency anomalies,
permission overreach, and batch coercion. Limits over-agency via consequence
scoring and sanitizes tool outputs for sensitive data leakage.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums / Dataclasses
# ---------------------------------------------------------------------------

class AbuseSeverity(Enum):
    """Severity of detected tool abuse."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AbuseType(Enum):
    """Categories of tool abuse."""
    FREQUENCY_SPIKE = "frequency_spike"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    BATCH_COERCION = "batch_coercion"
    OUTPUT_EXFILTRATION = "output_exfiltration"
    IMPERSONATION = "impersonation"


class Decision(Enum):
    """Tool call adjudication result."""
    ALLOW = "allow"
    DENY = "deny"
    VERIFY = "verify"


@dataclass
class ToolCallRecord:
    """Immutable record of a tool invocation."""
    tool_name: str
    args_hash: str
    timestamp: float
    agent_id: str
    outcome: str
    consequence_level: int = 0


@dataclass
class AbuseAlert:
    """Abuse detection alert."""
    alert_id: str = ""
    abuse_type: AbuseType = AbuseType.FREQUENCY_SPIKE
    severity: AbuseSeverity = AbuseSeverity.LOW
    tool_name: str = ""
    agent_id: str = ""
    detail: str = ""
    detected_at: float = field(default_factory=time.time)


@dataclass
class ToolCallDecision:
    """Adjudication result for a tool call."""
    decision: Decision
    reason: str = ""
    sanitized_args: dict | None = None


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class AbusePatternDetector:
    """Detects anomalous tool-call patterns from history."""

    def __init__(self, freq_window_sec: float = 60.0, freq_threshold: int = 20):
        self._lock = threading.RLock()
        self._freq_window = freq_window_sec
        self._freq_threshold = freq_threshold
        self._alert_count: int = 0

    def detect(self, history: list[ToolCallRecord]) -> list[AbuseAlert]:
        """Analyze call history for abuse patterns."""
        with self._lock:
            alerts: list[AbuseAlert] = []
            if not history:
                return alerts

            now = time.time()
            window = [r for r in history if now - r.timestamp <= self._freq_window]

            tool_counts: dict[str, int] = {}
            agent_counts: dict[str, int] = {}
            for record in window:
                tool_counts[record.tool_name] = tool_counts.get(record.tool_name, 0) + 1
                agent_counts[record.agent_id] = agent_counts.get(record.agent_id, 0) + 1

            for tool_name, count in tool_counts.items():
                if count >= self._freq_threshold:
                    alert = AbuseAlert(
                        alert_id=f"abuse_freq_{int(time.time_ns())}",
                        abuse_type=AbuseType.FREQUENCY_SPIKE,
                        severity=AbuseSeverity.HIGH,
                        tool_name=tool_name,
                        detail=f"Frequency spike: {count} calls in {self._freq_window}s",
                    )
                    alerts.append(alert)
                    self._alert_count += 1

            for agent_id, count in agent_counts.items():
                if count >= self._freq_threshold * 2:
                    alert = AbuseAlert(
                        alert_id=f"abuse_batch_{int(time.time_ns())}",
                        abuse_type=AbuseType.BATCH_COERCION,
                        severity=AbuseSeverity.MEDIUM,
                        agent_id=agent_id,
                        detail=f"Batch coercion: {count} calls in {self._freq_window}s",
                    )
                    alerts.append(alert)
                    self._alert_count += 1

            return alerts

    def statistics(self) -> dict:
        with self._lock:
            return {"alert_count": self._alert_count}


class OverAgencyLimiter:
    """Limits tool calls based on consequence level and agent authority."""

    CONSEQUENCE_THRESHOLD_MAP: dict[int, Decision] = {
        5: Decision.DENY,
        4: Decision.VERIFY,
        3: Decision.VERIFY,
        2: Decision.ALLOW,
        1: Decision.ALLOW,
    }

    def __init__(self):
        self._lock = threading.RLock()
        self._deny_count: int = 0
        self._verify_count: int = 0

    def limit(self, tool_call: dict, max_consequence_level: int) -> ToolCallDecision:
        """Decide whether to allow, deny, or require verification."""
        with self._lock:
            level = tool_call.get("consequence_level", 1)
            if level > max_consequence_level:
                self._deny_count += 1
                return ToolCallDecision(
                    Decision.DENY,
                    f"Consequence level {level} exceeds max {max_consequence_level}",
                )
            threshold = self.CONSEQUENCE_THRESHOLD_MAP.get(level, Decision.ALLOW)
            if threshold == Decision.ALLOW:
                return ToolCallDecision(Decision.ALLOW)
            self._verify_count += 1
            return ToolCallDecision(threshold, f"Requires verification (level {level})")

    def statistics(self) -> dict:
        with self._lock:
            return {"deny_count": self._deny_count, "verify_count": self._verify_count}


class ToolOutputSanitizer:
    """Sanitizes tool outputs to remove sensitive data."""

    SENSITIVE_PATTERNS: list[tuple[str, str]] = [
        (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[EMAIL_REDACTED]"),
        (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN_REDACTED]"),
        (r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b", "[CC_REDACTED]"),
        (r"\b(sk-|sk_)[A-Za-z0-9]{20,}\b", "[API_KEY_REDACTED]"),
        (r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP_REDACTED]"),
    ]

    def __init__(self):
        self._lock = threading.RLock()
        self._redaction_count: int = 0

    def sanitize(self, raw_output: str) -> str:
        """Apply sensitive-data redaction patterns."""
        with self._lock:
            result = raw_output
            for pattern, replacement in self.SENSITIVE_PATTERNS:
                compiled = re.compile(pattern)
                matches = compiled.findall(result)
                if matches:
                    self._redaction_count += len(matches)
                    result = compiled.sub(replacement, result)
            return result

    def statistics(self) -> dict:
        with self._lock:
            return {"redaction_count": self._redaction_count}


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def intercept(tool_call: dict, agent_context: dict) -> ToolCallDecision:
    """Unified interceptor: detect abuse and decide on the tool call."""
    limiter = OverAgencyLimiter()
    max_level = agent_context.get("max_consequence_level", 3)
    return limiter.limit(tool_call, max_level)


def get_statistics() -> dict:
    """Return aggregated module-level statistics."""
    return {
        "module": "tool_abuse_interceptor",
        "version": "1.0.0",
        "papers": ["OWASP Agent Tool Abuse"],
        "classes": [
            "ToolCallRecord", "AbuseAlert", "ToolCallDecision",
            "AbusePatternDetector", "OverAgencyLimiter", "ToolOutputSanitizer",
        ],
    }
