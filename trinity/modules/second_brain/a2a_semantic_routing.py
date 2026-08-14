"""
P26-1: A2A Semantic Routing — 对标 A2A Protocol (Google, 2026.04)
三元语: Register → Discover → Route → Track
设计要点:
  - AgentCard 为 agent 能力名片 dataclass，含 endpoint/capacity_score
  - CapabilityRegistry 维护 agent 注册表，支持能力发现
  - SemanticRouter 基于语义匹配选择最优 agent
  - TaskLifecycleManager 跟踪任务生命周期（pending→running→completed/failed）
  - discover_and_route 一站式发现+路由，含 fallback 策略
"""

from __future__ import annotations

import logging
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentCard:
    """A2A Agent 能力名片。"""

    agent_id: str
    capabilities: list[str] = field(default_factory=list)
    endpoint: str = ""
    status: str = "active"
    capacity_score: float = 0.8
    last_heartbeat: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class TaskRecord:
    task_id: str
    agent_id: str
    status: TaskStatus = TaskStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Optional[dict] = None


class CapabilityRegistry:
    """A2A 能力注册表 — agent 注册/发现/心跳刷新。"""

    def __init__(self) -> None:
        self._agents: dict[str, AgentCard] = {}
        self._lock = threading.RLock()

    def register(self, agent_card: AgentCard) -> None:
        """注册或更新 agent 名片。"""
        with self._lock:
            agent_card.last_heartbeat = time.time()
            self._agents[agent_card.agent_id] = agent_card

    def unregister(self, agent_id: str) -> bool:
        with self._lock:
            return self._agents.pop(agent_id, None) is not None

    def discover(self, capability: str) -> list[AgentCard]:
        """按 capability 关键词发现匹配的全部 agent（active 且含该能力）。"""
        with self._lock:
            results: list[AgentCard] = []
            for card in self._agents.values():
                if card.status != "active":
                    continue
                if capability in card.capabilities:
                    results.append(card)
            return results

    def heartbeat(self, agent_id: str) -> bool:
        with self._lock:
            card = self._agents.get(agent_id)
            if card is None:
                return False
            card.last_heartbeat = time.time()
            return True

    def statistics(self) -> dict:
        with self._lock:
            return {
                "agent_count": len(self._agents),
                "active_count": sum(
                    1 for c in self._agents.values() if c.status == "active"
                ),
            }


class SemanticRouter:
    """A2A 语义路由器 — 基于 task_intent 与 agent capabilities 匹配。

    route() 使用 Jaccard + capacity_score 加权，选择最优 AgentCard。
    """

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry
        self._route_count = 0

    def _jaccard(self, intent_tokens: set[str], capabilities: list[str]) -> float:
        cap_set = set(c.lower() for c in capabilities)
        if not cap_set:
            return 0.0
        intersection = intent_tokens & cap_set
        union = intent_tokens | cap_set
        return len(intersection) / len(union) if union else 0.0

    def route(self, task_intent: str) -> Optional[AgentCard]:
        """基于语义匹配选择最优 agent。"""
        intent_tokens = set(task_intent.lower().split())
        candidates: list[tuple[AgentCard, float]] = []

        with self._registry._lock:
            for card in self._registry._agents.values():
                if card.status != "active":
                    continue
                jac = self._jaccard(intent_tokens, card.capabilities)
                score = jac * 0.7 + card.capacity_score * 0.3
                if score > 0:
                    candidates.append((card, score))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[1], reverse=True)
        self._route_count += 1
        return candidates[0][0]

    def statistics(self) -> dict:
        return {"route_count": self._route_count}


class TaskLifecycleManager:
    """A2A 任务生命周期管理器 — track→start→complete/fail 状态流转。"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskRecord] = {}
        self._lock = threading.RLock()

    def track(self, task_id: Optional[str] = None, agent_id: str = "") -> TaskRecord:
        """创建并追踪新任务。"""
        tid = task_id or str(uuid.uuid4())[:8]
        with self._lock:
            record = TaskRecord(task_id=tid, agent_id=agent_id)
            self._tasks[tid] = record
            return record

    def start(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            record = self._tasks.get(task_id)
            if record and record.status == TaskStatus.PENDING:
                record.status = TaskStatus.RUNNING
                record.updated_at = time.time()
            return record

    def complete(self, task_id: str, result: Optional[dict] = None) -> Optional[TaskRecord]:
        with self._lock:
            record = self._tasks.get(task_id)
            if record:
                record.status = TaskStatus.COMPLETED
                record.result = result
                record.updated_at = time.time()
            return record

    def fail(self, task_id: str, reason: Optional[str] = None) -> Optional[TaskRecord]:
        with self._lock:
            record = self._tasks.get(task_id)
            if record:
                record.status = TaskStatus.FAILED
                record.result = {"error": reason or "unknown"}
                record.updated_at = time.time()
            return record

    def statistics(self) -> dict:
        with self._lock:
            statuses: dict[str, int] = {}
            for r in self._tasks.values():
                s = r.status.value
                statuses[s] = statuses.get(s, 0) + 1
            return {"task_count": len(self._tasks), "by_status": statuses}


def discover_and_route(
    task_intent: str,
    fallback_policy: Optional[dict] = None,
) -> Optional[AgentCard]:
    """一站式 A2A 发现+路由，含 fallback 策略。

    fallback_policy 可含 {"allow_inactive": bool} 在无 active agent 时放宽。
    """
    registry = CapabilityRegistry()
    router = SemanticRouter(registry)
    card = router.route(task_intent)

    if card is None and fallback_policy and fallback_policy.get("allow_inactive"):
        with registry._lock:
            for c in registry._agents.values():
                return c
    return card
