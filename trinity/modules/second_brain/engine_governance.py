# engine_governance — P21-P25 Governance & Multi-Head Memory Tier 1
# Auto-generated during engine_core.py split refactoring
# status: frozen (2026-09 EXECUTION 163)

from __future__ import annotations
import os, sys, time, math, random, uuid, json, hashlib, statistics, itertools, re
import numpy as np
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from collections import defaultdict, OrderedDict, deque
from datetime import datetime

SEP = "=" * 80; SUB = "-" * 60; VERSION = "v6.50"

from .engine_core_types import (
    ContextAction, MemoryHead, ContinuityState, SafetyAlarm,
    ExactKVEntry, ConsolidationRecord, ConsolidationPhase,
    ValueCategoryMapping, CacheWriteDecision,
)

class MultiHeadRecurrentMemory:
    """
    P21: Multi-Head Recurrent Memory Agents (UW-Madison)
    arXiv:2607.01523, 2026-07-01
    """

    def __init__(self, num_heads: int = 8, mem_capacity: int = 1024):
        self.num_heads = num_heads
        self.mem_capacity = mem_capacity
        self.heads: list[MemoryHead] = [
            MemoryHead(head_id=i) for i in range(num_heads)
        ]
        self.lru_tracker: list[int] = []
        self.retention_log: list[float] = []
        self.total_writes: int = 0
        self.overwrites: int = 0

    def select_head_for_update(self) -> int:
        if len(self.lru_tracker) < self.num_heads:
            head_id = len(self.lru_tracker)
        else:
            head_id = self.lru_tracker[0]
        if head_id in self.lru_tracker:
            self.lru_tracker.remove(head_id)
        self.lru_tracker.append(head_id)
        return head_id

    def update(self, new_memory: str, capture_score: float = 1.0) -> int:
        head_id = self.select_head_for_update()
        old_content = self.heads[head_id].content
        if old_content:
            self.overwrites += 1
        self.heads[head_id].content = new_memory
        self.heads[head_id].last_updated = time.time()
        self.heads[head_id].update_count += 1
        self.total_writes += 1
        return head_id

    def read_all(self) -> str:
        parts = [h.content for h in self.heads if h.content]
        return "\n---\n".join(parts)

    def read_head(self, head_id: int) -> str:
        return self.heads[head_id].content

    def compute_retention_rate(self) -> float:
        if self.total_writes == 0:
            return 1.0
        return 1.0 - (self.overwrites / self.total_writes)

    def get_head_utilization(self) -> dict:
        counts = [h.update_count for h in self.heads]
        total = sum(counts)
        if total == 0:
            return {h.head_id: 0.0 for h in self.heads}
        return {h.head_id: c / total for h in self.heads for c in [h.update_count] if h.head_id >= 0}

    def diagnostics(self) -> dict:
        return {
            "num_heads": self.num_heads,
            "retention_rate": f"{self.compute_retention_rate() * 100:.2f}%",
            "total_writes": self.total_writes,
            "overwrites": self.overwrites,
        }

print("[P21] MultiHeadRecurrentMemory (MHM-LRU) initialized")

# ============ M41: ContextNestVerifiableGovernance ============

class ContextNestVerifiableGovernance:
    """
    P22: ContextNest: Verifiable Context Governance (arXiv:2607.02116)
    """

    def __init__(self):
        self.provenance_chain: dict[str, ProvenanceRecord] = {}
        self.snapshots: dict[str, dict] = {}
        self.verification_log: list[dict] = []

    def record_provenance(self, source: str, content: Any,
                          parent_id: str = None) -> str:
        record_id = f"prov_{uuid.uuid4().hex[:10]}"
        content_str = json.dumps(str(content), sort_keys=True)
        integrity_hash = hashlib.sha256(content_str.encode()).hexdigest()[:16]
        record = ProvenanceRecord(
            record_id=record_id, source=source,
            timestamp=time.time(), integrity_hash=integrity_hash,
            parent_record=parent_id
        )
        self.provenance_chain[record_id] = record
        return record_id

    def verify_integrity(self, record_id: str, current_content: Any) -> bool:
        record = self.provenance_chain.get(record_id)
        if not record:
            return False
        current_hash = hashlib.sha256(
            json.dumps(str(current_content), sort_keys=True).encode()
        ).hexdigest()[:16]
        result = current_hash == record.integrity_hash
        self.verification_log.append({
            "record_id": record_id, "verified": result, "timestamp": time.time()
        })
        return result

    def snapshot(self, snapshot_id: str, state: dict):
        state_copy = {}
        for k, v in state.items():
            try:
                state_copy[k] = str(v)[:500]
            except Exception:
                state_copy[k] = "unserializable"
        self.snapshots[snapshot_id] = {
            "timestamp": time.time(), "state": state_copy,
            "provenance_count": len(self.provenance_chain),
        }

    def reconstruct(self, snapshot_id: str) -> Optional[dict]:
        return self.snapshots.get(snapshot_id, {}).get("state")

    def trace_lineage(self, record_id: str) -> list[str]:
        lineage = []
        current = record_id
        while current and current in self.provenance_chain:
            lineage.append(current)
            current = self.provenance_chain[current].parent_record
        return lineage

    def diagnostics(self) -> dict:
        return {
            "provenance_records": len(self.provenance_chain),
            "snapshots": len(self.snapshots),
            "verifications": len(self.verification_log),
        }

