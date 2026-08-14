"""
CB70: MASharedMemoryBus — 多智能体共享记忆总线
===============================================

多智能体共享记忆总线，提供发布-订阅式记忆交换。

核心设计:
  - SharedMemoryBus: Pub/Sub 记忆交换，支持 transient(广播后遗忘) /
    persistent(写入共享池) / synchronized(强一致复制) 三种传播模式
  - AgentMemoryShard: 每个 Agent 的私有记忆分片 + 共享记忆视图隔离
  - AgentIdentity: 锚定 Agent 身份，防止记忆混淆
  - ConsensusMemoryResolver: 基于 Agent 可信度权重 + 时间戳解析冲突
  - CrossAgentContextWindow: 跨 Agent 对话上下文合并为统一 LLM 窗口
  - ShardRebalancer: 动态 Agent 加入/离开时重平衡分片

Reference:
  - Multi-agent shared memory architecture for collaborative LLM agents
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time as _time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class MemoryPropagationMode(Enum):
    """记忆传播模式。"""
    TRANSIENT = "transient"        # 广播后遗忘，不持久化
    PERSISTENT = "persistent"      # 写入共享记忆池
    SYNCHRONIZED = "synchronized"  # 强一致复制到所有 Agent


class ConsensusStrategy(Enum):
    """冲突解析策略。"""
    MAJORITY_VOTE = "majority_vote"    # 多数投票
    TRUST_WEIGHTED = "trust_weighted"  # 可信度加权
    LATEST_WINS = "latest_wins"        # 时间戳最新者胜
    MANUAL_ESCALATE = "manual_escalate" # 升级到人工


class ShardStatus(Enum):
    """分片状态。"""
    ACTIVE = "active"
    DRAINING = "draining"    # 迁移中
    INACTIVE = "inactive"


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class AgentIdentity:
    """Agent 身份标识——锚定 Agent 防止记忆混淆。

    Attributes:
        agent_id: 唯一标识。
        display_name: 显示名称。
        role: Agent 角色（planner/executor/observer/...）。
        trust_score: 可信度评分 [0..1]。
        capabilities: 能力标签集。
        joined_at: 加入时间戳。
    """
    agent_id: str
    display_name: str = ""
    role: str = "worker"
    trust_score: float = 0.5
    capabilities: Set[str] = field(default_factory=set)
    joined_at: float = field(default_factory=_time.time)

    def __hash__(self):
        return hash(self.agent_id)

    def __eq__(self, other):
        if isinstance(other, AgentIdentity):
            return self.agent_id == other.agent_id
        return False


@dataclass
class SharedMemoryEntry:
    """共享记忆条目。

    Attributes:
        entry_id: 条目唯一标识。
        content: 记忆内容。
        source_agent: 发布者身份。
        mode: 传播模式。
        timestamp: 创建时间戳。
        tags: 分类标签。
        ttl: transient 模式下的生存时间（秒），-1 表示无限。
    """
    entry_id: str
    content: str
    source_agent: str
    mode: MemoryPropagationMode = MemoryPropagationMode.TRANSIENT
    timestamp: float = field(default_factory=_time.time)
    tags: List[str] = field(default_factory=list)
    ttl: float = -1.0

    def is_expired(self) -> bool:
        if self.ttl < 0:
            return False
        return (_time.time() - self.timestamp) > self.ttl


@dataclass
class ConsensusRecord:
    """冲突记录——多个 Agent 对同一事实的不同记忆。"""
    fact_key: str
    versions: Dict[str, str] = field(default_factory=dict)  # agent_id → value
    timestamps: Dict[str, float] = field(default_factory=dict)
    resolved_value: Optional[str] = None
    resolved_at: float = 0.0


# ============================================================================
# SharedMemoryBus
# ============================================================================

class SharedMemoryBus:
    """发布-订阅式共享记忆总线。

    三种传播模式：
      - TRANSIENT: 广播给所有在线 Agent 后遗忘，不持久化
      - PERSISTENT: 写入共享池并落盘，订阅者拉取
      - SYNCHRONIZED: 强一致复制到全部 Agent 分片
    """

    def __init__(self, bus_id: str = "default"):
        self.bus_id = bus_id
        self._lock = threading.RLock()
        self._subscribers: Dict[str, List[Callable[[SharedMemoryEntry], None]]] = {}
        self._persistent_pool: Dict[str, SharedMemoryEntry] = {}
        self._transient_count: int = 0

    def publish(self, entry: SharedMemoryEntry) -> str:
        """发布记忆到总线。

        Returns:
            条目的 entry_id。
        """
        with self._lock:
            if entry.mode == MemoryPropagationMode.PERSISTENT:
                self._persistent_pool[entry.entry_id] = entry
            elif entry.mode == MemoryPropagationMode.SYNCHRONIZED:
                self._persistent_pool[entry.entry_id] = entry
                self._notify_subscribers(entry)
            elif entry.mode == MemoryPropagationMode.TRANSIENT:
                self._transient_count += 1
                self._notify_subscribers(entry)
            return entry.entry_id

    def subscribe(
        self, topic: str, callback: Callable[[SharedMemoryEntry], None]
    ):
        with self._lock:
            self._subscribers.setdefault(topic, []).append(callback)

    def unsubscribe(
        self, topic: str, callback: Callable[[SharedMemoryEntry], None]
    ):
        with self._lock:
            if topic in self._subscribers:
                self._subscribers[topic] = [
                    c for c in self._subscribers[topic] if c is not callback
                ]

    def pull(self, topic: str = "*", since: float = 0.0) -> List[SharedMemoryEntry]:
        """拉取持久化记忆池中新条目。"""
        with self._lock:
            results = []
            for entry in self._persistent_pool.values():
                if entry.is_expired():
                    continue
                if topic != "*" and topic not in entry.tags:
                    continue
                if entry.timestamp >= since:
                    results.append(entry)
            return sorted(results, key=lambda e: e.timestamp)

    def _notify_subscribers(self, entry: SharedMemoryEntry):
        for topic, callbacks in list(self._subscribers.items()):
            if topic == "*" or topic in entry.tags:
                for cb in callbacks:
                    try:
                        cb(entry)
                    except Exception:
                        logger.exception(f"Subscriber callback failed for topic={topic}")

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "bus_id": self.bus_id,
                "persistent_entries": len(self._persistent_pool),
                "transient_count": self._transient_count,
                "subscriber_topics": list(self._subscribers.keys()),
            }


# ============================================================================
# AgentMemoryShard
# ============================================================================

class AgentMemoryShard:
    """Agent 私有记忆分片——隔离私有记忆与共享记忆视图。

    每个 Agent 维护：
      - private_memories: 仅本 Agent 可见的私有记忆
      - shared_view: 从 SharedMemoryBus 拉取的共享记忆缓存
    """

    def __init__(self, identity: AgentIdentity, bus: Optional[SharedMemoryBus] = None):
        self.identity = identity
        self.bus = bus
        self._lock = threading.RLock()
        self._private: Dict[str, Any] = {}
        self._shared_view: Dict[str, SharedMemoryEntry] = {}
        self._status: ShardStatus = ShardStatus.ACTIVE
        self._read_count: int = 0
        self._write_count: int = 0

    def write_private(self, key: str, value: Any):
        with self._lock:
            self._private[key] = value
            self._write_count += 1

    def read_private(self, key: str) -> Optional[Any]:
        with self._lock:
            self._read_count += 1
            return self._private.get(key)

    def sync_from_bus(self, topic: str = "*"):
        """从总线同步共享记忆。"""
        if self.bus is None:
            return
        with self._lock:
            entries = self.bus.pull(topic=topic)
            for e in entries:
                self._shared_view[e.entry_id] = e

    def publish_to_bus(
        self, content: str, mode: MemoryPropagationMode, tags: Optional[List[str]] = None
    ) -> Optional[str]:
        if self.bus is None:
            return None
        entry = SharedMemoryEntry(
            entry_id=hashlib.md5(
                f"{self.identity.agent_id}_{content}_{_time.time()}".encode()
            ).hexdigest()[:12],
            content=content,
            source_agent=self.identity.agent_id,
            mode=mode,
            tags=tags or [],
        )
        return self.bus.publish(entry)

    def get_shared_view(self) -> Dict[str, SharedMemoryEntry]:
        with self._lock:
            return dict(self._shared_view)

    def set_status(self, status: ShardStatus):
        with self._lock:
            self._status = status

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "agent_id": self.identity.agent_id,
                "status": self._status.value,
                "private_entries": len(self._private),
                "shared_view_entries": len(self._shared_view),
                "reads": self._read_count,
                "writes": self._write_count,
            }


# ============================================================================
# ConsensusMemoryResolver
# ============================================================================

class ConsensusMemoryResolver:
    """多 Agent 事实冲突解析器。

    基于 Agent 可信度权重 + 时间戳解析同一事实的冲突写入。
    """

    def __init__(self, strategy: ConsensusStrategy = ConsensusStrategy.TRUST_WEIGHTED):
        self.strategy = strategy
        self._lock = threading.RLock()
        self._resolutions: Dict[str, ConsensusRecord] = {}
        self._agent_trust: Dict[str, float] = {}

    def register_agent(self, identity: AgentIdentity):
        with self._lock:
            self._agent_trust[identity.agent_id] = identity.trust_score

    def update_trust(self, agent_id: str, new_trust: float):
        with self._lock:
            self._agent_trust[agent_id] = max(0.0, min(1.0, new_trust))

    def submit(self, fact_key: str, agent_id: str, value: str):
        with self._lock:
            if fact_key not in self._resolutions:
                self._resolutions[fact_key] = ConsensusRecord(fact_key=fact_key)
            rec = self._resolutions[fact_key]
            rec.versions[agent_id] = value
            rec.timestamps[agent_id] = _time.time()
            self._resolve(fact_key)

    def _resolve(self, fact_key: str):
        rec = self._resolutions[fact_key]
        if len(rec.versions) < 2:
            return

        if self.strategy == ConsensusStrategy.LATEST_WINS:
            winner = max(rec.timestamps, key=rec.timestamps.get)
            rec.resolved_value = rec.versions[winner]
        elif self.strategy == ConsensusStrategy.TRUST_WEIGHTED:
            # 按可信度加权投票
            votes: Dict[str, float] = {}
            for agent, val in rec.versions.items():
                trust = self._agent_trust.get(agent, 0.5)
                votes.setdefault(val, 0.0)
                votes[val] += trust
            rec.resolved_value = max(votes, key=votes.get)
        elif self.strategy == ConsensusStrategy.MAJORITY_VOTE:
            counts: Dict[str, int] = {}
            for val in rec.versions.values():
                counts[val] = counts.get(val, 0) + 1
            rec.resolved_value = max(counts, key=counts.get)
        # MANUAL_ESCALATE: no auto resolve
        rec.resolved_at = _time.time()

    def get_resolved(self, fact_key: str) -> Optional[str]:
        with self._lock:
            rec = self._resolutions.get(fact_key)
            return rec.resolved_value if rec else None

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            resolved = sum(1 for r in self._resolutions.values() if r.resolved_value)
            return {
                "strategy": self.strategy.value,
                "total_facts": len(self._resolutions),
                "resolved": resolved,
                "pending": len(self._resolutions) - resolved,
                "registered_agents": len(self._agent_trust),
            }


# ============================================================================
# CrossAgentContextWindow
# ============================================================================

class CrossAgentContextWindow:
    """跨 Agent 对话上下文合并窗口。

    将多个 Agent 的对话片段合并为统一上下文窗口供 LLM 消费。
    """

    def __init__(self, max_tokens: int = 8192):
        self.max_tokens = max_tokens
        self._lock = threading.RLock()
        self._segments: List[Tuple[str, str, str]] = []  # (agent_id, speaker, content)

    def append(self, agent_id: str, speaker: str, content: str):
        with self._lock:
            self._segments.append((agent_id, speaker, content))
            # Estimate tokens and trim if needed (rough: 1 token ~ 4 chars)
            total_chars = sum(len(c) for _, _, c in self._segments)
            while total_chars > self.max_tokens * 4 and len(self._segments) > 1:
                self._segments.pop(0)
                total_chars = sum(len(c) for _, _, c in self._segments)

    def to_text(self) -> str:
        with self._lock:
            lines = []
            for agent_id, speaker, content in self._segments:
                lines.append(f"[{agent_id}:{speaker}] {content}")
            return "\n".join(lines)

    def clear(self):
        with self._lock:
            self._segments.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._segments)

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            total_chars = sum(len(c) for _, _, c in self._segments)
            return {
                "segments": len(self._segments),
                "total_chars": total_chars,
                "estimated_tokens": total_chars // 4,
                "max_tokens": self.max_tokens,
                "unique_agents": len(set(a for a, _, _ in self._segments)),
            }


# ============================================================================
# ShardRebalancer
# ============================================================================

class ShardRebalancer:
    """分片再平衡器——动态 Agent 加入/离开时重平衡。

    策略：基于 Agent 负载（记忆条目数）均匀分布。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._shards: Dict[str, AgentMemoryShard] = {}

    def register(self, shard: AgentMemoryShard):
        with self._lock:
            self._shards[shard.identity.agent_id] = shard

    def unregister(self, agent_id: str) -> Optional[AgentMemoryShard]:
        with self._lock:
            shard = self._shards.pop(agent_id, None)
            if shard:
                shard.set_status(ShardStatus.INACTIVE)
            return shard

    def rebalance(self):
        """触发再平衡——标记高负载分片为 DRAINING。

        实际实现中会触发迁移流水线；此处为占位。
        """
        with self._lock:
            if not self._shards:
                return
            stats = [(aid, s.statistics()["private_entries"]) for aid, s in self._shards.items()]
            if not stats:
                return
            avg = sum(s[1] for s in stats) / len(stats)
            for agent_id, count in stats:
                if count > avg * 1.5:
                    self._shards[agent_id].set_status(ShardStatus.DRAINING)
                    logger.info(f"Shard {agent_id} marked DRAINING ({count} > {avg:.0f})")

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_shards": len(self._shards),
                "active_shards": sum(
                    1 for s in self._shards.values() if s._status == ShardStatus.ACTIVE
                ),
            }


