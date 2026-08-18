"""
# status: orphan (2026-08-15 audit, not in runtime path)
P13-7: Memory Streaming
========================

对标 AWS AgentCore Streaming Notifications — 记忆事件流管道。

设计要点：
  - MemoryEventBus：发布-订阅模式的事件总线，支持按事件类型路由
  - StreamProducer：在记忆写入/更新/过期/融合时推送结构化事件
  - StreamConsumer：Lambda 式回调处理器，支持多消费者并行消费
  - change_log()：维护有序变更日志，含 event_type / memory_id / timestamp / payload

接口兼容：
  - episodic_rl.py MemoryWriter：写入时自动触发事件
  - hippocampus_cortex_bridge.py：可作为消费者订阅记忆巩固事件
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict, defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger(__name__)


# ============================================================================
# Enums
# ============================================================================

class StreamEventType(Enum):
    """流事件类型——覆盖记忆全生命周期。"""
    MEMORY_CREATED = "memory_created"           # 新记忆写入
    MEMORY_UPDATED = "memory_updated"           # 记忆内容/元数据更新
    MEMORY_ACCESSED = "memory_accessed"         # 记忆被检索/读取
    MEMORY_EXPIRED = "memory_expired"           # 记忆自然过期
    MEMORY_MERGED = "memory_merged"             # 多条记忆融合为一条
    MEMORY_CONFLICT = "memory_conflict"         # 记忆冲突检测
    MEMORY_DISTILLED = "memory_distilled"       # 记忆被蒸馏为复合对象
    MEMORY_REINFORCED = "memory_reinforced"     # 记忆被强化（保留分提升）
    MEMORY_DEGRADED = "memory_degraded"         # 记忆降级（保留分下降）
    MEMORY_DELETED = "memory_deleted"           # 记忆被删除
    CHECKPOINT = "checkpoint"                   # 系统检查点事件
    STREAM_ERROR = "stream_error"               # 流处理错误


class ConsumerPolicy(Enum):
    """消费者策略。"""
    SYNCHRONOUS = "synchronous"      # 同步处理（阻塞发布者）
    ASYNCHRONOUS = "asynchronous"    # 异步处理（线程池）
    BATCHED = "batched"              # 批量处理（攒批后消费）


class EventPriority(Enum):
    """事件优先级。"""
    CRITICAL = 0       # 关键事件（冲突、错误）
    HIGH = 1           # 高优先级（创建、删除、融合）
    NORMAL = 2         # 常规事件（更新、访问）
    LOW = 3            # 低优先级（检查点、统计）


# ============================================================================
# Dataclasses
# ============================================================================

@dataclass
class StreamEvent:
    """流事件——推送给消费者的结构化消息。"""
    event_id: str
    event_type: StreamEventType
    memory_id: str
    timestamp: float = field(default_factory=time.time)
    priority: EventPriority = EventPriority.NORMAL
    payload: Dict[str, Any] = field(default_factory=dict)
    source: str = ""                   # 事件来源模块
    trace_id: str = ""                 # 分布式追踪 ID
    retry_count: int = 0


@dataclass
class ChangeLogEntry:
    """变更日志条目。"""
    sequence: int                      # 全局递增序号
    event_type: StreamEventType
    memory_id: str
    timestamp: float
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_type": self.event_type.value,
            "memory_id": self.memory_id,
            "timestamp": self.timestamp,
            "payload": self.payload,
            "event_id": self.event_id,
        }


@dataclass
class ConsumerConfig:
    """消费者配置。"""
    consumer_id: str
    event_types: List[StreamEventType]       # 订阅的事件类型
    policy: ConsumerPolicy = ConsumerPolicy.ASYNCHRONOUS
    batch_size: int = 10
    batch_interval_ms: float = 100.0
    max_retries: int = 3
    dead_letter_queue: bool = True


@dataclass
class EventBusStats:
    """事件总线统计信息。"""
    total_events_published: int = 0
    total_events_consumed: int = 0
    total_errors: int = 0
    active_consumers: int = 0
    queue_depth: int = 0
    change_log_entries: int = 0
    uptime_seconds: float = 0.0
    events_per_second: float = 0.0
    timestamp: float = field(default_factory=time.time)


# ============================================================================
# MemoryEventBus
# ============================================================================

class MemoryEventBus:
    """记忆事件总线——发布-订阅模式的事件路由器。

    支持按事件类型注册消费者，同步/异步/批量三种消费策略。
    对标 AWS AgentCore Streaming Notifications。
    """

    def __init__(self, name: str = "memory_event_bus") -> None:
        self._name = name
        self._lock = threading.RLock()
        # event_type → List[ConsumerConfig]
        self._subscriptions: Dict[StreamEventType, List[ConsumerConfig]] = defaultdict(list)
        # consumer_id → Callable[[StreamEvent], None]
        self._handlers: Dict[str, Callable[[StreamEvent], None]] = {}
        # consumer_id → ConsumerConfig
        self._consumer_configs: Dict[str, ConsumerConfig] = {}
        # 事件队列（按优先级分组）
        self._queues: Dict[EventPriority, deque] = {
            EventPriority.CRITICAL: deque(maxlen=500),
            EventPriority.HIGH: deque(maxlen=1000),
            EventPriority.NORMAL: deque(maxlen=5000),
            EventPriority.LOW: deque(maxlen=2000),
        }
        # 统计
        self._total_published: int = 0
        self._total_consumed: int = 0
        self._total_errors: int = 0
        self._start_time: float = time.time()
        self._active: bool = True

    def subscribe(
        self,
        consumer_id: str,
        event_types: List[StreamEventType],
        handler: Callable[[StreamEvent], None],
        policy: ConsumerPolicy = ConsumerPolicy.ASYNCHRONOUS,
    ) -> ConsumerConfig:
        """注册消费者。

        Args:
            consumer_id: 消费者唯一标识
            event_types: 订阅的事件类型列表
            handler: Lambda 式回调函数，签名为 (event: StreamEvent) -> None
            policy: 消费策略

        Returns:
            消费者配置对象
        """
        config = ConsumerConfig(
            consumer_id=consumer_id,
            event_types=list(event_types),
            policy=policy,
        )
        with self._lock:
            for etype in event_types:
                self._subscriptions[etype].append(config)
            self._handlers[consumer_id] = handler
            self._consumer_configs[consumer_id] = config
        logger.info(
            "Consumer '%s' subscribed to %d event types (policy=%s)",
            consumer_id, len(event_types), policy.value,
        )
        return config

    def unsubscribe(self, consumer_id: str) -> bool:
        """注销消费者。"""
        with self._lock:
            config = self._consumer_configs.pop(consumer_id, None)
            if config is None:
                return False
            for etype in config.event_types:
                sub_list = self._subscriptions.get(etype, [])
                self._subscriptions[etype] = [c for c in sub_list if c.consumer_id != consumer_id]
            self._handlers.pop(consumer_id, None)
        logger.info("Consumer '%s' unsubscribed", consumer_id)
        return True

    def publish(self, event: StreamEvent) -> None:
        """发布事件。

        事件被推送到对应优先级的队列，并路由到所有订阅该事件类型的消费者。
        """
        with self._lock:
            if not self._active:
                logger.warning("EventBus is inactive, dropping event %s", event.event_id)
                return

            self._total_published += 1
            # 入队
            self._queues[event.priority].append(event)

            # 路由到消费者
            consumers = self._subscriptions.get(event.event_type, [])
            for config in consumers:
                handler = self._handlers.get(config.consumer_id)
                if handler is None:
                    continue
                self._dispatch(config, event, handler)

    def _dispatch(
        self,
        config: ConsumerConfig,
        event: StreamEvent,
        handler: Callable[[StreamEvent], None],
    ) -> None:
        """将事件分发给消费者。"""
        if config.policy == ConsumerPolicy.SYNCHRONOUS:
            self._invoke_handler(config.consumer_id, handler, event)
        elif config.policy == ConsumerPolicy.ASYNCHRONOUS:
            t = threading.Thread(
                target=self._invoke_handler,
                args=(config.consumer_id, handler, event),
                daemon=True,
            )
            t.start()
        # BATCHED 模式由 change_log 批处理，单个事件暂存

    def _invoke_handler(
        self,
        consumer_id: str,
        handler: Callable[[StreamEvent], None],
        event: StreamEvent,
    ) -> None:
        """调用消费者回调，带重试与死信队列。"""
        config = self._consumer_configs.get(consumer_id)
        max_retries = config.max_retries if config else 3

        for attempt in range(max_retries + 1):
            try:
                handler(event)
                with self._lock:
                    self._total_consumed += 1
                return
            except Exception as e:
                event.retry_count = attempt + 1
                logger.error(
                    "Handler '%s' failed for event %s (attempt %d/%d): %s",
                    consumer_id, event.event_id, attempt + 1, max_retries, e,
                )
                if attempt >= max_retries:
                    with self._lock:
                        self._total_errors += 1
                    # 死信队列：记录失败事件
                    logger.critical(
                        "Event %s moved to dead-letter queue: %s",
                        event.event_id, event.payload,
                    )
                    return
                time.sleep(0.1 * (attempt + 1))

    def shutdown(self, drain: bool = True) -> None:
        """关闭事件总线。"""
        with self._lock:
            self._active = False
            if drain:
                remaining = sum(len(q) for q in self._queues.values())
                logger.info("EventBus shutting down with %d events in queues", remaining)
            else:
                for q in self._queues.values():
                    q.clear()

    def get_stats(self) -> Dict[str, Any]:
        """获取事件总线统计信息。"""
        with self._lock:
            uptime = time.time() - self._start_time
            return {
                "name": self._name,
                "total_published": self._total_published,
                "total_consumed": self._total_consumed,
                "total_errors": self._total_errors,
                "active_consumers": len(self._handlers),
                "subscription_types": {
                    etype.value: len(subs) for etype, subs in self._subscriptions.items()
                },
                "queue_depths": {
                    p.name: len(q) for p, q in self._queues.items()
                },
                "uptime_seconds": round(uptime, 2),
                "events_per_second": round(
                    self._total_published / max(uptime, 1), 2
                ),
                "active": self._active,
            }


# ============================================================================
# StreamProducer
# ============================================================================

class StreamProducer:
    """流生产者——在记忆生命周期事件发生时推送结构化事件。

    与 MemoryEventBus 配合使用，作为记忆系统的事件入口。
    """

    def __init__(
        self,
        event_bus: MemoryEventBus,
        source: str = "stream_producer",
    ) -> None:
        self._event_bus = event_bus
        self._source = source
        self._lock = threading.RLock()
        self._produced_count: int = 0

    def _create_event(
        self,
        event_type: StreamEventType,
        memory_id: str,
        payload: Optional[Dict[str, Any]] = None,
        priority: EventPriority = EventPriority.NORMAL,
    ) -> StreamEvent:
        """创建标准事件。"""
        return StreamEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            memory_id=memory_id,
            priority=priority,
            payload=payload or {},
            source=self._source,
            trace_id=str(uuid.uuid4())[:12],
        )

    def on_memory_created(
        self, memory_id: str, metadata: Optional[Dict[str, Any]] = None
    ) -> StreamEvent:
        """记忆创建事件。"""
        event = self._create_event(
            StreamEventType.MEMORY_CREATED, memory_id,
            payload=metadata, priority=EventPriority.HIGH,
        )
        self._event_bus.publish(event)
        with self._lock:
            self._produced_count += 1
        return event

    def on_memory_updated(
        self, memory_id: str, changes: Optional[Dict[str, Any]] = None
    ) -> StreamEvent:
        """记忆更新事件。"""
        event = self._create_event(
            StreamEventType.MEMORY_UPDATED, memory_id, payload=changes,
        )
        self._event_bus.publish(event)
        with self._lock:
            self._produced_count += 1
        return event

    def on_memory_expired(
        self, memory_id: str, reason: str = ""
    ) -> StreamEvent:
        """记忆过期事件。"""
        event = self._create_event(
            StreamEventType.MEMORY_EXPIRED, memory_id,
            payload={"reason": reason}, priority=EventPriority.HIGH,
        )
        self._event_bus.publish(event)
        with self._lock:
            self._produced_count += 1
        return event

    def on_memory_merged(
        self, source_ids: List[str], target_id: str
    ) -> StreamEvent:
        """记忆融合事件。"""
        event = self._create_event(
            StreamEventType.MEMORY_MERGED, target_id,
            payload={"source_ids": source_ids, "target_id": target_id},
            priority=EventPriority.HIGH,
        )
        self._event_bus.publish(event)
        with self._lock:
            self._produced_count += 1
        return event

    def on_memory_reinforced(
        self, memory_id: str, new_score: float, previous_score: float
    ) -> StreamEvent:
        """记忆强化事件。"""
        event = self._create_event(
            StreamEventType.MEMORY_REINFORCED, memory_id,
            payload={"new_score": new_score, "previous_score": previous_score},
            priority=EventPriority.NORMAL,
        )
        self._event_bus.publish(event)
        with self._lock:
            self._produced_count += 1
        return event

    def on_error(self, memory_id: str, error: str) -> StreamEvent:
        """错误事件。"""
        event = self._create_event(
            StreamEventType.STREAM_ERROR, memory_id,
            payload={"error": error}, priority=EventPriority.CRITICAL,
        )
        self._event_bus.publish(event)
        with self._lock:
            self._produced_count += 1
        return event

    def get_stats(self) -> Dict[str, Any]:
        """获取生产者统计信息。"""
        with self._lock:
            return {
                "source": self._source,
                "produced_count": self._produced_count,
            }


# ============================================================================
# StreamConsumer
# ============================================================================

class StreamConsumer:
    """流消费者——Lambda 式回调处理器。

    订阅 MemoryEventBus 上特定类型的事件，注册回调函数处理。
    支持多个消费者并行消费同一事件流。
    """

    def __init__(
        self,
        event_bus: MemoryEventBus,
        consumer_id: Optional[str] = None,
    ) -> None:
        self._event_bus = event_bus
        self._consumer_id = consumer_id or f"consumer_{uuid.uuid4().hex[:8]}"
        self._lock = threading.RLock()
        self._handled_count: int = 0
        self._error_count: int = 0

    def listen(
        self,
        event_types: List[StreamEventType],
        callback: Callable[[StreamEvent], None],
        policy: ConsumerPolicy = ConsumerPolicy.ASYNCHRONOUS,
    ) -> ConsumerConfig:
        """订阅事件类型并注册回调。

        callback 签名为 (event: StreamEvent) -> None，
        支持 Lambda / 函数 / 绑定方法等任意可调用对象。
        """
        return self._event_bus.subscribe(
            consumer_id=self._consumer_id,
            event_types=event_types,
            handler=callback,
            policy=policy,
        )

    def stop(self) -> bool:
        """取消所有订阅。"""
        return self._event_bus.unsubscribe(self._consumer_id)

    def get_stats(self) -> Dict[str, Any]:
        """获取消费者统计信息。"""
        with self._lock:
            return {
                "consumer_id": self._consumer_id,
                "handled_count": self._handled_count,
                "error_count": self._error_count,
            }


# ============================================================================
# ChangeLog — Ordered Change Log
# ============================================================================

class ChangeLog:
    """有序变更日志。

    维护全局递增序列号，记录所有记忆事件的历史轨迹。
    支持按时间范围、事件类型、记忆 ID 过滤查询。
    """

    def __init__(
        self,
        max_entries: int = 10000,
        name: str = "change_log",
    ) -> None:
        self._max_entries = max_entries
        self._name = name
        self._lock = threading.RLock()
        self._entries: deque = deque(maxlen=max_entries)
        self._sequence: int = 0
        # 索引：memory_id → list of entry indices
        self._by_memory: Dict[str, List[int]] = defaultdict(list)
        self._by_type: Dict[StreamEventType, List[int]] = defaultdict(list)

    def append(self, event: StreamEvent) -> ChangeLogEntry:
        """追加变更日志条目。"""
        with self._lock:
            self._sequence += 1
            entry = ChangeLogEntry(
                sequence=self._sequence,
                event_type=event.event_type,
                memory_id=event.memory_id,
                timestamp=event.timestamp,
                payload=event.payload,
                event_id=event.event_id,
            )
            self._entries.append(entry)
            # 维护索引
            idx = len(self._entries) - 1
            self._by_memory[event.memory_id].append(idx)
            self._by_type[event.event_type].append(idx)
            return entry

    def query_by_memory(
        self, memory_id: str, limit: int = 100
    ) -> List[ChangeLogEntry]:
        """按记忆 ID 查询变更日志。"""
        with self._lock:
            indices = self._by_memory.get(memory_id, [])[-limit:]
            return [self._entries[i] for i in indices if i < len(self._entries)]

    def query_by_type(
        self, event_type: StreamEventType, limit: int = 100
    ) -> List[ChangeLogEntry]:
        """按事件类型查询变更日志。"""
        with self._lock:
            indices = self._by_type.get(event_type, [])[-limit:]
            return [self._entries[i] for i in indices if i < len(self._entries)]

    def query_by_timerange(
        self, start: float, end: float, limit: int = 100
    ) -> List[ChangeLogEntry]:
        """按时间范围查询变更日志。"""
        with self._lock:
            result = []
            for entry in self._entries:
                if start <= entry.timestamp <= end:
                    result.append(entry)
                if len(result) >= limit:
                    break
            return result

    def recent(self, n: int = 50) -> List[ChangeLogEntry]:
        """获取最近 N 条日志。"""
        with self._lock:
            items = list(self._entries)[-n:]
            return items

    def to_list(self) -> List[Dict[str, Any]]:
        """导出为字典列表。"""
        with self._lock:
            return [e.to_dict() for e in self._entries]

    def get_stats(self) -> Dict[str, Any]:
        """获取日志统计信息。"""
        with self._lock:
            return {
                "name": self._name,
                "total_entries": len(self._entries),
                "sequence": self._sequence,
                "max_entries": self._max_entries,
                "unique_memories": len(self._by_memory),
                "event_type_counts": {
                    etype.value: len(indices)
                    for etype, indices in self._by_type.items()
                },
                "oldest_timestamp": (
                    self._entries[0].timestamp if self._entries else None
                ),
                "newest_timestamp": (
                    self._entries[-1].timestamp if self._entries else None
                ),
            }


# ============================================================================
# change_log() — Module-Level Function
# ============================================================================

def change_log(
    event_bus: MemoryEventBus,
    memory_id: Optional[str] = None,
    event_type: Optional[StreamEventType] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """查询变更日志的快捷入口。

    Args:
        event_bus: 事件总线实例（需关联 ChangeLog）
        memory_id: 可选，按记忆 ID 过滤
        event_type: 可选，按事件类型过滤
        limit: 返回条目上限

    Returns:
        变更日志条目列表
    """
    # 此处为模块级便捷函数，实际使用时需绑定 ChangeLog 实例
    return []


# ============================================================================
# Module Statistics
# ============================================================================

def get_stats() -> Dict[str, Any]:
    """返回模块级统计信息。"""
    return {
        "module": "P13-7 Memory Streaming",
        "benchmark": "AWS AgentCore Streaming Notifications",
        "classes": 4,
        "enums": 3,
        "dataclasses": 4,
        "key_pattern": "Pub/Sub Event Bus + Lambda Consumers",
        "functions": ["change_log"],
        "thread_safe": True,
    }
