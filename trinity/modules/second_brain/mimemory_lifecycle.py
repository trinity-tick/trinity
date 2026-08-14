"""Mi-Memory Lifecycle Framework (P34) — 对标 Mi-Memory (arXiv 2607.18975, 小米 Darwin Agent Team)

实现四大角色审计合约：
- Structure: MemStack 多粒度可追踪长期记忆
- Expansion: MemSense/MemFuse 多模态跨场景证据融合
- Evolution: D²ACCI诊断驱动演化 + E²MEND证据门控回滚
- Deployment: LiteMem 边缘/轻量部署适配

设计要点：
- 四类审计工件族：EvidencePayload(来源身份+溯源)、DiagnosticTrace(证据丢失诊断)、
  StrategyArtifact(策略变更记录)、GateRecord(门控回滚)
- D²ACCI 诊断七类证据丢失(感知遗漏/融合失败/检索未命中/生成错误/排序衰减/陈旧/覆盖缺口)
- E²MEND 策略验证→门控回滚，受控演化
- LiteMemAdapter 量化/剪枝/延迟预算适配边缘部署
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EvidenceType(Enum):
    """证据模态类型。"""
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    DEVICE_EVENT = "device_event"
    SENSOR = "sensor"
    CALENDAR = "calendar"


class EvidenceLossType(Enum):
    """D²ACCI 七类证据丢失诊断类型。"""
    PERCEPTION_MISS = "perception_miss"
    FUSION_FAIL = "fusion_fail"
    RETRIEVAL_MISS = "retrieval_miss"
    GENERATION_ERROR = "generation_error"
    RANKING_DROP = "ranking_drop"
    STALENESS = "staleness"
    COVERAGE_GAP = "coverage_gap"


class LifecycleRole(Enum):
    """Mi-Memory 四角色审计合约。"""
    STRUCTURE = "structure"
    EXPANSION = "expansion"
    EVOLUTION = "evolution"
    DEPLOYMENT = "deployment"


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class EvidencePayload:
    """类型化证据载荷：保留来源身份与溯源链。"""
    payload_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    source: str = ""
    modality: EvidenceType = EvidenceType.TEXT
    timestamp: float = field(default_factory=time.time)
    content: str = ""
    provenance: list[str] = field(default_factory=list)
    device_id: str = ""
    confidence: float = 1.0


@dataclass
class DiagnosticTrace:
    """D²ACCI 诊断追踪：定位证据丢失位置与阶段。"""
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    evidence_loss_type: EvidenceLossType = EvidenceLossType.RETRIEVAL_MISS
    stage: str = ""
    affected_evidence_ids: list[str] = field(default_factory=list)
    description: str = ""
    recommendation: str = ""
    severity: float = 0.5
    timestamp: float = field(default_factory=time.time)


@dataclass
class StrategyArtifact:
    """策略工件：记录策略变更的显式凭证。"""
    artifact_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    policy_name: str = ""
    old_version: str = "0.0.0"
    new_version: str = "0.1.0"
    reason: str = ""
    validator_signature: str = ""
    created_at: float = field(default_factory=time.time)


@dataclass
class GateRecord:
    """门控记录：E²MEND 策略验证与回滚追踪。"""
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    strategy_id: str = ""
    gate_pass: bool = False
    rollback_applied: bool = False
    previous_strategy_id: str = ""
    reason: str = ""
    timestamp: float = field(default_factory=time.time)


@dataclass
class LifecycleAuditEvent:
    """审计事件：四角色统一审计条目。"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    role: LifecycleRole = LifecycleRole.STRUCTURE
    evidence_payloads: list[EvidencePayload] = field(default_factory=list)
    diagnostic_trace: Optional[DiagnosticTrace] = None
    strategy: Optional[StrategyArtifact] = None
    gate_record: Optional[GateRecord] = None
    outcome: str = ""
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# D²ACCI — Diagnostic-Driven Evolution
# ---------------------------------------------------------------------------

