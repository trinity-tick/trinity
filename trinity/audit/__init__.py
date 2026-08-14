"""
Trinity DCSA-EJP Audit Package
===============================
Dual-Loop Constitutional Self-Auditing with Executor-Judge Paralysis (Curve Labs, 2026).

Exports:
  Auditor              — Independent audit loop engine
  ConstitutionalEngine — Invariant-based policy checker
  JustificationPacket  — Standardized 6-field rationale structure
  AuditResult          — Audit action result dataclass
  Invariant            — Constitutional invariant rule definition
  UncertaintyLevel     — LOW / MEDIUM / HIGH / CRITICAL
  Severity             — Violation severity (CRITICAL / HIGH / MEDIUM / LOW)
  ViolationResult      — Per-invariant check result (PASS / FAIL / FLAG / SKIP)
"""

from trinity.audit.auditor import Auditor, AuditResult
from trinity.audit.constitution import (
    ConstitutionalEngine,
    Invariant,
    Severity,
    ViolationResult,
)
from trinity.audit.justification_packet import JustificationPacket, UncertaintyLevel

__all__ = [
    "Auditor",
    "AuditResult",
    "ConstitutionalEngine",
    "Invariant",
    "JustificationPacket",
    "Severity",
    "UncertaintyLevel",
    "ViolationResult",
]

__version__ = "8.0.0"
