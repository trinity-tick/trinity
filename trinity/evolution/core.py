"""
Meta-Evolution Core Engine.
The central loop that drives Trinity's self-evolution.

Architecture:
  Observe ─→ Analyze ─→ Plan ─→ Execute ─→ Certify ─→ (repeat)
    ↑                                                    │
    └────────────────────────────────────────────────────┘

Each phase is a module that can be independently evolved.

EvolutionState is serialized to JSON for cross-session persistence.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


class EvolutionPhase(Enum):
    """The five phases of each evolution cycle."""
    OBSERVE = "observe"
    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    CERTIFY = "certify"


@dataclass
class EvolutionCycle:
    """A single evolution cycle record."""
    cycle_id: str
    phase: EvolutionPhase
    started_at: float
    completed_at: Optional[float] = None
    observations: List[Dict[str, Any]] = field(default_factory=list)
    analysis: Dict[str, Any] = field(default_factory=dict)
    plan: Dict[str, Any] = field(default_factory=dict)
    execution_result: Dict[str, Any] = field(default_factory=dict)
    certificates: Dict[str, Any] = field(default_factory=dict)
    tick_count: int = 0

    def to_dict(self) -> Dict:
        return {
            "cycle_id": self.cycle_id,
            "phase": self.phase.value,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "observations_count": len(self.observations),
            "certificates": self.certificates,
            "tick_count": self.tick_count,
        }

    def duration(self) -> Optional[float]:
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        return None


@dataclass
class EvolutionState:
    """Persistent state of the evolution system."""
    version: str = "1.0"
    total_cycles: int = 0
    last_cycle_id: Optional[str] = None
    active_preferences: Dict[str, float] = field(default_factory=dict)
    active_patterns: Dict[str, float] = field(default_factory=dict)
    corrections_log: List[Dict[str, Any]] = field(default_factory=list)
    skill_scores: Dict[str, float] = field(default_factory=dict)
    cycle_history: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class MetaEvolution:
    """
    Meta-Evolution Engine.

    Runs the Observe → Analyze → Plan → Execute → Certify loop.
    Each cycle learns from experience and improves the system.

    Features:
      - Tick-based execution (one phase per tick)
      - JSON-serializable state for cross-session persistence
      - Integration with self-improving/ skill system
      - Integration with M112-M114 certification
    """

    def __init__(
        self,
        state_path: Optional[str] = None,
        skill_dir: Optional[str] = None,
    ):
        self.state_path = state_path or os.path.join(
            os.path.expanduser("~"), ".trinity", "evolution_state.json"
        )
        self.skill_dir = skill_dir or os.path.join(
            os.path.expanduser("~"), "self-improving"
        )
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)

        # Load or create state
        self.state = self._load_state()

        # Current cycle tracking
        self.current_cycle: Optional[EvolutionCycle] = None
        self._phase_queue: List[EvolutionPhase] = []

        # Observer hooks
        self._observation_hooks: List[Callable] = []
        # 2026-08-16 修复:注册默认观察钩子——从审计日志挖掘真实使用模式,
        # 让进化"空转"(维护链只传 action=scheduled)变成"真学"。
        # 此前 20 轮周期 preferences/patterns 恒 0,因无任何 observation 输入。
        try:
            self._observation_hooks.append(self._audit_observation_hook)
        except Exception:
            pass

        # Cross-session context
        self.session_context: Dict[str, Any] = {}

    # ── State Persistence ────────────────────────────────────────────

    def _load_state(self) -> EvolutionState:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                state = EvolutionState(**data)
                return state
            except Exception:
                pass
        return EvolutionState()

    def save_state(self):
        self.state.updated_at = time.time()
        data = {
            "version": self.state.version,
            "total_cycles": self.state.total_cycles,
            "last_cycle_id": self.state.last_cycle_id,
            "active_preferences": self.state.active_preferences,
            "active_patterns": self.state.active_patterns,
            "corrections_log": self.state.corrections_log[-100:],
            "skill_scores": self.state.skill_scores,
            "cycle_history": self.state.cycle_history[-50:],
            "created_at": self.state.created_at,
            "updated_at": self.state.updated_at,
        }
        with open(self.state_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    # ── Observation Hooks ────────────────────────────────────────────

    def register_observation_hook(self, hook: Callable):
        """Register a function that returns observations."""
        self._observation_hooks.append(hook)

    def _audit_observation_hook(self, context: Dict[str, Any]) -> List[Dict]:
        """Mine the audit log (search/ingest actions) for real usage patterns.

        从真实使用数据生成 observations,让进化周期有输入:
        - 高频搜索主题 → pattern 观察(按 action=search 的 details/timestamp 聚合)
        - 高频写入 agent → preference 观察
        失败静默:审计表不可读时不产出,不影响周期。
        """
        try:
            import sqlite3
            from pathlib import Path
            db = os.path.join(os.path.expanduser("~"), ".trinity", "store", "trinity_store.db")
            if not os.path.exists(db):
                return []
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            observations = []
            # 最近 24h 的高频搜索查询(前 5)
            rows = conn.execute(
                "SELECT agent_id, details, count(*) c FROM audit_log "
                "WHERE action='search' AND timestamp > ? "
                "GROUP BY agent_id, details ORDER BY c DESC LIMIT 5",
                (time.time() - 86400,)
            ).fetchall()
            for r in rows:
                q = (r["details"] or "")[:200]
                # details 可能是 JSON({"query": "..."}) 或纯文本——提取可读 query
                try:
                    _d = json.loads(q)
                    q = str(_d.get("query") or _d.get("q") or q)[:60]
                except Exception:
                    pass
                if q and not q.isspace():
                    observations.append({
                        "type": "pattern",
                        "key": f"frequent_search:{q}",
                        "description": f"高频检索主题(agent={r['agent_id']}, x{r['c']})",
                        "agent_id": r["agent_id"],
                    })
            # 最近 24h 高频写入 agent(前 3)
            rows2 = conn.execute(
                "SELECT agent_id, count(*) c FROM audit_log "
                "WHERE action IN ('ingest','STORE_MEMORY') AND timestamp > ? "
                "GROUP BY agent_id ORDER BY c DESC LIMIT 3",
                (time.time() - 86400,)
            ).fetchall()
            for r in rows2:
                observations.append({
                    "type": "preference",
                    "key": f"active_agent:{r['agent_id']}",
                    "description": f"活跃写入者(agent={r['agent_id']}, {r['c']}条)",
                    "agent_id": r["agent_id"],
                })
            conn.close()
            return observations
        except Exception:
            return []

    def observe(self, context: Dict[str, Any]) -> List[Dict]:
        """Collect observations from all hooks."""
        observations = []
        for hook in self._observation_hooks:
            try:
                result = hook(context)
                if result:
                    observations.extend(result if isinstance(result, list) else [result])
            except Exception as e:
                observations.append({
                    "type": "hook_error",
                    "hook": hook.__name__,
                    "error": str(e),
                })
        return observations

    # ── Evolution Loop ──────────────────────────────────────────────

    def tick(self, context: Optional[Dict] = None) -> Dict[str, Any]:
        """
        Execute one tick of the evolution loop.

        Each tick executes the next phase in sequence.
        Completes a full cycle every 5 ticks.

        Returns the current state and phase result.
        """
        if context is None:
            context = {}

        if self.current_cycle is None:
            # Start a new cycle
            self.current_cycle = EvolutionCycle(
                cycle_id=f"evo_{uuid.uuid4().hex[:10]}",
                phase=EvolutionPhase.OBSERVE,
                started_at=time.time(),
            )
            self._phase_queue = [
                EvolutionPhase.ANALYZE,
                EvolutionPhase.PLAN,
                EvolutionPhase.EXECUTE,
                EvolutionPhase.CERTIFY,
            ]

        # Execute current phase
        phase = self.current_cycle.phase
        result = self._execute_phase(phase, context)

        # Advance to next phase
        if self._phase_queue:
            self.current_cycle.phase = self._phase_queue.pop(0)
        else:
            # Cycle complete
            self.current_cycle.completed_at = time.time()
            self.current_cycle.certificates = result.get("certificates", {})
            self.state.total_cycles += 1
            self.state.last_cycle_id = self.current_cycle.cycle_id
            self.state.cycle_history.append(self.current_cycle.cycle_id)
            self.current_cycle = None
            self.save_state()

        return {
            "phase": phase.value,
            "cycle_id": self.current_cycle.cycle_id if self.current_cycle else None,
            "cycle_complete": self.current_cycle is None,
            "total_cycles": self.state.total_cycles,
            "result": result,
        }

    def _execute_phase(self, phase: EvolutionPhase, context: Dict) -> Dict:
        """Execute a single evolution phase."""
        if phase == EvolutionPhase.OBSERVE:
            observations = self.observe(context)
            if self.current_cycle:
                self.current_cycle.observations = observations
            return {"observations": observations, "count": len(observations)}

        elif phase == EvolutionPhase.ANALYZE:
            analysis = self._analyze(
                self.current_cycle.observations if self.current_cycle else [],
                context
            )
            if self.current_cycle:
                self.current_cycle.analysis = analysis
            return analysis

        elif phase == EvolutionPhase.PLAN:
            plan = self._plan(
                self.current_cycle.analysis if self.current_cycle else {},
                context
            )
            if self.current_cycle:
                self.current_cycle.plan = plan
            return plan

        elif phase == EvolutionPhase.EXECUTE:
            result = self._execute(
                self.current_cycle.plan if self.current_cycle else {},
                context
            )
            if self.current_cycle:
                self.current_cycle.execution_result = result
            return result

        elif phase == EvolutionPhase.CERTIFY:
            certs = self._certify(
                self.current_cycle.execution_result if self.current_cycle else {},
                context
            )
            if self.current_cycle:
                self.current_cycle.certificates = certs
            return {"certificates": certs}

        return {"error": f"Unknown phase: {phase}"}

    def _analyze(self, observations: List[Dict], context: Dict) -> Dict:
        """Phase 2: Analyze observations for patterns and insights."""
        # Categorize observations
        corrections = [o for o in observations if o.get("type") == "correction"]
        patterns = [o for o in observations if o.get("type") == "pattern"]
        preferences = [o for o in observations if o.get("type") == "preference"]

        # Detect frequency-based patterns
        pattern_counts: Dict[str, int] = {}
        for p in patterns:
            key = p.get("key", p.get("description", "unknown"))
            pattern_counts[key] = pattern_counts.get(key, 0) + 1

        # Update state
        for key, count in pattern_counts.items():
            if count >= 3:
                self.state.active_patterns[key] = 1.0  # confirmed
            elif count >= 1:
                self.state.active_patterns[key] = min(
                    self.state.active_patterns.get(key, 0) + 0.3, 1.0
                )

        return {
            "corrections_found": len(corrections),
            "patterns_detected": len(pattern_counts),
            "preferences_found": len(preferences),
            "pattern_summary": dict(sorted(pattern_counts.items(), key=lambda x: -x[1])[:10]),
            "skill_impact": self.state.skill_scores,
        }

    def _plan(self, analysis: Dict, context: Dict) -> Dict:
        """Phase 3: Plan actions based on analysis."""
        actions = []

        # If corrections found, plan to update corrections.md
        if analysis.get("corrections_found", 0) > 0:
            actions.append({
                "type": "update_corrections",
                "priority": "high",
                "target": os.path.join(self.skill_dir, "corrections.md"),
            })

        # If new patterns confirmed, plan to update memory.md
        if analysis.get("patterns_detected", 0) > 0:
            actions.append({
                "type": "update_memory",
                "priority": "medium",
                "target": os.path.join(self.skill_dir, "memory.md"),
            })

        # Heartbeat check
        actions.append({
            "type": "heartbeat",
            "priority": "low",
            "target": os.path.join(self.skill_dir, "heartbeat-state.md"),
        })

        return {
            "actions": actions,
            "total_actions": len(actions),
            "state_snapshot": {
                "preferences": len(self.state.active_preferences),
                "patterns": len(self.state.active_patterns),
                "corrections": len(self.state.corrections_log),
                "skills": len(self.state.skill_scores),
            },
        }

    def _execute(self, plan: Dict, context: Dict) -> Dict:
        """Phase 4: Execute planned actions."""
        results = []
        for action in plan.get("actions", []):
            try:
                if action["type"] == "update_memory":
                    results.append(self._update_memory_file(action))
                elif action["type"] == "update_corrections":
                    results.append(self._update_corrections_file(action))
                elif action["type"] == "heartbeat":
                    results.append(self._heartbeat_check(action))
                else:
                    results.append({"action": action["type"], "status": "skipped"})
            except Exception as e:
                results.append({"action": action["type"], "status": "error", "error": str(e)})

        return {
            "executed": len(results),
            "successful": sum(1 for r in results if r.get("status") == "done"),
            "results": results,
        }

    def _certify(self, execution_result: Dict, context: Dict) -> Dict:
        """Phase 5: Certify the evolution cycle."""
        executed = execution_result.get("executed", 0)
        successful = execution_result.get("successful", 0)

        # Simple certification
        success_rate = successful / max(executed, 1)

        certs = {
            "cycle_complete": True,
            "success_rate": success_rate,
            "passed": success_rate >= 0.5,
        }

        # Integrate with M112 if available
        try:
            from trinity.modules.second_brain.engine import SecondBrainV636
            sb = SecondBrainV636()
            m112 = sb.m112 if hasattr(sb, 'm112') else None
            if m112:
                certs["m112_available"] = True
        except Exception:
            pass

        return certs

    # ── File Management Methods ─────────────────────────────────────

    def _update_memory_file(self, action: Dict) -> Dict:
        """Update memory.md with new patterns/preferences."""
        path = action.get("target", os.path.join(self.skill_dir, "memory.md"))
        os.makedirs(os.path.dirname(path), exist_ok=True)

        header = "# Self-Improving Memory\n\n"
        confirmed = "## Confirmed Preferences\n"
        for pref, score in sorted(self.state.active_preferences.items(), key=lambda x: -x[1]):
            if score > 0.8:
                confirmed += f"- {pref}\n"

        patterns = "\n## Active Patterns\n"
        for pat, score in sorted(self.state.active_patterns.items(), key=lambda x: -x[1]):
            if score > 0.5:
                patterns += f"- {pat} (confidence: {score:.1f})\n"

        recent = "\n## Recent\n- Updated: " + datetime.now().isoformat()

        content = header + confirmed + patterns + recent + "\n"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return {"action": "update_memory", "status": "done", "file": path}

    def _update_corrections_file(self, action: Dict) -> Dict:
        """Log corrections to corrections.md."""
        path = action.get("target", os.path.join(self.skill_dir, "corrections.md"))
        os.makedirs(os.path.dirname(path), exist_ok=True)

        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write("# Corrections Log\n\n## Initialized\n- System initialized\n")

        return {"action": "update_corrections", "status": "done", "file": path}

    def _heartbeat_check(self, action: Dict) -> Dict:
        """Perform heartbeat state check."""
        path = action.get("target", os.path.join(self.skill_dir, "heartbeat-state.md"))
        os.makedirs(os.path.dirname(path), exist_ok=True)

        content = f"""# Self-Improving Heartbeat State

last_heartbeat: {datetime.now().isoformat()}
total_cycles: {self.state.total_cycles}
active_preferences: {len(self.state.active_preferences)}
active_patterns: {len(self.state.active_patterns)}
corrections_log: {len(self.state.corrections_log)}
skill_scores: {len(self.state.skill_scores)}
"""
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

        return {"action": "heartbeat", "status": "done", "file": path}

    # ── Diagnostics ─────────────────────────────────────────────────

    def diagnostics(self) -> Dict[str, Any]:
        return {
            "module": "MetaEvolution",
            "version": self.state.version,
            "total_cycles": self.state.total_cycles,
            "last_cycle": self.state.last_cycle_id,
            "preferences_count": len(self.state.active_preferences),
            "patterns_count": len(self.state.active_patterns),
            "corrections_count": len(self.state.corrections_log),
            "skills_count": len(self.state.skill_scores),
            "cycle_history_count": len(self.state.cycle_history),
            "state_path": self.state_path,
            "skill_dir": self.skill_dir,
            "in_cycle": self.current_cycle is not None,
        }