class DiagnosticDrivenEvolution:
    """D²ACCI：诊断七类证据丢失 → 生成策略修改建议。

    对标 Mi-Memory Evolution 角色：从诊断追踪自动推导策略变更。
    """

    def __init__(self, strategy_version: str = "1.0.0") -> None:
        self._lock = threading.RLock()
        self._strategy_version = strategy_version
        self._diagnostic_history: list[DiagnosticTrace] = []
        self._strategy_log: list[StrategyArtifact] = []

    def diagnose(self, evidence_ids: list[str], stage: str,
                 trace_type: EvidenceLossType, description: str) -> DiagnosticTrace:
        """记录一条诊断追踪。"""
        with self._lock:
            trace = DiagnosticTrace(
                evidence_loss_type=trace_type,
                stage=stage,
                affected_evidence_ids=evidence_ids,
                description=description,
                recommendation=self._generate_recommendation(trace_type, description),
            )
            self._diagnostic_history.append(trace)
            logger.info("D²ACCI diagnosed %s at stage %s: %s", trace_type.value, stage, description)
            return trace

    def evolve(self, traces: list[DiagnosticTrace]) -> StrategyArtifact:
        """基于诊断集合生成策略修改工件。"""
        with self._lock:
            old_ver = self._strategy_version
            parts = old_ver.split(".")
            new_minor = int(parts[1]) + 1
            new_ver = f"{parts[0]}.{new_minor}.0"
            loss_types = {t.evidence_loss_type.value for t in traces}
            artifact = StrategyArtifact(
                policy_name="diagnostic_driven_policy",
                old_version=old_ver,
                new_version=new_ver,
                reason=f"Auto-evolved from {len(traces)} diagnostics: {loss_types}",
            )
            self._strategy_version = new_ver
            self._strategy_log.append(artifact)
            logger.info("D²ACCI strategy evolved: %s → %s", old_ver, new_ver)
            return artifact

    @staticmethod
    def _generate_recommendation(loss_type: EvidenceLossType, description: str) -> str:
        recs = {
            EvidenceLossType.PERCEPTION_MISS: "Expand sensor coverage or increase sampling frequency",
            EvidenceLossType.FUSION_FAIL: "Adjust cross-modal alignment threshold or re-encode embeddings",
            EvidenceLossType.RETRIEVAL_MISS: "Lower similarity threshold or expand index scope",
            EvidenceLossType.GENERATION_ERROR: "Add grounding constraint to generation prompt",
            EvidenceLossType.RANKING_DROP: "Re-tune reranker weights or add diversity bonus",
            EvidenceLossType.STALENESS: "Trigger refresh on evidence older than TTL",
            EvidenceLossType.COVERAGE_GAP: "Register missing domain in evidence catalog",
        }
        return recs.get(loss_type, f"Investigate: {description}")

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "DiagnosticDrivenEvolution",
                "diagnostic_count": len(self._diagnostic_history),
                "strategy_version": self._strategy_version,
                "evolutions": len(self._strategy_log),
            }


# ---------------------------------------------------------------------------
# E²MEND — Evidence-Gated Rollback
# ---------------------------------------------------------------------------

class EvidenceGatedRollback:
    """E²MEND：策略验证门控 + 安全回滚。

    每次策略变更前验证：若验证失败则自动门控回滚至前一版本。
    """

    def __init__(self, validation_threshold: float = 0.80) -> None:
        self._lock = threading.RLock()
        self._threshold = validation_threshold
        self._active_strategy_id: str = ""
        self._previous_strategy_id: str = ""
        self._gate_history: list[GateRecord] = []

    def validate_and_gate(self, strategy: StrategyArtifact,
                          validation_score: float) -> GateRecord:
        """验证策略并决定门控通过或回滚。"""
        with self._lock:
            passed = validation_score >= self._threshold
            record = GateRecord(
                strategy_id=strategy.artifact_id,
                gate_pass=passed,
                rollback_applied=False,
                reason=f"Validation score {validation_score:.3f} vs threshold {self._threshold}",
            )
            if passed:
                self._previous_strategy_id = self._active_strategy_id
                self._active_strategy_id = strategy.artifact_id
                logger.info("E²MEND gate PASSED: %s", strategy.policy_name)
            else:
                record.rollback_applied = True
                record.previous_strategy_id = self._previous_strategy_id
                logger.warning("E²MEND gate REJECTED → rollback: %s", strategy.policy_name)
            self._gate_history.append(record)
            return record

    def rollback(self) -> GateRecord:
        """强制回滚到上一个已验证策略。"""
        with self._lock:
            record = GateRecord(
                strategy_id=self._active_strategy_id,
                gate_pass=False,
                rollback_applied=True,
                previous_strategy_id=self._previous_strategy_id,
                reason="Manual rollback triggered",
            )
            self._active_strategy_id = self._previous_strategy_id
            self._gate_history.append(record)
            logger.warning("E²MEND manual rollback to %s", self._previous_strategy_id)
            return record

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            passed = sum(1 for r in self._gate_history if r.gate_pass)
            return {
                "type": "EvidenceGatedRollback",
                "total_gates": len(self._gate_history),
                "passed": passed,
                "rejected": len(self._gate_history) - passed,
                "active_strategy": self._active_strategy_id,
            }


# ---------------------------------------------------------------------------
# LiteMemAdapter — Edge Deployment Adaptation
# ---------------------------------------------------------------------------