print("[P22] ContextNestVerifiableGovernance initialized")


# ============ M42: ElephantAgentStateContinuity ============

class ElephantAgentStateContinuity:
    """
    P23: ElephantAgent: Contextual State Continuity (arXiv:2607.01919)
    """

    def __init__(self, drift_threshold: float = 0.3):
        self.drift_threshold = drift_threshold
        self.state_history: list[ContinuityState] = []
        self.poison_alerts: list[dict] = []
        self.tool_invocations: list[dict] = []

    def compute_state_vector(self, context_summary: str) -> list[float]:
        h = hashlib.sha256(context_summary.encode()).digest()
        return [b / 255.0 for b in h[:16]]

    def check_continuity(self, current_context: str,
                         expected_tools: list[str] = None) -> dict:
        vector = self.compute_state_vector(current_context)
        result = {"continuity_preserved": True, "memory_poisoning": False,
                  "tool_poisoning": False, "drift_magnitude": 0.0}
        if self.state_history:
            prev = self.state_history[-1].state_vector
            if len(prev) == len(vector):
                dot = sum(a * b for a, b in zip(prev, vector))
                mag_a = math.sqrt(sum(a * a for a in prev))
                mag_b = math.sqrt(sum(b * b for b in vector))
                cosine = dot / (mag_a * mag_b + 1e-10)
                drift = 1.0 - cosine
                result["drift_magnitude"] = drift
                if drift > self.drift_threshold:
                    result["memory_poisoning"] = True
                    result["continuity_preserved"] = False
        if expected_tools and self.tool_invocations:
            recent_tools = [t["tool_name"] for t in self.tool_invocations[-5:]]
            unexpected = set(recent_tools) - set(expected_tools)
            if unexpected:
                result["tool_poisoning"] = True
                result["continuity_preserved"] = False
        state = ContinuityState(
            state_vector=vector, timestamp=time.time(),
            expected_range=(0.0, self.drift_threshold),
            drift_detected=result["memory_poisoning"] or result["tool_poisoning"]
        )
        self.state_history.append(state)
        if not result["continuity_preserved"]:
            self.poison_alerts.append({
                "timestamp": time.time(),
                "type": "memory" if result["memory_poisoning"] else "tool",
                "drift": result["drift_magnitude"],
            })
        return result

    def log_tool_invocation(self, tool_name: str, params: dict):
        self.tool_invocations.append({
            "tool_name": tool_name, "params": str(params)[:200],
            "timestamp": time.time()
        })

    def diagnostics(self) -> dict:
        return {
            "state_snapshots": len(self.state_history),
            "poison_alerts": len(self.poison_alerts),
            "tool_invocations": len(self.tool_invocations),
            "drift_threshold": self.drift_threshold,
        }

print("[P23] ElephantAgentStateContinuity initialized")

# ============ M43: ConstraintSteerableOversight ============

