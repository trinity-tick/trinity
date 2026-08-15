"""
# status: orphan (2026-08-15 audit, not in runtime path)
P6-5: Context Structured Workspace (对标 Cat ACL2026)
======================================================

三区模型：
  - StableTaskSemantics:    任务目标/约束/状态（稳定、不变）
  - CondensedLongTermMemory: 压缩后的长期记忆（结构化摘要）
  - HighFidelityRecent:     近期交互高保真保留（完整记录）

主动里程碑压缩：
  Agent 自行判断何时压缩历史为可执行摘要，主动调用压缩。
  将上下文管理提升为可调用工具，集成到 Agent 决策流程中。

Cat 核心设计：
  - 结构化上下文工作区（三区模型）
  - 主动压缩：在适当里程碑将历史轨迹压缩为可操作摘要
  - CaT-Generator：基于轨迹级监督框架的数据构建管线
  - SWE-Compressor：上下文感知的压缩模型

Reference: Liu et al., "Context as a Tool: Context Management for
           Long-Horizon SWE-Agents", ACL 2026 Findings.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ── 枚举与常量 ───────────────────────────────────────────────────────

class WorkspaceZone(Enum):
    """工作区三区标识。"""
    STABLE = "stable_task_semantics"
    CONDENSED = "condensed_long_term_memory"
    HIGH_FIDELITY = "high_fidelity_recent"


class CompressionTrigger(Enum):
    """压缩触发条件。"""
    TOKEN_THRESHOLD = "token_threshold"
    TIME_INTERVAL = "time_interval"
    TASK_MILESTONE = "task_milestone"
    SEMANTIC_BOUNDARY = "semantic_boundary"
    MEMORY_PRESSURE = "memory_pressure"
    MANUAL = "manual"


class CompressionStrategy(Enum):
    """压缩策略。"""
    EXTRACTIVE = "extractive"
    ABSTRACTIVE = "abstractive"
    KEY_VALUE = "key_value"
    HYBRID = "hybrid"


class MilestoneType(Enum):
    """里程碑类型。"""
    SUBTASK_COMPLETE = "subtask_complete"
    ERROR_ENCOUNTERED = "error_encountered"
    DECISION_POINT = "decision_point"
    EXTERNAL_FEEDBACK = "external_feedback"
    TIMEOUT = "timeout"
    BUDGET_EXHAUSTED = "budget_exhausted"


# ── 数据结构 ─────────────────────────────────────────────────────────

@dataclass
class TaskSemantics:
    """稳定任务语义。"""
    task_id: str = field(default_factory=lambda: f"tsk_{uuid.uuid4().hex[:12]}")
    goal: str = ""
    constraints: List[str] = field(default_factory=list)
    state: str = "pending"
    priority: int = 0
    deadline: Optional[float] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


@dataclass
class CondensedMemory:
    """压缩后的长期记忆条目。"""
    memory_id: str = field(default_factory=lambda: f"cnd_{uuid.uuid4().hex[:12]}")
    summary: str = ""
    key_facts: List[str] = field(default_factory=list)
    source_span: Tuple[int, int] = (0, 0)
    confidence: float = 1.0
    compressed_at: float = field(default_factory=time.time)
    token_saved: int = 0


@dataclass
class RecentInteraction:
    """近期高保真交互记录。"""
    interaction_id: str = field(default_factory=lambda: f"int_{uuid.uuid4().hex[:12]}")
    turn_number: int = 0
    role: str = "user"
    content: str = ""
    timestamp: float = field(default_factory=time.time)
    token_count: int = 0
    importance: float = 0.5


@dataclass
class CompressionEvent:
    """压缩事件记录。"""
    event_id: str = field(default_factory=lambda: f"cmp_{uuid.uuid4().hex[:12]}")
    trigger: CompressionTrigger = CompressionTrigger.TOKEN_THRESHOLD
    strategy: CompressionStrategy = CompressionStrategy.HYBRID
    milestone: Optional[MilestoneType] = None
    source_count: int = 0
    result_count: int = 0
    token_before: int = 0
    token_after: int = 0
    timestamp: float = field(default_factory=time.time)


@dataclass
class WorkspaceStats:
    """工作区统计快照。"""
    stable_goal: str = ""
    stable_constraints: int = 0
    condensed_count: int = 0
    condensed_total_tokens: int = 0
    recent_count: int = 0
    recent_total_tokens: int = 0
    compression_count: int = 0
    total_token_saved: int = 0


# ── 辅助类：三区存储管理 ────────────────────────────────────────────

class _WorkspaceAllocator:
    """管理三区存储：Stable / Condensed / HighFidelity 分区的增删查。

    从 ContextWorkspace 拆分而来，负责底层数据结构的 CRUD 操作。
    """

    def __init__(self, max_recent_interactions: int = 50):
        self._stable: Dict[str, TaskSemantics] = {}
        self._condensed: Dict[str, CondensedMemory] = {}
        self._recent: deque = deque(maxlen=max_recent_interactions)
        self._lock = threading.RLock()

    # ── Zone 1: 稳定任务语义 ─────────────────────────────────────

    def set_task(
        self, goal: str, constraints: Optional[List[str]] = None,
        priority: int = 0, deadline: Optional[float] = None,
    ) -> TaskSemantics:
        task = TaskSemantics(
            goal=goal, constraints=constraints or [],
            priority=priority, deadline=deadline, state="active",
        )
        with self._lock:
            self._stable[task.task_id] = task
        logger.info("ContextWorkspace: task set — %s (priority=%d)", goal[:80], priority)
        return task

    def update_task_state(self, task_id: str, new_state: str) -> bool:
        with self._lock:
            task = self._stable.get(task_id)
            if task is None:
                return False
            task.state = new_state
            task.updated_at = time.time()
        return True

    def get_task(self, task_id: Optional[str] = None) -> Optional[TaskSemantics]:
        with self._lock:
            if task_id:
                return self._stable.get(task_id)
            active = [t for t in self._stable.values() if t.state == "active"]
            if active:
                return max(active, key=lambda t: t.created_at)
            return None

    def get_stable(self) -> Dict[str, TaskSemantics]:
        with self._lock:
            return dict(self._stable)

    # ── Zone 2: 压缩长期记忆 ─────────────────────────────────────

    def add_condensed_memory(
        self, summary: str, key_facts: Optional[List[str]] = None,
        source_span: Tuple[int, int] = (0, 0), confidence: float = 1.0,
        token_saved: int = 0,
    ) -> CondensedMemory:
        memory = CondensedMemory(
            summary=summary, key_facts=key_facts or [],
            source_span=source_span, confidence=confidence,
            token_saved=token_saved,
        )
        with self._lock:
            self._condensed[memory.memory_id] = memory
        return memory

    def get_condensed_memories(
        self, limit: int = 20, min_confidence: float = 0.0,
    ) -> List[CondensedMemory]:
        with self._lock:
            memories = list(self._condensed.values())
            memories = [m for m in memories if m.confidence >= min_confidence]
            memories.sort(key=lambda m: m.compressed_at, reverse=True)
            return memories[:limit]

    def summarize_condensed(self, top_k: int = 5) -> str:
        memories = self.get_condensed_memories(limit=top_k)
        if not memories:
            return ""
        lines = ["[Condensed Long-Term Memory Summary]"]
        for i, m in enumerate(memories, 1):
            facts_str = "; ".join(m.key_facts[:3]) if m.key_facts else "—"
            lines.append(
                f"{i}. {m.summary[:200]} "
                f"[Facts: {facts_str}] "
                f"(confidence={m.confidence:.2f}, saved={m.token_saved}tok)"
            )
        return "\n".join(lines)

    def get_condensed_count(self) -> int:
        with self._lock:
            return len(self._condensed)

    def get_condensed_total_tokens(self) -> int:
        with self._lock:
            return sum(len(m.summary) // 4 for m in self._condensed.values())

    # ── Zone 3: 近期高保真交互 ───────────────────────────────────

    def log_interaction(
        self, role: str, content: str, importance: float = 0.5,
        turn_number: int = 0,
    ) -> RecentInteraction:
        token_count = max(1, len(content) // 4)
        interaction = RecentInteraction(
            turn_number=turn_number, role=role, content=content,
            importance=importance, token_count=token_count,
        )
        with self._lock:
            self._recent.append(interaction)
        return interaction

    def get_recent_interactions(
        self, limit: int = 20, min_importance: float = 0.0,
    ) -> List[RecentInteraction]:
        with self._lock:
            interactions = list(self._recent)
            interactions = [i for i in interactions if i.importance >= min_importance]
            return interactions[-limit:]

    def get_recent_token_count(self) -> int:
        with self._lock:
            return sum(i.token_count for i in self._recent)

    def get_recent_count(self) -> int:
        with self._lock:
            return len(self._recent)

    def get_all_recent(self) -> List[RecentInteraction]:
        with self._lock:
            return list(self._recent)

    def replace_recent(self, interactions: List[RecentInteraction], maxlen: int) -> None:
        with self._lock:
            self._recent = deque(interactions, maxlen=maxlen)

    def clear(self) -> None:
        with self._lock:
            self._stable.clear()
            self._condensed.clear()
            self._recent.clear()


# ── 辅助类：压缩调度与里程碑 ────────────────────────────────────────

class _PriorityScheduler:
    """管理压缩判断、执行、里程碑检测与辅助方法。

    从 ContextWorkspace 拆分而来。
    """

    def __init__(
        self, allocator: _WorkspaceAllocator,
        token_threshold_recent: int = 16000,
        token_threshold_condensed: int = 32000,
        default_strategy: CompressionStrategy = CompressionStrategy.HYBRID,
        max_recent_interactions: int = 50,
    ):
        self._allocator = allocator
        self.token_threshold_recent = token_threshold_recent
        self.token_threshold_condensed = token_threshold_condensed
        self.default_strategy = default_strategy
        self.max_recent_interactions = max_recent_interactions

        self.compression_history: deque = deque(maxlen=200)
        self._milestone_listeners: Dict[MilestoneType, List[Callable]] = defaultdict(list)
        self._lock = threading.RLock()
        self._stats: Dict[str, int] = {
            "total_compressions": 0,
            "total_token_saved": 0,
            "total_milestones_detected": 0,
        }

    def should_compress(self) -> Tuple[bool, Optional[CompressionTrigger]]:
        recent_tokens = self._allocator.get_recent_token_count()
        recent_count = self._allocator.get_recent_count()
        condensed_tokens = self._allocator.get_condensed_total_tokens()

        if recent_tokens > self.token_threshold_recent:
            return True, CompressionTrigger.TOKEN_THRESHOLD
        if recent_count > self.max_recent_interactions:
            return True, CompressionTrigger.MEMORY_PRESSURE
        if condensed_tokens > self.token_threshold_condensed:
            return True, CompressionTrigger.MEMORY_PRESSURE
        return False, None

    def compress_recent_to_condensed(
        self, strategy: Optional[CompressionStrategy] = None,
        milestone: Optional[MilestoneType] = None, keep_last_n: int = 5,
    ) -> CompressionEvent:
        strategy = strategy or self.default_strategy
        interactions = self._allocator.get_all_recent()

        if len(interactions) <= keep_last_n:
            return CompressionEvent(
                trigger=CompressionTrigger.MANUAL, strategy=strategy,
                milestone=milestone, source_count=0, result_count=0,
                token_before=0, token_after=0,
            )

        to_compress = interactions[:-keep_last_n]
        token_before = sum(i.token_count for i in to_compress)
        important = [i for i in to_compress if i.importance >= 0.7]
        rest = [i for i in to_compress if i.importance < 0.7]

        compressed_count = 0
        token_after = 0

        if rest:
            if strategy in (CompressionStrategy.ABSTRACTIVE, CompressionStrategy.HYBRID):
                summary = self._generate_summary(rest)
                self._allocator.add_condensed_memory(
                    summary=summary,
                    key_facts=self._extract_key_facts(rest),
                    source_span=(
                        rest[0].turn_number if rest else 0,
                        rest[-1].turn_number if rest else 0,
                    ),
                    confidence=0.85,
                    token_saved=token_before - (len(summary) // 4),
                )
                compressed_count += 1
                token_after += len(summary) // 4

            if strategy in (CompressionStrategy.KEY_VALUE, CompressionStrategy.HYBRID):
                kv_entries = self._extract_key_value_pairs(rest)
                for kv in kv_entries:
                    self._allocator.add_condensed_memory(
                        summary=kv.get("summary", ""),
                        key_facts=kv.get("facts", []),
                        source_span=kv.get("span", (0, 0)),
                        confidence=0.75,
                        token_saved=kv.get("token_saved", 0),
                    )
                    compressed_count += 1
                    token_after += len(kv.get("summary", "")) // 4

        new_recent = deque(
            list(important[-keep_last_n:]), maxlen=self.max_recent_interactions,
        )
        new_recent.extendleft(reversed(interactions[-keep_last_n:]))
        self._allocator.replace_recent(list(new_recent), self.max_recent_interactions)

        event = CompressionEvent(
            trigger=CompressionTrigger.TOKEN_THRESHOLD, strategy=strategy,
            milestone=milestone, source_count=len(to_compress),
            result_count=compressed_count, token_before=token_before,
            token_after=token_after,
        )
        self.compression_history.append(event)
        self._stats["total_compressions"] += 1
        self._stats["total_token_saved"] += (token_before - token_after)

        logger.info(
            "ContextWorkspace: compressed %d interactions -> %d memories "
            "(saved %d tokens, strategy=%s)",
            len(to_compress), compressed_count,
            token_before - token_after, strategy.value,
        )
        return event

    def register_milestone_listener(
        self, milestone_type: MilestoneType, callback: Callable,
    ) -> None:
        with self._lock:
            self._milestone_listeners[milestone_type].append(callback)

    def detect_trigger_compress(
        self, milestone: MilestoneType, auto_compress: bool = True,
    ) -> Optional[CompressionEvent]:
        self._stats["total_milestones_detected"] += 1
        if not auto_compress:
            return None
        should, _ = self.should_compress()
        if not should:
            return None
        event = self.compress_recent_to_condensed(milestone=milestone)
        listeners = self._milestone_listeners.get(milestone, [])
        for cb in listeners:
            try:
                cb(event)
            except Exception as exc:
                logger.warning("ContextWorkspace: milestone listener error: %s", exc)
        return event

    def clear_listeners(self) -> None:
        with self._lock:
            self._milestone_listeners.clear()

    def get_stat(self, key: str) -> int:
        return self._stats.get(key, 0)

    def reset_stats(self) -> None:
        for k in self._stats:
            self._stats[k] = 0

    # ── 私有辅助 ─────────────────────────────────────────────────

    def _generate_summary(self, interactions: List[RecentInteraction]) -> str:
        if not interactions:
            return ""
        roles_summary: Dict[str, int] = defaultdict(int)
        for i in interactions:
            roles_summary[i.role] += 1
        total_chars = sum(len(i.content) for i in interactions)
        important_ones = [i for i in interactions if i.importance >= 0.6]
        lines = [
            f"Summary of {len(interactions)} interactions "
            f"(turns {interactions[0].turn_number}-{interactions[-1].turn_number}):",
            f"Roles: " + ", ".join(f"{r}: {c}" for r, c in roles_summary.items()),
            f"Total content: ~{total_chars} chars",
        ]
        if important_ones:
            lines.append("Key interactions:")
            for imp in important_ones[:3]:
                lines.append(f"  - [{imp.role}] {imp.content[:150]}...")
        return "\n".join(lines)

    def _extract_key_facts(self, interactions: List[RecentInteraction]) -> List[str]:
        facts = []
        for i in interactions:
            if i.importance >= 0.6:
                facts.append(i.content[:80].strip())
                if len(facts) >= 5:
                    break
        return facts

    def _extract_key_value_pairs(
        self, interactions: List[RecentInteraction],
    ) -> List[Dict[str, Any]]:
        entries = []
        user_msgs = [i for i in interactions if i.role == "user"]
        if user_msgs:
            combined = "User requests: " + "; ".join(
                m.content[:60] for m in user_msgs[:5]
            )
            entries.append({
                "summary": combined[:300],
                "facts": [m.content[:80] for m in user_msgs[:3]],
                "span": (user_msgs[0].turn_number, user_msgs[-1].turn_number),
                "token_saved": sum(m.token_count for m in user_msgs) - len(combined) // 4,
            })
        return entries


# ── 上下文工作区 Facade ──────────────────────────────────────────────

class ContextWorkspace:
    """Cat ACL2026 风格三区结构化工作区（Stable + Condensed + HighFidelity）。"""

    def __init__(self, max_recent_interactions: int = 50, token_threshold_recent: int = 16000,
                 token_threshold_condensed: int = 32000,
                 default_strategy: CompressionStrategy = CompressionStrategy.HYBRID,
                 auto_compress: bool = True):
        self.max_recent_interactions = max_recent_interactions
        self.token_threshold_recent = token_threshold_recent
        self.token_threshold_condensed = token_threshold_condensed
        self.default_strategy = default_strategy; self.auto_compress = auto_compress
        self._alloc = _WorkspaceAllocator(max_recent_interactions=max_recent_interactions)
        self._scheduler = _PriorityScheduler(
            allocator=self._alloc, token_threshold_recent=token_threshold_recent,
            token_threshold_condensed=token_threshold_condensed,
            default_strategy=default_strategy, max_recent_interactions=max_recent_interactions)
        self._stats: Dict[str, int] = {"total_interactions_logged": 0}

    # ── Zone 1: Stable ──
    def set_task(self, goal: str, constraints: Optional[List[str]] = None,
                 priority: int = 0, deadline: Optional[float] = None) -> TaskSemantics:
        return self._alloc.set_task(goal, constraints, priority, deadline)
    def update_task_state(self, task_id: str, new_state: str) -> bool:
        return self._alloc.update_task_state(task_id, new_state)
    def get_task(self, task_id: Optional[str] = None) -> Optional[TaskSemantics]:
        return self._alloc.get_task(task_id)

    # ── Zone 2: Condensed ──
    def add_condensed_memory(self, summary: str, key_facts: Optional[List[str]] = None,
                              source_span: Tuple[int, int] = (0, 0), confidence: float = 1.0,
                              token_saved: int = 0) -> CondensedMemory:
        return self._alloc.add_condensed_memory(summary, key_facts, source_span, confidence, token_saved)
    def get_condensed_memories(self, limit: int = 20, min_confidence: float = 0.0) -> List[CondensedMemory]:
        return self._alloc.get_condensed_memories(limit, min_confidence)
    def summarize_condensed(self, top_k: int = 5) -> str:
        return self._alloc.summarize_condensed(top_k)

    # ── Zone 3: HighFidelity ──
    def log_interaction(self, role: str, content: str, importance: float = 0.5) -> RecentInteraction:
        turn = self._stats["total_interactions_logged"] + 1
        result = self._alloc.log_interaction(role, content, importance, turn)
        self._stats["total_interactions_logged"] += 1; return result
    def get_recent_interactions(self, limit: int = 20, min_importance: float = 0.0) -> List[RecentInteraction]:
        return self._alloc.get_recent_interactions(limit, min_importance)
    def get_recent_token_count(self) -> int: return self._alloc.get_recent_token_count()

    # ── 压缩调度 ──
    def should_compress(self) -> Tuple[bool, Optional[CompressionTrigger]]:
        return self._scheduler.should_compress()
    def compress_recent_to_condensed(self, strategy: Optional[CompressionStrategy] = None,
                                      milestone: Optional[MilestoneType] = None,
                                      keep_last_n: int = 5) -> CompressionEvent:
        return self._scheduler.compress_recent_to_condensed(strategy, milestone, keep_last_n)
    def register_milestone_listener(self, milestone_type: MilestoneType,
                                     callback: Callable) -> None:
        self._scheduler.register_milestone_listener(milestone_type, callback)
    def detect_trigger_compress(self, milestone: MilestoneType) -> Optional[CompressionEvent]:
        return self._scheduler.detect_trigger_compress(milestone, self.auto_compress)

    # ── 快照/统计/重置 ──
    def snapshot(self) -> WorkspaceStats:
        task = self._alloc.get_task()
        return WorkspaceStats(
            stable_goal=task.goal[:100] if task else "",
            stable_constraints=len(task.constraints) if task else 0,
            condensed_count=self._alloc.get_condensed_count(),
            condensed_total_tokens=self._alloc.get_condensed_total_tokens(),
            recent_count=self._alloc.get_recent_count(),
            recent_total_tokens=self._alloc.get_recent_token_count(),
            compression_count=len(self._scheduler.compression_history),
            total_token_saved=self._scheduler.get_stat("total_token_saved"))
    def statistics(self) -> Dict[str, Any]:
        snap = self.snapshot()
        return {"stable_task_active": bool(snap.stable_goal),
                "stable_goal_preview": snap.stable_goal,
                "stable_constraints_count": snap.stable_constraints,
                "condensed_memory_count": snap.condensed_count,
                "condensed_total_tokens": snap.condensed_total_tokens,
                "recent_interaction_count": snap.recent_count,
                "recent_total_tokens": snap.recent_total_tokens,
                "compression_events": snap.compression_count,
                "total_token_saved": snap.total_token_saved,
                "total_interactions_logged": self._stats["total_interactions_logged"],
                "total_compressions": self._scheduler.get_stat("total_compressions"),
                "total_milestones": self._scheduler.get_stat("total_milestones_detected"),
                "token_threshold_recent": self.token_threshold_recent,
                "token_threshold_condensed": self.token_threshold_condensed,
                "auto_compress": self.auto_compress}
    def reset(self) -> None:
        self._alloc.clear(); self._scheduler.compression_history.clear()
        self._scheduler.clear_listeners(); self._scheduler.reset_stats()
        for k in self._stats: self._stats[k] = 0

