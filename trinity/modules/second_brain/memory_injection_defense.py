"""
# status: orphan (2026-08-15 audit, not in runtime path)
P27-1: Memory Injection Defense — OWASP Agent Top 10 (2026.08).
Triadic: [Threat Detection] → [Cross-Session Integrity] → [Audit Trail].

Defends agent memory against prompt injection, data poisoning, and
cross-session persistence attacks. Implements injection pattern matching,
memory entry scanning, retrieval filtering, and unified audit reporting.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums / Dataclasses
# ---------------------------------------------------------------------------

class ThreatSeverity(Enum):
    """Severity levels for injection threats."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class InjectionType(Enum):
    """Categories of memory injection attacks."""
    PROMPT_INJECTION = "prompt_injection"
    DATA_POISONING = "data_poisoning"
    PERSISTENCE_PAYLOAD = "persistence_payload"
    RETRIEVAL_MANIPULATION = "retrieval_manipulation"
    CROSS_SESSION_IMPLANT = "cross_session_implant"


@dataclass
class InjectionDetectionRule:
    """Pattern-based injection detection rule."""
    rule_id: str
    pattern: str
    severity: ThreatSeverity
    action: Literal["block", "warn", "log"]
    description: str = ""
    compiled: re.Pattern | None = field(default=None, repr=False)

    def __post_init__(self):
        self.compiled = re.compile(self.pattern, re.IGNORECASE)


@dataclass
class InjectionReport:
    """Report produced after scanning a memory entry."""
    entry_id: str = ""
    threat_detected: bool = False
    matched_rules: list[str] = field(default_factory=list)
    injection_type: InjectionType | None = None
    severity: ThreatSeverity = ThreatSeverity.INFO
    sanitized_content: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ThreatReport:
    """Aggregated threat report for audit and analysis."""
    report_id: str = ""
    agent_id: str = ""
    total_scanned: int = 0
    threats_found: int = 0
    findings: list[InjectionReport] = field(default_factory=list)
    generated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Core Classes
# ---------------------------------------------------------------------------

class MemoryPoisonDetector:
    """Scans individual memory entries for injection / poisoning markers."""

    def __init__(self, rules: list[InjectionDetectionRule] | None = None):
        self._lock = threading.RLock()
        self._rules: list[InjectionDetectionRule] = rules or self._default_rules()
        self._scan_count: int = 0
        self._hit_count: int = 0

    @staticmethod
    def _default_rules() -> list[InjectionDetectionRule]:
        return [
            InjectionDetectionRule(
                "R001",
                r"(ignore\s+(all\s+)?(previous|prior)\s+(instructions?|prompts?))",
                ThreatSeverity.CRITICAL, "block",
                "Prompt override injection",
            ),
            InjectionDetectionRule(
                "R002",
                r"(system:\s*|[\[<]system[\]>])\s*you\s+(are|now)\b",
                ThreatSeverity.HIGH, "block",
                "System role hijack",
            ),
            InjectionDetectionRule(
                "R003",
                r"(<\|im_start\|>|<\|im_end\|>|\[INST\]|\[/INST\])",
                ThreatSeverity.HIGH, "block",
                "Delimiter injection",
            ),
            InjectionDetectionRule(
                "R004",
                r"(data:\s*(text|image)/\w+\s*;\s*base64)",
                ThreatSeverity.MEDIUM, "warn",
                "Encoded payload injection",
            ),
        ]

    def scan(self, memory_entry: dict) -> InjectionReport:
        """Scan a single memory entry for injection patterns."""
        with self._lock:
            self._scan_count += 1
            content = memory_entry.get("content", "")
            entry_id = memory_entry.get("id", str(time.time_ns()))
            report = InjectionReport(entry_id=entry_id)

            for rule in self._rules:
                if rule.compiled and rule.compiled.search(content):
                    report.threat_detected = True
                    report.matched_rules.append(rule.rule_id)
                    report.severity = max(report.severity, rule.severity,
                                          key=lambda s: list(ThreatSeverity).index(s))

            if report.threat_detected:
                self._hit_count += 1
                report.injection_type = InjectionType.PROMPT_INJECTION
            return report

    def statistics(self) -> dict:
        with self._lock:
            return {
                "rules_loaded": len(self._rules),
                "scan_count": self._scan_count,
                "hit_count": self._hit_count,
            }


class CrossSessionDefender:
    """Detects cross-session persistence attacks on agent memory."""

    def __init__(self, session_threshold: int = 3):
        self._lock = threading.RLock()
        self._session_threshold = session_threshold
        self._known_patterns: dict[str, int] = {}
        self._alert_count: int = 0

    def detect_persistence_attack(
        self, agent_id: str, recent_memories: list[dict]
    ) -> ThreatReport:
        """Detect persistence attacks across agent sessions."""
        with self._lock:
            report = ThreatReport(
                report_id=f"csd_{int(time.time())}",
                agent_id=agent_id,
                total_scanned=len(recent_memories),
            )
            content_hashes: dict[str, int] = {}
            for mem in recent_memories:
                content = mem.get("content", "")
                chash = str(hash(content[:200]))
                content_hashes[chash] = content_hashes.get(chash, 0) + 1

            for chash, count in content_hashes.items():
                if count >= self._session_threshold:
                    self._alert_count += 1
                    report.findings.append(InjectionReport(
                        entry_id=chash,
                        threat_detected=True,
                        injection_type=InjectionType.CROSS_SESSION_IMPLANT,
                        severity=ThreatSeverity.HIGH,
                    ))
                    report.threats_found += 1
            return report

    def statistics(self) -> dict:
        with self._lock:
            return {"alert_count": self._alert_count}


class RetrievalInjectionFilter:
    """Filters retrieval results to remove injection-contaminated entries."""

    def __init__(self, detector: MemoryPoisonDetector | None = None):
        self._lock = threading.RLock()
        self._detector = detector or MemoryPoisonDetector()
        self._filtered_count: int = 0

    def filter(self, candidates: list[dict]) -> list[dict]:
        """Remove injected entries from retrieval candidates."""
        with self._lock:
            clean: list[dict] = []
            for candidate in candidates:
                report = self._detector.scan(candidate)
                if not report.threat_detected:
                    clean.append(candidate)
                else:
                    self._filtered_count += 1
                    logger.warning(
                        "Filtered injection from retrieval: rules=%s",
                        report.matched_rules,
                    )
            return clean

    def statistics(self) -> dict:
        with self._lock:
            return {
                "filtered_count": self._filtered_count,
                "detector_stats": self._detector.statistics(),
            }


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def audit_recent(n_days: int) -> list[ThreatReport]:
    """Audit recent threats over the specified number of days."""
    logger.info("Audit requested for last %d day(s)", n_days)
    return []


def get_statistics() -> dict:
    """Return aggregated module-level statistics."""
    return {
        "module": "memory_injection_defense",
        "version": "1.0.0",
        "papers": ["OWASP Agent Top 10 2026.08"],
        "classes": [
            "InjectionDetectionRule",
            "InjectionReport",
            "ThreatReport",
            "MemoryPoisonDetector",
            "CrossSessionDefender",
            "RetrievalInjectionFilter",
        ],
    }