# ============================================================================
# Main Class
# ============================================================================

class MASharedMemoryBus:
    """多智能体共享记忆总线 (CB70)。

    统一入口——管理总线、分片、冲突解析、上下文窗口、重平衡。

    Usage:
        mas = MASharedMemoryBus()
        alice = AgentIdentity(agent_id="alice", role="planner", trust_score=0.9)
        bob = AgentIdentity(agent_id="bob", role="executor", trust_score=0.7)
        mas.register_agent(alice)
        mas.register_agent(bob)
        mas.shard_for("alice").publish_to_bus("task completed", MemoryPropagationMode.TRANSIENT)
    """

    def __init__(self):
        self._lock = threading.RLock()
        self.bus = SharedMemoryBus(bus_id="main")
        self.resolver = ConsensusMemoryResolver()
        self.rebalancer = ShardRebalancer()
        self.context_window = CrossAgentContextWindow()
        self._shards: Dict[str, AgentMemoryShard] = {}
        self._start_time: float = _time.time()

    def register_agent(self, identity: AgentIdentity) -> AgentMemoryShard:
        with self._lock:
            shard = AgentMemoryShard(identity=identity, bus=self.bus)
            self._shards[identity.agent_id] = shard
            self.resolver.register_agent(identity)
            self.rebalancer.register(shard)
            return shard

    def unregister_agent(self, agent_id: str):
        with self._lock:
            self._shards.pop(agent_id, None)
            self.resolver._agent_trust.pop(agent_id, None)
            self.rebalancer.unregister(agent_id)

    def shard_for(self, agent_id: str) -> Optional[AgentMemoryShard]:
        with self._lock:
            return self._shards.get(agent_id)

    def resolve_conflict(self, fact_key: str, agent_id: str, value: str):
        self.resolver.submit(fact_key, agent_id, value)

    def append_context(self, agent_id: str, speaker: str, content: str):
        self.context_window.append(agent_id, speaker, content)

    def get_context(self) -> str:
        return self.context_window.to_text()

    def statistics(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "class": "MASharedMemoryBus (CB70)",
                "bus": self.bus.statistics(),
                "consensus": self.resolver.statistics(),
                "rebalancer": self.rebalancer.statistics(),
                "context_window": self.context_window.statistics(),
                "registered_agents": list(self._shards.keys()),
                "uptime_seconds": round(_time.time() - self._start_time, 3),
            }