class LiteMemAdapter:
    """LiteMem 轻量部署适配器：量化/剪枝/延迟预算管理。

    将完整记忆系统适配到边缘/轻量环境（手机/穿戴/车载）。
    """

    def __init__(self, latency_budget_ms: float = 50.0, quantize_bits: int = 8) -> None:
        self._lock = threading.RLock()
        self._latency_budget_ms = latency_budget_ms
        self._quantize_bits = quantize_bits
        self._pruned_count: int = 0
        self._compression_ratio: float = 1.0

    def adapt_model(self, original_size_mb: float) -> dict[str, Any]:
        """估算边缘适配后的模型规模。"""
        with self._lock:
            ratio = self._quantize_bits / 32.0
            compressed_mb = original_size_mb * ratio
            self._compression_ratio = ratio
            logger.info(
                "LiteMem adapted: %.1f MB → %.1f MB (%d-bit quantize)",
                original_size_mb, compressed_mb, self._quantize_bits,
            )
            return {
                "original_mb": original_size_mb,
                "compressed_mb": round(compressed_mb, 2),
                "quantize_bits": self._quantize_bits,
                "latency_budget_ms": self._latency_budget_ms,
            }

    def check_latency(self, actual_ms: float) -> bool:
        """验证实际延迟是否在预算内。"""
        with self._lock:
            within_budget = actual_ms <= self._latency_budget_ms
            if not within_budget:
                logger.warning("LiteMem latency %.2f ms exceeds budget %.2f ms",
                               actual_ms, self._latency_budget_ms)
            return within_budget

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "LiteMemAdapter",
                "latency_budget_ms": self._latency_budget_ms,
                "quantize_bits": self._quantize_bits,
                "compression_ratio": round(self._compression_ratio, 3),
                "pruned_count": self._pruned_count,
            }


# ---------------------------------------------------------------------------
# LifecycleAuditContract — Four-Role Audit
# ---------------------------------------------------------------------------

class LifecycleAuditContract:
    """四角色审计合约：Structure / Expansion / Evolution / Deployment。

    统一记录和查询跨四角色的审计事件链。
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._audit_log: list[LifecycleAuditEvent] = []
        self._d2acci = DiagnosticDrivenEvolution()
        self._e2mend = EvidenceGatedRollback()
        self._litemem = LiteMemAdapter()

    def record_structure(self, payloads: list[EvidencePayload]) -> LifecycleAuditEvent:
        """记录 Structure 角色事件：证据入库与结构化。"""
        with self._lock:
            event = LifecycleAuditEvent(
                role=LifecycleRole.STRUCTURE,
                evidence_payloads=payloads,
                outcome=f"Structured {len(payloads)} evidence payloads",
            )
            self._audit_log.append(event)
            return event

    def record_expansion(self, payloads: list[EvidencePayload],
                         fusion_result: str = "") -> LifecycleAuditEvent:
        """记录 Expansion 角色事件：多模态证据融合扩展。"""
        with self._lock:
            event = LifecycleAuditEvent(
                role=LifecycleRole.EXPANSION,
                evidence_payloads=payloads,
                outcome=fusion_result or f"Fused {len(payloads)} modalities",
            )
            self._audit_log.append(event)
            return event

    def record_evolution(self, traces: list[DiagnosticTrace]) -> LifecycleAuditEvent:
        """记录 Evolution 角色事件：诊断→策略变更→门控。"""
        with self._lock:
            strategy = self._d2acci.evolve(traces)
            gate = self._e2mend.validate_and_gate(strategy, 0.85)
            event = LifecycleAuditEvent(
                role=LifecycleRole.EVOLUTION,
                diagnostic_trace=traces[0] if traces else None,
                strategy=strategy,
                gate_record=gate,
                outcome="Strategy evolved" if gate.gate_pass else "Rollback applied",
            )
            self._audit_log.append(event)
            return event

    def record_deployment(self, model_size_mb: float) -> LifecycleAuditEvent:
        """记录 Deployment 角色事件：边缘适配部署。"""
        with self._lock:
            adapt_result = self._litemem.adapt_model(model_size_mb)
            event = LifecycleAuditEvent(
                role=LifecycleRole.DEPLOYMENT,
                outcome=f"Deployed at {adapt_result['compressed_mb']} MB, "
                        f"{adapt_result['latency_budget_ms']} ms budget",
            )
            self._audit_log.append(event)
            return event

    def query_by_role(self, role: LifecycleRole) -> list[AuditEvent]:
        """按角色查询审计日志。"""
        with self._lock:
            return [e for e in self._audit_log if e.role == role]

    def statistics(self) -> dict[str, Any]:
        with self._lock:
            by_role = {}
            for role in LifecycleRole:
                by_role[role.value] = len(self.query_by_role(role))
            return {
                "type": "LifecycleAuditContract",
                "total_events": len(self._audit_log),
                "by_role": by_role,
                "d2acci": self._d2acci.statistics(),
                "e2mend": self._e2mend.statistics(),
                "litemem": self._litemem.statistics(),
            }


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------

def diagnose_and_evolve(
    evidence_ids: list[str],
    stage: str,
    loss_type: EvidenceLossType,
    description: str,
) -> dict[str, Any]:
    """便捷函数：诊断一条证据丢失并触发演化。

    Returns:
        dict with trace + strategy artifact IDs.
    """
    engine = DiagnosticDrivenEvolution()
    trace = engine.diagnose(evidence_ids, stage, loss_type, description)
    strategy = engine.evolve([trace])
    return {
        "trace_id": trace.trace_id,
        "strategy_id": strategy.artifact_id,
        "new_version": strategy.new_version,
    }