class ConstraintSteerableOversight:
    """
    P24: Steerability via Constraints (arXiv:2607.02389)
    """

    def __init__(self):
        self.constraints: dict[str, dict] = {}
        self.violations: list[dict] = []
        self.backdoor_patterns: set[str] = set()

    def add_constraint(self, constraint_id: str, rule: str,
                       category: str = "general", severity: str = "medium"):
        self.constraints[constraint_id] = {
            "rule": rule, "category": category, "severity": severity,
            "added_at": time.time(), "violation_count": 0,
        }

    def evaluate(self, action: str, code_context: str = "",
                 agent_output: str = "") -> dict:
        results = {"passed": True, "violations": [], "backdoor_detected": False}
        for cid, constraint in self.constraints.items():
            rule_lower = constraint["rule"].lower()
            action_lower = action.lower()
            context_lower = (code_context + agent_output).lower()
            keywords = rule_lower.split()
            action_match = all(kw in action_lower for kw in keywords) if keywords else False
            context_match = all(kw in context_lower for kw in keywords) if keywords else False
            if action_match or context_match:
                constraint["violation_count"] += 1
                violation = {
                    "constraint_id": cid, "rule": constraint["rule"],
                    "severity": constraint["severity"], "timestamp": time.time(),
                }
                results["violations"].append(violation)
                self.violations.append(violation)
                if constraint["severity"] in ["critical", "high"]:
                    results["passed"] = False
        backdoor_signatures = [
            "eval(", "exec(", "__import__", "os.system", "subprocess",
            "base64.decode", "hidden", "backdoor", "c2_server",
        ]
        combined = action + code_context + agent_output
        for sig in backdoor_signatures:
            if sig.lower() in combined.lower():
                results["backdoor_detected"] = True
                self.backdoor_patterns.add(sig)
                results["passed"] = False
        return results

    def diagnostics(self) -> dict:
        return {
            "active_constraints": len(self.constraints),
            "total_violations": len(self.violations),
            "backdoor_patterns": len(self.backdoor_patterns),
        }

print("[P24] ConstraintSteerableOversight initialized")

# ============ M44: OnlineSafetyMonitor ============

class OnlineSafetyMonitor:
    """
    P25: Online Safety Monitoring for LLMs (arXiv:2607.02510)
    """

    def __init__(self, risk_threshold: float = 0.7):
        self.risk_threshold = risk_threshold
        self.alarm_history: list[SafetyAlarm] = []
        self.observation_window: list[float] = []
        self.total_observations: int = 0
        self.blocked_actions: int = 0

    def observe(self, action: str, model_output: str,
                confidence: float = 1.0) -> dict:
        risk_score = self._compute_risk_score(action, model_output)
        self.observation_window.append(risk_score)
        self.total_observations += 1
        if len(self.observation_window) > 50:
            self.observation_window.pop(0)
        triggered = risk_score > self.risk_threshold
        recent = self.observation_window[-5:] if len(self.observation_window) >= 5 else []
        trend_up = (len(recent) >= 3 and
                    recent[-1] > recent[0] and
                    sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1]) >= len(recent) - 1)
        if triggered or trend_up:
            severity = "critical" if risk_score > 0.9 else "high" if risk_score > 0.7 else "medium"
            alarm = SafetyAlarm(
                alarm_id=f"alarm_{uuid.uuid4().hex[:8]}",
                severity=severity, source="OnlineSafetyMonitor",
                message=f"Risk score {risk_score:.3f} exceeded threshold {self.risk_threshold}",
                timestamp=time.time(), risk_score=risk_score,
                blocked=severity in ["critical", "high"]
            )
            self.alarm_history.append(alarm)
            if alarm.blocked:
                self.blocked_actions += 1
        return {
            "risk_score": risk_score, "triggered": triggered,
            "trend_up": trend_up, "blocked": triggered,
            "confidence": confidence,
        }

    def _compute_risk_score(self, action: str, model_output: str) -> float:
        combined = (action + " " + model_output).lower()
        risk_signals = {
            "delete": 0.8, "remove": 0.8, "overwrite": 0.7,
            "execute": 0.7, "sudo": 0.95, "root": 0.9,
            "rm -rf": 1.0, "format": 1.0, "dd if=": 0.95,
            "chmod 777": 0.7, "wget | sh": 0.9, "curl | bash": 0.9,
            "/dev/null": 0.6, "kill": 0.8, "shutdown": 0.9,
        }
        scores = [v for k, v in risk_signals.items() if k in combined]
        if not scores:
            return random.uniform(0.1, 0.3)
        return max(scores) * (1.0 + 0.1 * (len(scores) - 1))

    def diagnostics(self) -> dict:
        return {
            "total_observations": self.total_observations,
            "alarms_triggered": len(self.alarm_history),
            "blocked_actions": self.blocked_actions,
            "risk_threshold": self.risk_threshold,
        }

print("[P25] OnlineSafetyMonitor initialized")


# ============ M45-M100: Round 2 & 3 占位模块 ============
# M45-M70: Round 2 (P26-P45) — 模块已在先前版本实现
# M71-M100: Round 3 (P46-P75) — 模块已在先前版本实现


# ============ M101: HippocampalComplementaryMemory [NEW, P76] ============

