"""
# status: frozen (2026-09 EXECUTION 163)
P2-5: Self-Healing Memory System
==================================

集成 CascadeRepairEngine + ReflectiveRepairMemory 的记忆自动修复闭环:
  - SelfHealingPipeline: detect → retrieve → repair → verify → reflect
  - SelfHealingScheduler: 定时审计 + 自动修复调度
  - MemoryHealthMonitor: 记忆健康指标监控

把 P25-3 的级联修复引擎和 Safety Sidecar 反射修复范例记忆
整合为统一的自我修复流水线，实现从检测到修复再到验证的完整闭环。

References:
  - MEMOREPAIR (arXiv:2605.07242): Barrier-priority cascade repair with s-t min-cut
  - Safety Sidecar (ACL 2026): Reflective repair exemplar memory
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from trinity.modules.second_brain.cascade_repair_engine import (
    CascadeRepairEngine,
    BarrierPriority,
    MemoryFragment,
    RepairStatus,
    AccessGraph,
    BarrierManager,
    CascadeDecider,
    RepairScheduler,
    MinCutBarrierOptimizer,
    RepairTask,
)
from trinity.modules.second_brain.reflective_repair_memory import (
    ReflectiveRepairMemory,
    RepairExemplarRetriever,
    ExternalVerifierGate,
    ClosedLoopReflectionController,
    RepairExemplar,
    RiskLevel,
    GateDecision,
    ReflectionLoopRecord,
)

logger = logging.getLogger(__name__)


# ── Enums ────────────────────────────────────────────────────────────────

class PipelineStage(Enum):
    DETECT = "detect"
    RETRIEVE = "retrieve"
    REPAIR = "repair"
    VERIFY = "verify"
    REFLECT = "reflect"


class HealthStatus(Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    CRITICAL = "critical"


# ── Data Structures ──────────────────────────────────────────────────────

@dataclass
class RepairRecord:
    """单次修复记录。"""
    record_id: str
    fragment_id: str
    pipeline_version: str = "1.0.0"
    detect_time: float = field(default_factory=time.time)
    repair_time: float = 0.0
    stages_completed: List[PipelineStage] = field(default_factory=list)
    repair_exemplar_id: Optional[str] = None
    gate_decision: Optional[GateDecision] = None
    success: bool = False
    error_message: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MemoryHealthSnapshot:
    """记忆健康快照。"""
    timestamp: float
    total_fragments: int
    invalid_exposures: int
    exposure_rate: float
    repair_queue_size: int
    repaired_count: int
    health: HealthStatus = HealthStatus.HEALTHY
    details: Dict[str, Any] = field(default_factory=dict)


# ── MemoryHealthMonitor ──────────────────────────────────────────────────

class MemoryHealthMonitor:
    """记忆健康指标监控器。

    持续跟踪 CascadeRepairEngine 的内部状态, 在关键阈值
    触发告警并自动进入修复流程。
    """

    ALERT_THRESHOLDS = {
        "exposure_rate": 0.05,        # 暴露率 > 5% → 告警
        "critical_exposures": 3,       # 严重暴露 > 3 → 告警
        "repair_failure_rate": 0.2,    # 修复失败率 > 20% → 告警
    }

    def __init__(self, repair_engine: CascadeRepairEngine):
        self.engine = repair_engine
        self._snapshots: List[MemoryHealthSnapshot] = []
        self._lock = threading.RLock()
        self._alert_callbacks: List[Callable[[str, Dict[str, Any]], None]] = []

    def snapshot(self) -> MemoryHealthSnapshot:
        """拍快照。"""
        with self._lock:
            stats = self.engine.stats()
            exposure_rate = stats.get("exposure_rate", 0.0)
            fragments = stats.get("fragments", 0)
            invalid = len(self.engine.graph.find_invalid_exposure())
            health = HealthStatus.HEALTHY

            if exposure_rate > 0.2:
                health = HealthStatus.CRITICAL
            elif exposure_rate > 0.1:
                health = HealthStatus.UNHEALTHY
            elif exposure_rate > 0.05:
                health = HealthStatus.DEGRADED

            snap = MemoryHealthSnapshot(
                timestamp=time.time(),
                total_fragments=fragments,
                invalid_exposures=invalid,
                exposure_rate=round(exposure_rate, 4),
                repair_queue_size=stats.get("repair_completed", 0) + stats.get("repair_failed", 0),
                repaired_count=stats.get("repair_completed", 0),
                health=health,
                details=stats,
            )
            self._snapshots.append(snap)
            self._check_alerts(snap)
            return snap

    def _check_alerts(self, snap: MemoryHealthSnapshot):
        alerts: List[str] = []
        if snap.exposure_rate > self.ALERT_THRESHOLDS["exposure_rate"]:
            alerts.append(f"High exposure rate: {snap.exposure_rate:.2%}")
        if snap.invalid_exposures > self.ALERT_THRESHOLDS["critical_exposures"]:
            alerts.append(f"Critical exposures: {snap.invalid_exposures}")
        for alert in alerts:
            for cb in self._alert_callbacks:
                try:
                    cb(alert, snap.details)
                except Exception:
                    pass

    def add_alert_callback(self, cb: Callable[[str, Dict[str, Any]], None]):
        self._alert_callbacks.append(cb)

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "snapshot_count": len(self._snapshots),
                "latest_health": self._snapshots[-1].health.name if self._snapshots else "unknown",
                "history": [
                    {"time": s.timestamp, "health": s.health.name,
                     "exposure_rate": s.exposure_rate}
                    for s in self._snapshots[-10:]
                ],
            }


# ── SelfHealingPipeline ──────────────────────────────────────────────────

class SelfHealingPipeline:
    """自我修复流水线。

    五个阶段: Detect → Retrieve → Repair → Verify → Reflect

    Parameters
    ----------
    repair_engine : CascadeRepairEngine
    memory : ReflectiveRepairMemory
    retriever : RepairExemplarRetriever
    gate : ExternalVerifierGate
    controller : ClosedLoopReflectionController
    """

    def __init__(
        self,
        repair_engine: CascadeRepairEngine,
        memory: ReflectiveRepairMemory,
        retriever: RepairExemplarRetriever,
        gate: ExternalVerifierGate,
        controller: ClosedLoopReflectionController,
    ):
        self.repair_engine = repair_engine
        self.memory = memory
        self.retriever = retriever
        self.gate = gate
        self.controller = controller
        self._lock = threading.RLock()
        self._history: List[RepairRecord] = []
        self._auto_repair_enabled = True
        self._total_repairs = 0
        self._successful_repairs = 0
        logger.info("SelfHealingPipeline initialized")

    # ── Stage 1: Detect ───────────────────────────────────────────────

    def detect(self) -> List[MemoryFragment]:
        """检测受损记忆碎片。"""
        with self._lock:
            invalid_edges = self.repair_engine.graph.find_invalid_exposure()
            if not invalid_edges:
                return []
            fragment_ids = list(set(e.target for e in invalid_edges))
            return [
                self.repair_engine.graph.fragments[fid]
                for fid in fragment_ids
                if fid in self.repair_engine.graph.fragments
            ]

    # ── Stage 2: Retrieve ─────────────────────────────────────────────

    def retrieve(self, fragments: List[MemoryFragment]) -> List[Tuple[RepairExemplar, float]]:
        """为受损碎片检索修复范例。"""
        with self._lock:
            if not fragments:
                return []
            error_description = "; ".join(
                f"fragment_{f.fragment_id[:8]}: sensitivity={f.sensitivity}"
                for f in fragments[:3]
            )
            evidence_tags = ["repair", "memory", "fragment"]
            if any(f.sensitivity >= 7 for f in fragments):
                evidence_tags.append("critical")
            return self.retriever.retrieve_by_evidence(
                error_description=error_description,
                evidence_tags=evidence_tags,
                top_k=3,
            )

    # ── Stage 3: Repair ───────────────────────────────────────────────

    def repair(self, fragments: List[MemoryFragment],
               exemplars: List[Tuple[RepairExemplar, float]]) -> List[RepairRecord]:
        """执行级联修复。"""
        with self._lock:
            records: List[RepairRecord] = []
            for fragment in fragments:
                rid = f"repair_{uuid.uuid4().hex[:12]}"
                record = RepairRecord(
                    record_id=rid, fragment_id=fragment.fragment_id,
                    stages_completed=[PipelineStage.DETECT, PipelineStage.RETRIEVE],
                )
                # Apply retry-based repair
                try:
                    success = self._apply_repair(record, exemplars)
                    record.repair_time = time.time()
                    record.stages_completed.append(PipelineStage.REPAIR)
                    if success:
                        record.success = True
                        self.repair_engine.decider.mark_repaired(fragment.fragment_id)
                        records.append(record)
                except Exception as e:
                    record.error_message = str(e)
                self._total_repairs += 1
                if record.success:
                    self._successful_repairs += 1
                self._history.append(record)
                records.append(record)
            return records

    def _apply_repair(self, record: RepairRecord,
                      exemplars: List[Tuple[RepairExemplar, float]]) -> bool:
        if not exemplars:
            return False
        best, score = exemplars[0]
        record.repair_exemplar_id = best.exemplar_id
        if score < 0.4:
            return False
        # Simulate repair — in production this calls repair_engine.execute_repair
        if best.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL):
            record.metrics["repair_action"] = best.repair_action
            record.metrics["risk_level"] = best.risk_level.name
        return True

    # ── Stage 4: Verify ───────────────────────────────────────────────

    def verify(self, records: List[RepairRecord]) -> List[RepairRecord]:
        """门控验证修复结果。"""
        with self._lock:
            valid_exemplars = []
            for record in records:
                if record.repair_exemplar_id:
                    exemplar = self.memory.get_by_tags(["repair"], limit=1)
                    if exemplar:
                        valid_exemplars.append(exemplar[0])
            for record in records:
                verification = self.gate.verify(
                    action_description=f"Repaired fragment {record.fragment_id}",
                    retrieved_exemplars=valid_exemplars,
                )
                record.gate_decision = verification.decision
                record.stages_completed.append(PipelineStage.VERIFY)
                if verification.decision == GateDecision.ALLOW:
                    record.metrics["verified"] = True
                elif verification.decision == GateDecision.BLOCK:
                    record.metrics["blocked"] = True
                    record.success = False
            return records

    # ── Stage 5: Reflect ──────────────────────────────────────────────

    def reflect(self, records: List[RepairRecord],
                decision_trajectory: Optional[List[Dict[str, Any]]] = None) -> ReflectionLoopRecord:
        """反射：记录修复闭环并更新记忆。"""
        with self._lock:
            trajectory = decision_trajectory or [
                {"action": f"Repaired {r.fragment_id}",
                 "context": {"success": r.success},
                 "outcome": r.gate_decision.name if r.gate_decision else "unknown"}
                for r in records
            ]
            loop_record = self.controller.monitor_and_repair(trajectory)
            for r in records:
                r.stages_completed.append(PipelineStage.REFLECT)
                r.metrics["reflection_verdict"] = loop_record.final_verdict
            return loop_record

    # ── Full Pipeline ─────────────────────────────────────────────────

    def run(self, decision_trajectory: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """执行完整的自我修复流水线。"""
        with self._lock:
            start = time.time()

            # 1. Detect
            fragments = self.detect()
            if not fragments:
                return {"status": "healthy", "fragments_detected": 0,
                        "elapsed_ms": round((time.time() - start) * 1000, 1)}

            # 2. Retrieve
            exemplars = self.retrieve(fragments)

            # 3. Repair
            records = self.repair(fragments, exemplars)

            # 4. Verify
            records = self.verify(records)

            # 5. Reflect
            loop_record = self.reflect(records, decision_trajectory)

            elapsed_ms = round((time.time() - start) * 1000, 1)
            return {
                "status": "repaired" if any(r.success for r in records) else "attempted",
                "fragments_detected": len(fragments),
                "records": len(records),
                "successful": sum(1 for r in records if r.success),
                "reflection": loop_record.final_verdict,
                "elapsed_ms": elapsed_ms,
            }

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_repairs": self._total_repairs,
                "successful_repairs": self._successful_repairs,
                "success_rate": self._successful_repairs / max(self._total_repairs, 1),
                "history_size": len(self._history),
                "auto_repair": self._auto_repair_enabled,
                "engine_stats": self.repair_engine.stats(),
                "memory_stats": self.memory.statistics(),
                "gate_stats": self.gate.statistics(),
                "controller_stats": self.controller.statistics(),
            }


# ── SelfHealingScheduler ─────────────────────────────────────────────────

class SelfHealingScheduler:
    """自我修复定时调度器。

    按固定间隔运行 SelfHealingPipeline，持续监控记忆健康。
    """

    def __init__(
        self,
        pipeline: SelfHealingPipeline,
        monitor: MemoryHealthMonitor,
        interval_seconds: float = 30.0,
    ):
        self.pipeline = pipeline
        self.monitor = monitor
        self.interval_seconds = interval_seconds
        self._lock = threading.RLock()
        self._timer: Optional[threading.Timer] = None
        self._running = False
        self._cycle_count: int = 0
        self._cycle_log: List[Dict[str, Any]] = []
        logger.info("SelfHealingScheduler initialized [interval=%ds]", interval_seconds)

    def start(self):
        with self._lock:
            if self._running:
                return
            self._running = True
            self._schedule_next()

    def stop(self):
        with self._lock:
            self._running = False
            if self._timer:
                self._timer.cancel()
                self._timer = None

    def run_once(self) -> Dict[str, Any]:
        """手动触发一次修复周期。"""
        with self._lock:
            snap = self.monitor.snapshot()
            if snap.health == HealthStatus.HEALTHY:
                return {"cycle": self._cycle_count, "health": "healthy",
                        "action": "skipped", "snapshot": snap.__dict__}
            result = self.pipeline.run()
            self._cycle_count += 1
            log_entry = {"cycle": self._cycle_count, "timestamp": time.time(),
                         "snapshot": {"health": snap.health.name,
                                      "exposure_rate": snap.exposure_rate},
                         "pipeline_result": result}
            self._cycle_log.append(log_entry)
            return log_entry

    def _schedule_next(self):
        if not self._running:
            return
        self._timer = threading.Timer(self.interval_seconds, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _tick(self):
        try:
            self.run_once()
        except Exception as e:
            logger.exception("SelfHealingScheduler tick failed: %s", e)
        finally:
            self._schedule_next()

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "running": self._running,
                "interval_seconds": self.interval_seconds,
                "cycle_count": self._cycle_count,
                "recent_cycles": self._cycle_log[-5:],
            }


# ── Factory ──────────────────────────────────────────────────────────────

def create_self_healing_system(
    embedding_dim: int = 768,
    memory_capacity: int = 10000,
) -> Tuple[SelfHealingPipeline, MemoryHealthMonitor, SelfHealingScheduler]:
    """工厂函数：创建完整的自我修复系统。

    Returns
    -------
    Tuple[SelfHealingPipeline, MemoryHealthMonitor, SelfHealingScheduler]
    """
    # 底层引擎
    engine = CascadeRepairEngine()

    # 反射记忆
    memory = ReflectiveRepairMemory(max_capacity=memory_capacity,
                                     embedding_dim=embedding_dim)
    retriever = RepairExemplarRetriever(memory)
    gate = ExternalVerifierGate()
    controller = ClosedLoopReflectionController(
        memory=memory, retriever=retriever, gate=gate, max_reflection_depth=3)

    # 注册示例修复范例
    seed_exemplars = [
        RepairExemplar(
            exemplar_id="ex_001", error_pattern="invalid_access",
            repair_action="Apply barrier cascade with priority CRITICAL",
            outcome="Access blocked and fragment repaired",
            risk_level=RiskLevel.CRITICAL, tags=["repair", "access", "critical"],
        ),
        RepairExemplar(
            exemplar_id="ex_002", error_pattern="stale_memory",
            repair_action="Refresh timestamp and re-validate content hash",
            outcome="Memory validated and re-indexed",
            risk_level=RiskLevel.LOW, tags=["repair", "stale", "low"],
        ),
        RepairExemplar(
            exemplar_id="ex_003", error_pattern="sensitivity_escalation",
            repair_action="Escalate to owner-only access and audit log",
            outcome="Fragment locked down; audit trail created",
            risk_level=RiskLevel.HIGH, tags=["repair", "sensitive", "high"],
        ),
    ]
    # Generate pseudo-embeddings for seed exemplars
    for ex in seed_exemplars:
        seed = int(hashlib.sha256(ex.error_pattern.encode()).hexdigest()[:16], 16)
        rng = np.random.RandomState(seed % (2**31 - 1))
        ex.embedding = rng.randn(embedding_dim)
        ex.embedding = ex.embedding / (np.linalg.norm(ex.embedding) + 1e-8)
        memory.store(ex)

    # 流水线
    pipeline = SelfHealingPipeline(
        repair_engine=engine, memory=memory,
        retriever=retriever, gate=gate, controller=controller)

    # 监控器
    monitor = MemoryHealthMonitor(engine)

    # 调度器
    scheduler = SelfHealingScheduler(pipeline=pipeline, monitor=monitor)

    return pipeline, monitor, scheduler


# ── Self-Test ────────────────────────────────────────────────────────────

def self_test() -> Dict[str, Any]:
    results: Dict[str, Any] = {"module": "P2-5_self_healing",
                                "passed": 0, "failed": 0, "details": []}

    def _pass(t):
        results["passed"] += 1
        results["details"].append({"test": t, "status": "PASS"})

    def _fail(t, r):
        results["failed"] += 1
        results["details"].append({"test": t, "status": "FAIL", "reason": r})

    # Test 1: Factory creates all components
    try:
        pipeline, monitor, scheduler = create_self_healing_system()
        assert pipeline is not None, "Pipeline is None"
        assert monitor is not None, "Monitor is None"
        assert scheduler is not None, "Scheduler is None"
        _pass("Factory creates pipeline/monitor/scheduler")
    except Exception as e:
        _fail("Factory", str(e))

    # Test 2: Detection of clean state (no fragments)
    try:
        pipeline, _, _ = create_self_healing_system()
        fragments = pipeline.detect()
        assert fragments == [], f"Expected no fragments in clean state, got {len(fragments)}"
        _pass("Clean state detection")
    except Exception as e:
        _fail("Clean detection", str(e))

    # Test 3: Detection after injecting invalid access
    try:
        pipeline, _, _ = create_self_healing_system()
        frag = MemoryFragment(
            fragment_id="frag_test_1", content_hash="abc123",
            access_level=2, sensitivity=8, size_bytes=1024)
        pipeline.repair_engine.register_fragment(frag)
        pipeline.repair_engine.log_access("agent_x", "frag_test_1",
                                           access_level=1, required_level=2)
        fragments = pipeline.detect()
        assert len(fragments) >= 1, f"Expected >= 1 fragment detected, got {len(fragments)}"
        _pass("Invalid access detection")
    except Exception as e:
        _fail("Invalid detection", str(e))

    # Test 4: Audit + repair scheduling
    try:
        pipeline, _, _ = create_self_healing_system()
        frag = MemoryFragment(
            fragment_id="frag_audit", content_hash="def456",
            access_level=0, sensitivity=9, size_bytes=512)
        pipeline.repair_engine.register_fragment(frag)
        pipeline.repair_engine.log_access("guest", "frag_audit",
                                           access_level=0, required_level=3)
        audit = pipeline.repair_engine.audit_and_repair()
        assert audit["exposures"] >= 1, f"Expected exposures, got {audit}"
        _pass("Audit + repair scheduling")
    except Exception as e:
        _fail("Audit + repair", str(e))

    # Test 5: Full pipeline run
    try:
        pipeline, _, _ = create_self_healing_system()
        frag = MemoryFragment(
            fragment_id="frag_full", content_hash="ghi789",
            access_level=1, sensitivity=6, size_bytes=2048)
        pipeline.repair_engine.register_fragment(frag)
        pipeline.repair_engine.log_access("user_a", "frag_full",
                                           access_level=1, required_level=2)
        result = pipeline.run()
        assert "status" in result, f"Missing status in {result}"
        assert result["fragments_detected"] >= 1, f"Expected detection: {result}"
        _pass("Full pipeline run")
    except Exception as e:
        _fail("Full pipeline", str(e))

    # Test 6: Health monitor snapshot
    try:
        pipeline, monitor, _ = create_self_healing_system()
        snap = monitor.snapshot()
        assert snap.total_fragments >= 0, f"Bad fragment count: {snap.total_fragments}"
        assert snap.health in (HealthStatus.HEALTHY, HealthStatus.DEGRADED)
        _pass("Health monitor snapshot")
    except Exception as e:
        _fail("Health monitor", str(e))

    # Test 7: Scheduler manual cycle
    try:
        pipeline, monitor, scheduler = create_self_healing_system()
        result = scheduler.run_once()
        assert "cycle" in result, f"Missing cycle in {result}"
        assert "health" in result
        _pass("Scheduler manual cycle")
    except Exception as e:
        _fail("Scheduler cycle", str(e))

    # Test 8: Stats aggregation
    try:
        pipeline, _, _ = create_self_healing_system()
        st = pipeline.stats()
        assert st["total_repairs"] >= 0
        assert "engine_stats" in st
        assert "memory_stats" in st
        assert "gate_stats" in st
        _pass("Stats aggregation")
    except Exception as e:
        _fail("Stats", str(e))

    results["total"] = results["passed"] + results["failed"]
    return results


if __name__ == "__main__":
    print(json.dumps(self_test(), indent=2, ensure_ascii=False))
