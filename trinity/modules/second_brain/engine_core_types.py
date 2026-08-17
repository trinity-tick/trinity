# engine_core_types — Enums and data classes extracted from engine_core.py
# Auto-generated during engine_core.py split refactoring

from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime
import time, random, math
import numpy as np

SEP = "=" * 80; SUB = "-" * 60; VERSION = "v6.50"

# ============ 枚举 ============
class ContextAction(Enum): FOLD="fold"; MASK="mask"; PRUNE="prune"; RETAIN="retain"
class ExecutionGear(Enum): G_OBS="G_obs"; G_SUG="G_sug"; G_PLAN="G_plan"; G_EXEC="G_exec"; G_INT="G_int"
class GovernanceState(Enum): STABLE="Stable"; META="Meta"; ASSISTED="Assisted"; REGULATED="Regulated"
class CertificateStatus(Enum): VALID="valid"; EXPIRED="expired"; REVOKED="revoked"; PENDING="pending"
class MemoryErrorType(Enum):
    STATE_TRACKING="state_tracking_error"
    TEMPORAL_CONFUSION="temporal_confusion"
    ENTITY_CONFUSION="entity_confusion"
    NONE="none"
class CacheWriteDecision(Enum): WRITE="write"; SKIP="skip"; EVICT="evict"
class ConsolidationPhase(Enum): IDLE="idle"; TRIGGERED="triggered"; COMMITTING="committing"; VERIFIED="verified"

# ============ 数据类 ============

@dataclass
class ContextObject:
    obj_id: str; obj_type: str; payload: Any; round_idx: int
    created_at: float; dependencies: set = field(default_factory=set)
    reference_count: int = 1; is_recoverable: bool = True
    last_action: Optional[ContextAction] = None

@dataclass
class ContextCommit:
    commit_id: str; actions: list; stats: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

@dataclass
class MemoryHead:
    head_id: int; content: str = ""; last_updated: float = 0.0
    update_count: int = 0; locked: bool = False

@dataclass
class ProvenanceRecord:
    record_id: str; source: str; timestamp: float; integrity_hash: str
    parent_record: Optional[str] = None; context_snapshot: Optional[str] = None

@dataclass
class ContinuityState:
    state_vector: list[float]; timestamp: float
    expected_range: tuple; drift_detected: bool = False

@dataclass
class SafetyAlarm:
    alarm_id: str; severity: str; source: str; message: str
    timestamp: float; risk_score: float; blocked: bool = False

@dataclass
class ExactKVEntry:
    key: str; value: Any; residual_norm: float
    timestamp: float; access_count: int = 0; pinned: bool = False

@dataclass
class ConsolidationRecord:
    record_id: str; identity_hash: str; confidence: float
    supporting_events: list[str]; provenance: str
    timestamp: float; phase: ConsolidationPhase = ConsolidationPhase.IDLE

@dataclass
class ValueCategoryMapping:
    step_index: int; value_category: str
    baseline_vector: list[float]; conditioned_vector: list[float]
    divergence_js: float = 0.0

print(f"[Second Brain {VERSION}] Core imports & data classes ready")


# ============ M1-M39: 继承自 v6.1 (占位模块) ============
# 这些模块在 v6.1 中已实现，此处为版本连续性保留引用


# ============ M40: MultiHeadRecurrentMemory ============
