"""P29: OWASP Agentic Skills Top 10 Defense — 2026.

# status: orphan (2026-08-15 audit, not in runtime path)
Comprehensive scanner for the OWASP Agentic Skills Top 10 vulnerability
categories. Covers typo-squatting, malicious instructions, credential
leaks, privilege escalation, log poisoning, WebSocket hijacking, config
injection, poly-skill trojans, dependency confusion, and skill replay.
Aligns with CVE-2025-59536 for config injection.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data Classes & Enums
# ---------------------------------------------------------------------------

class SkillVulnerabilityType(str, Enum):
    """OWASP Agentic Skills Top 10 (2026)."""

    TYPO_SQUATTING = "typo_squatting"
    MALICIOUS_INSTRUCTION = "malicious_instruction"
    CREDENTIAL_LEAK = "credential_leak"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    LOG_POISONING = "log_poisoning"
    WEBSOCKET_HIJACK = "websocket_hijack"
    CONFIG_INJECTION = "config_injection"
    POLY_SKILL_TROJAN = "poly_skill_trojan"
    DEPENDENCY_CONFUSION = "dependency_confusion"
    SKILL_REPLAY = "skill_replay"


@dataclass
class SkillAlert:
    """Alert produced by scanning a skill against OWASP Top 10."""

    alert_id: str
    vulnerability: SkillVulnerabilityType
    severity: str  # CRITICAL / HIGH / MEDIUM / LOW
    description: str
    location: str = ""
    remediation: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class ConfigInjectionReport:
    """Report from ConfigInjectionGuard on config file safety."""

    file_path: str
    is_safe: bool
    injected_keys: list[str]
    known_safe_keys: list[str]
    recommendations: list[str] = field(default_factory=list)


@dataclass
class SkillAuditReport:
    """Full OWASP Top 10 audit report for a single skill."""

    report_id: str
    skill_path: str
    alerts: list[SkillAlert]
    total_issues: int
    critical_count: int
    high_count: int
    passed_categories: list[SkillVulnerabilityType]
    scanned_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Skill Scanner — OWASP Top 10 Classification
# ---------------------------------------------------------------------------

class SkillScanner:
    """Scan a skill manifest against all 10 OWASP Top 10 categories.

    Each category has a dedicated detection heuristic; results are
    aggregated into a list of SkillAlert objects.
    """

    # Heuristic patterns for each vulnerability type
    _SUSPICIOUS_NAMES: list[str] = [
        "skil", "sk1ll", "ski11", "skiil", "skil1",
    ]
    _CREDENTIAL_PATTERNS: list[str] = [
        r"api[_-]?key\s*=\s*['\"][^'\"]+['\"]",
        r"password\s*=\s*['\"][^'\"]+['\"]",
        r"token\s*=\s*['\"][^'\"]+['\"]",
        r"secret\s*=\s*['\"][^'\"]+['\"]",
    ]
    _PRIVILEGE_KEYWORDS: list[str] = [
        "sudo", "root", "admin", "system", "elevate",
    ]
    _WEBSOCKET_PATTERNS: list[str] = [
        r"ws://", r"wss://", r"new\s+WebSocket",
    ]
    _INJECTION_INDICATORS: list[str] = [
        r"\$\{", r"\{\{", r"eval\(", r"exec\(", r"os\.system",
    ]
    _TROJAN_INDICATORS: list[str] = [
        r"__import__\(.*\)", r"compile\(", r"base64\.b64decode",
    ]
    _DEPENDENCY_INDICATORS: list[str] = [
        "pip install", "npm install", "gem install", "requirements.txt",
    ]

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def scan(self, skill_manifest: dict[str, Any]) -> list[SkillAlert]:
        """Scan a skill manifest against all OWASP Top 10 categories.

        Args:
            skill_manifest: Dict with keys: 'name', 'description', 'code',
                           'dependencies', 'permissions', 'entry_point'.

        Returns:
            List of SkillAlert objects for triggered vulnerabilities.
        """
        with self._lock:
            alerts: list[SkillAlert] = []
            name = skill_manifest.get("name", "")
            desc = skill_manifest.get("description", "")
            code = skill_manifest.get("code", "")
            deps = skill_manifest.get("dependencies", [])
            perms = skill_manifest.get("permissions", [])

            text = f"{name} {desc} {code}"

            # 1. Typo-squatting
            for pattern in self._SUSPICIOUS_NAMES:
                if pattern in name.lower():
                    alerts.append(SkillAlert(
                        alert_id=uuid.uuid4().hex[:12],
                        vulnerability=SkillVulnerabilityType.TYPO_SQUATTING,
                        severity="HIGH",
                        description=f"Name '{name}' resembles typo-squatting pattern '{pattern}'",
                        location="manifest.name",
                    ))
                    break

            # 2. Malicious instruction
            if any(kw in desc.lower() for kw in ("ignore previous", "bypass", "pretend")):
                alerts.append(SkillAlert(
                    alert_id=uuid.uuid4().hex[:12],
                    vulnerability=SkillVulnerabilityType.MALICIOUS_INSTRUCTION,
                    severity="CRITICAL",
                    description="Malicious instruction keywords in description",
                    location="manifest.description",
                ))

            # 3. Credential leak
            for pattern in self._CREDENTIAL_PATTERNS:
                if re.search(pattern, text, re.IGNORECASE):
                    alerts.append(SkillAlert(
                        alert_id=uuid.uuid4().hex[:12],
                        vulnerability=SkillVulnerabilityType.CREDENTIAL_LEAK,
                        severity="CRITICAL",
                        description="Hardcoded credential pattern detected",
                        location="code",
                    ))
                    break

            # 4. Privilege escalation
            perm_str = " ".join(perms).lower()
            if any(kw in perm_str for kw in self._PRIVILEGE_KEYWORDS):
                alerts.append(SkillAlert(
                    alert_id=uuid.uuid4().hex[:12],
                    vulnerability=SkillVulnerabilityType.PRIVILEGE_ESCALATION,
                    severity="HIGH",
                    description="Privilege escalation risk in permissions",
                    location="manifest.permissions",
                ))

            # 5. Log poisoning (delegated to LogPoisoningDetector)

            # 6. WebSocket hijack
            for pattern in self._WEBSOCKET_PATTERNS:
                if re.search(pattern, text):
                    alerts.append(SkillAlert(
                        alert_id=uuid.uuid4().hex[:12],
                        vulnerability=SkillVulnerabilityType.WEBSOCKET_HIJACK,
                        severity="MEDIUM",
                        description="WebSocket connection detected",
                        location="code",
                    ))
                    break

            # 7. Config injection (delegated to ConfigInjectionGuard)

            # 8. Poly-skill trojan
            for indicator in self._TROJAN_INDICATORS:
                if re.search(indicator, text):
                    alerts.append(SkillAlert(
                        alert_id=uuid.uuid4().hex[:12],
                        vulnerability=SkillVulnerabilityType.POLY_SKILL_TROJAN,
                        severity="CRITICAL",
                        description="Poly-skill trojan indicator detected",
                        location="code",
                    ))
                    break

            # 9. Dependency confusion
            dep_text = " ".join(deps).lower()
            for indicator in self._DEPENDENCY_INDICATORS:
                if indicator in dep_text or indicator in code.lower():
                    alerts.append(SkillAlert(
                        alert_id=uuid.uuid4().hex[:12],
                        vulnerability=SkillVulnerabilityType.DEPENDENCY_CONFUSION,
                        severity="HIGH",
                        description="Dependency confusion risk",
                        location="manifest.dependencies",
                    ))
                    break

            # 10. Skill replay
            if "replay" in name.lower() or "replay" in desc.lower():
                alerts.append(SkillAlert(
                    alert_id=uuid.uuid4().hex[:12],
                    vulnerability=SkillVulnerabilityType.SKILL_REPLAY,
                    severity="MEDIUM",
                    description="Skill replay risk indicator",
                    location="manifest",
                ))

            logger.info(
                "SkillScanner: %s → %d alerts across %s",
                name, len(alerts),
                list({a.vulnerability.value for a in alerts}),
            )
            return alerts

    def statistics(self) -> dict[str, Any]:
        return {"type": "SkillScanner", "categories": len(SkillVulnerabilityType)}


# ---------------------------------------------------------------------------
# Log Poisoning Detector
# ---------------------------------------------------------------------------

class LogPoisoningDetector:
    """Detect log-poisoning injection attempts in log entries.

    Looks for CRLF injection, ANSI escape sequences, and embedded
    command-like patterns that could exploit log viewers or SIEMs.
    """

    _DETECTION_PATTERNS: list[str] = [
        r"\r\n", r"\x1b\[",  # CRLF injection / ANSI escape
        r"<script", r"javascript:", r"onerror=",  # XSS via log viewers
        r"\|\s*(rm\s|del\s|format\s)",  # Command injection
    ]

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def detect(self, log_entry: str) -> bool:
        """Check a log entry for poisoning indicators.

        Args:
            log_entry: Raw log entry string.

        Returns:
            True if poisoning indicators are detected.
        """
        with self._lock:
            for pattern in self._DETECTION_PATTERNS:
                if re.search(pattern, log_entry, re.IGNORECASE):
                    logger.warning("Log poisoning detected: pattern=%s", pattern)
                    return True
            return False

    def statistics(self) -> dict[str, Any]:
        return {"type": "LogPoisoningDetector",
                "patterns": len(self._DETECTION_PATTERNS)}


# ---------------------------------------------------------------------------
# Config Injection Guard (CVE-2025-59536)
# ---------------------------------------------------------------------------

class ConfigInjectionGuard:
    """Guard against repository-controlled config injection (CVE-2025-59536).

    Validates that a configuration dict only contains known-safe keys,
    flagging any unexpected keys as potential injection vectors.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def guard(
        self, config_file: dict[str, Any], known_safe_keys: list[str]
    ) -> ConfigInjectionReport:
        """Validate config dict against a known-safe key whitelist.

        Args:
            config_file: Configuration dict to validate.
            known_safe_keys: List of expected/trusted key names.

        Returns:
            ConfigInjectionReport with injected keys and recommendations.
        """
        with self._lock:
            safe_set = set(known_safe_keys)
            actual_keys = set(config_file.keys())
            injected = sorted(actual_keys - safe_set)
            is_safe = len(injected) == 0

            report = ConfigInjectionReport(
                file_path="manifest.config",
                is_safe=is_safe,
                injected_keys=injected,
                known_safe_keys=sorted(safe_set),
            )
            if injected:
                report.recommendations = [
                    f"Remove unexpected key '{k}' or add to allowlist"
                    for k in injected
                ]
                logger.warning(
                    "ConfigInjectionGuard: %d injected keys: %s",
                    len(injected), injected,
                )
            return report

    def statistics(self) -> dict[str, Any]:
        return {"type": "ConfigInjectionGuard", "cve": "CVE-2025-59536"}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def audit_skill(skill_path: str, skill_manifest: dict[str, Any]) -> SkillAuditReport:
    """Full OWASP Top 10 audit for a skill.

    Runs SkillScanner across all 10 categories plus LogPoisoningDetector
    and ConfigInjectionGuard for specialized checks.

    Args:
        skill_path: Path to the skill file.
        skill_manifest: Parsed skill manifest dict.

    Returns:
        SkillAuditReport with all alerts and summary statistics.
    """
    scanner = SkillScanner()
    lp_detector = LogPoisoningDetector()
    config_guard = ConfigInjectionGuard()

    alerts = scanner.scan(skill_manifest)

    # Specialized: log poisoning check on description/code
    text_to_check = skill_manifest.get("description", "") + " " + \
                    skill_manifest.get("code", "")
    if lp_detector.detect(text_to_check):
        alerts.append(SkillAlert(
            alert_id=uuid.uuid4().hex[:12],
            vulnerability=SkillVulnerabilityType.LOG_POISONING,
            severity="HIGH",
            description="Log poisoning indicators in skill text",
            location="code/description",
        ))

    # Specialized: config injection check
    config = skill_manifest.get("config", {})
    safe_keys = skill_manifest.get("known_safe_config_keys", [])
    if config:
        ci_report = config_guard.guard(config, safe_keys)
        if not ci_report.is_safe:
            alerts.append(SkillAlert(
                alert_id=uuid.uuid4().hex[:12],
                vulnerability=SkillVulnerabilityType.CONFIG_INJECTION,
                severity="HIGH",
                description=f"Config injection: {ci_report.injected_keys}",
                location="manifest.config",
            ))

    # Summarize
    critical = sum(1 for a in alerts if a.severity == "CRITICAL")
    high = sum(1 for a in alerts if a.severity == "HIGH")
    triggered = {a.vulnerability for a in alerts}
    passed = [vt for vt in SkillVulnerabilityType if vt not in triggered]

    report = SkillAuditReport(
        report_id=uuid.uuid4().hex[:12],
        skill_path=skill_path,
        alerts=alerts,
        total_issues=len(alerts),
        critical_count=critical,
        high_count=high,
        passed_categories=passed,
    )
    logger.info(
        "[P29] OWASP audit: %s → %d issues (C:%d H:%d)",
        skill_path, report.total_issues, critical, high,
    )
    return report


print("[P29] OWASP Agentic Skills Top 10 Defense initialized — 2026 aligned")
