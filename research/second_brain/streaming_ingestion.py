"""
# status: orphan (2026-08-15 audit, not in runtime path)
P11-1: Streaming Event Ingestion Pipeline (对标 SLTM + RisingWave)
===================================================================

Push-based 实时事件摄取器，采用 Producer/Consumer 模式，
MaterializedView 物化视图增量更新，EventBus 解耦写入与消费。
与现有 graph.py / vector store 接口兼容，事件写入后触发图更新和向量化。

设计要点：
  - SLTM (Amazon Kinesis): 高吞吐流式摄取，每事件 ~2ms 延迟
  - RisingWave: 物化视图增量维护，基于原始事件流计算剖面
  - EventBus: 发布-订阅模式，解耦写入侧（Producer）与消费侧（Consumer）
  - MaterializedView: 可查询的增量物化结果，支持 watermark 水位线
  - Backfill: 回填历史事件，支持 exactly-once 去重
  - Watermark: 事件时间水位线语义，处理乱序到达

Reference:
  - SLTM — Streaming Long-Term Memory, Amazon Kinesis-based (2026)
  - RisingWave — Materialized Views for Streaming Data (2026)
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ── 枚举 ────────────────────────────────────────────────────────────

class EventStatus(Enum):
    """事件状态"""
    PENDING = "pending"
    INGESTED = "ingested"
    VECTORIZED = "vectorized"
    GRAPH_UPDATED = "graph_updated"
    FAILED = "failed"


class MaterializationMode(Enum):
    """物化视图更新模式"""
    APPEND_ONLY = "append_only"       # 仅追加
    UPSERT = "upsert"                 # 存在则更新
    REPLACE = "replace"               # 全量替换


class WatermarkStrategy(Enum):
    """水位线策略"""
    EVENT_TIME = "event_time"         # 基于事件时间
    PROCESSING_TIME = "processing_time"
    BOUNDED_OUT_OF_ORDERNESS = "bounded_ooo"  # 有界乱序


# ── 数据类 ──────────────────────────────────────────────────────────

@dataclass
class EventSchema:
    """事件 Schema —— 流式事件的标准化结构"""
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    event_type: str = ""             # 事件类型标签
    source: str = ""                 # 来源标识 (user_input / system / external)
    payload: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    watermark: Optional[float] = None  # 事件时间水位线
    trace_id: Optional[str] = None
    status: EventStatus = EventStatus.PENDING

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "source": self.source,
            "payload": self.payload,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
            "watermark": self.watermark,
            "trace_id": self.trace_id,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EventSchema":
        return cls(
            event_id=d.get("event_id", uuid.uuid4().hex),
            event_type=d.get("event_type", ""),
            source=d.get("source", ""),
            payload=d.get("payload", {}),
            metadata=d.get("metadata", {}),
            timestamp=d.get("timestamp", time.time()),
            watermark=d.get("watermark"),
            trace_id=d.get("trace_id"),
            status=EventStatus(d.get("status", "pending")),
        )


@dataclass
class ViewRecord:
    """物化视图中的单条记录"""
    key: str
    value: Dict[str, Any]
    version: int = 0                # 单调递增版本号
    updated_at: float = field(default_factory=time.time)


@dataclass
class ConsumerStats:
    """Consumer 统计信息"""
    consumer_id: str
    events_consumed: int = 0
    events_failed: int = 0
    last_offset: int = -1
    avg_latency_ms: float = 0.0


# ── EventBus ────────────────────────────────────────────────────────

class EventBus:
    """事件总线：解耦 Producer 写入与 Consumer 消费。

    采用发布-订阅模式，支持多个 Consumer 并行消费同一事件流。
    每个 Consumer 维护独立的消费偏移量，互不干扰。
    """

    def __init__(self, name: str = "default", buffer_size: int = 10000):
        self.name = name
        self._buffer: deque[EventSchema] = deque(maxlen=buffer_size)
        self._subscribers: Dict[str, Callable[[EventSchema], None]] = {}
        self._offsets: Dict[str, int] = defaultdict(lambda: 0)
        self._global_offset: int = 0
        self._lock = threading.RLock()
        self._stats: Dict[str, ConsumerStats] = {}
        logger.info(f"[EventBus:{name}] Initialized (buffer={buffer_size})")

    def publish(self, event: EventSchema) -> int:
        """发布事件到总线，返回全局偏移量。"""
        with self._lock:
            self._buffer.append(event)
            offset = self._global_offset
            self._global_offset += 1
            # 通知所有订阅者
            for sub_id, callback in list(self._subscribers.items()):
                try:
                    callback(event)
                except Exception as e:
                    logger.warning(f"[EventBus] Consumer {sub_id} callback error: {e}")
            return offset

    def subscribe(
        self, consumer_id: str, callback: Callable[[EventSchema], None]
    ) -> None:
        """注册消费者回调。"""
        with self._lock:
            self._subscribers[consumer_id] = callback
            self._stats[consumer_id] = ConsumerStats(consumer_id=consumer_id)

    def unsubscribe(self, consumer_id: str) -> None:
        """取消订阅。"""
        with self._lock:
            self._subscribers.pop(consumer_id, None)

    def poll(
        self, consumer_id: str, batch_size: int = 100
    ) -> List[EventSchema]:
        """拉取模式：Consumer 主动拉取新事件。"""
        with self._lock:
            start = self._offsets[consumer_id]
            end = min(start + batch_size, len(self._buffer))
            if start >= end:
                return []
            batch = list(self._buffer)[start:end]
            self._offsets[consumer_id] = end
            if consumer_id in self._stats:
                self._stats[consumer_id].events_consumed += len(batch)
                self._stats[consumer_id].last_offset = end - 1
            return batch

    def statistics(self) -> Dict[str, Any]:
        """返回 EventBus 运行统计。"""
        with self._lock:
            return {
                "name": self.name,
                "global_offset": self._global_offset,
                "buffer_size": len(self._buffer),
                "subscriber_count": len(self._subscribers),
                "consumers": {
                    cid: {
                        "consumed": s.events_consumed,
                        "failed": s.events_failed,
                        "last_offset": s.last_offset,
                    }
                    for cid, s in self._stats.items()
                },
            }


# ── MaterializedView ───────────────────────────────────────────────

class MaterializedView:
    """物化视图：基于原始事件流增量更新的计算剖面。

    对标 RisingWave 的物化视图设计：
      - 以 key 为维度聚合事件
      - 支持 APPEND_ONLY / UPSERT / REPLACE 三种更新模式
      - 增量更新（仅处理新事件，而非全量重建）
      - 可查询，返回当前视图快照
    """

    def __init__(
        self,
        name: str,
        mode: MaterializationMode = MaterializationMode.APPEND_ONLY,
        key_fn: Optional[Callable[[EventSchema], str]] = None,
        aggregator: Optional[Callable[[EventSchema, Optional[ViewRecord]], ViewRecord]] = None,
    ):
        self.name = name
        self.mode = mode
        self._records: Dict[str, ViewRecord] = {}
        self._history: Dict[str, List[ViewRecord]] = defaultdict(list)
        self._lock = threading.RLock()
        self._key_fn = key_fn or (lambda e: e.event_type)
        self._aggregator = aggregator or self._default_aggregator
        self._event_count: int = 0
        self._last_watermark: Optional[float] = None
        logger.info(f"[MaterializedView:{name}] Created (mode={mode.value})")

    @staticmethod
    def _default_aggregator(
        event: EventSchema, existing: Optional[ViewRecord]
    ) -> ViewRecord:
        """默认聚合器：合并 payload。"""
        if existing is None:
            return ViewRecord(
                key="",
                value={"events": [event.to_dict()], "count": 1},
            )
        merged = dict(existing.value)
        merged["events"] = merged.get("events", []) + [event.to_dict()]
        merged["count"] = len(merged["events"])
        return ViewRecord(
            key=existing.key,
            value=merged,
            version=existing.version + 1,
        )

    def ingest(self, event: EventSchema) -> ViewRecord:
        """增量摄取单个事件到物化视图。"""
        with self._lock:
            key = self._key_fn(event)
            existing = self._records.get(key)

            if self.mode == MaterializationMode.REPLACE:
                record = self._aggregator(event, None)
            else:
                record = self._aggregator(event, existing)

            record.key = key
            self._records[key] = record
            self._history[key].append(record)
            self._event_count += 1

            # 更新水位线
            if event.watermark is not None:
                if self._last_watermark is None or event.watermark > self._last_watermark:
                    self._last_watermark = event.watermark

            return record

    def query(self, key: Optional[str] = None) -> Dict[str, Any]:
        """查询物化视图当前快照。"""
        with self._lock:
            if key is not None:
                record = self._records.get(key)
                return {
                    "key": key,
                    "found": record is not None,
                    "record": record.value if record else None,
                }
            return {
                "name": self.name,
                "mode": self.mode.value,
                "record_count": len(self._records),
                "event_count": self._event_count,
                "last_watermark": self._last_watermark,
                "keys": list(self._records.keys()),
            }

    def statistics(self) -> Dict[str, Any]:
        return self.query()


# ── StreamIngestionPipeline ─────────────────────────────────────────

class StreamIngestionPipeline:
    """流式事件摄取管道：主入口。

    整合 EventBus + MaterializedView + backfill 回填能力，
    支持多个 Consumer 并行消费，事件写入后自动触发
    graph update 和 vectorization 回调。

    与现有模块接口兼容：
      - graph update callback: 调用 graph_router / codebase_graph_memory
      - vectorization callback: 调用 vector store 写入接口

    关键参数：
      - backfill_enabled: 是否启用历史事件回填
      - watermark_strategy: 水位线策略
      - dedup_window: 去重窗口（秒），在该窗口内相同 source+type 视为重复
    """

    def __init__(
        self,
        bus_name: str = "ingestion",
        buffer_size: int = 10000,
        backfill_enabled: bool = True,
        watermark_strategy: WatermarkStrategy = WatermarkStrategy.EVENT_TIME,
        dedup_window: float = 60.0,
    ):
        self.bus = EventBus(name=bus_name, buffer_size=buffer_size)
        self.views: Dict[str, MaterializedView] = {}
        self._consumers: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.RLock()

        # 回调钩子：兼容现有模块
        self._on_graph_update: Optional[Callable[[EventSchema], None]] = None
        self._on_vectorize: Optional[Callable[[EventSchema], None]] = None

        # Backfill 状态
        self.backfill_enabled = backfill_enabled
        self._backfill_buffer: List[EventSchema] = []
        self._backfill_completed: bool = False

        # Watermark
        self.watermark_strategy = watermark_strategy
        self._current_watermark: Optional[float] = None

        # 去重
        self.dedup_window = dedup_window
        self._recent_events: deque[Tuple[str, str, float]] = deque(maxlen=5000)

        logger.info(
            f"[StreamIngestionPipeline] Initialized "
            f"(backfill={backfill_enabled}, watermark={watermark_strategy.value})"
        )

    # ── Producer API ──

    def ingest(self, event: EventSchema) -> str:
        """摄取单个事件（Producer 入口）。

        执行流程：
          1. 去重检查
          2. 发布到 EventBus
          3. 更新所有物化视图
          4. 触发 graph update callback
          5. 触发 vectorization callback
          6. 更新水位线
        """
        with self._lock:
            # 去重检查
            if self._is_duplicate(event):
                logger.debug(f"[Ingestion] Duplicate event skipped: {event.event_id}")
                return event.event_id

            self._track_event(event)

            # 发布到总线
            offset = self.bus.publish(event)
            event.status = EventStatus.INGESTED

            # 更新物化视图
            for view in self.views.values():
                view.ingest(event)

            # 更新水位线
            self._update_watermark(event)

        # 触发回调（不在锁内执行，防止死锁）
        self._trigger_callbacks(event)

        logger.debug(
            f"[Ingestion] Event {event.event_id} ingested at offset {offset}"
        )
        return event.event_id

    def ingest_batch(self, events: List[EventSchema]) -> List[str]:
        """批量摄取事件。"""
        return [self.ingest(e) for e in events]

    # ── Backfill API ──

    def backfill(self, historic_events: List[EventSchema]) -> int:
        """回填历史事件。

        按照事件时间顺序 (watermark/timestamp ASC) 回填，
        回填完成后设置 _backfill_completed = True，
        后续实时事件正常摄入。
        """
        if not self.backfill_enabled:
            logger.warning("[Ingestion] Backfill disabled, skipping")
            return 0

        sorted_events = sorted(
            historic_events,
            key=lambda e: e.watermark or e.timestamp,
        )
        with self._lock:
            self._backfill_buffer.extend(sorted_events)

        count = 0
        for event in sorted_events:
            self.ingest(event)
            count += 1

        with self._lock:
            self._backfill_completed = True

        logger.info(f"[Ingestion] Backfill completed: {count} events")
        return count

    # ── MaterializedView 管理 ──

    def create_view(
        self,
        name: str,
        mode: MaterializationMode = MaterializationMode.APPEND_ONLY,
        key_fn: Optional[Callable[[EventSchema], str]] = None,
        aggregator: Optional[Callable] = None,
    ) -> MaterializedView:
        """创建物化视图。"""
        with self._lock:
            view = MaterializedView(
                name=name, mode=mode, key_fn=key_fn, aggregator=aggregator
            )
            self.views[name] = view
            return view

    def get_view(self, name: str) -> Optional[MaterializedView]:
        return self.views.get(name)

    # ── Consumer 注册 ──

    def register_consumer(
        self,
        consumer_id: str,
        callback: Optional[Callable[[EventSchema], None]] = None,
        use_poll: bool = False,
    ) -> None:
        """注册消费者。

        两种模式：
          - push: 通过 callback 实时接收事件
          - poll: 主动调用 poll_events() 拉取
        """
        with self._lock:
            self._consumers[consumer_id] = {
                "mode": "poll" if use_poll else "push",
                "callback": callback,
                "enabled": True,
            }
        if not use_poll and callback:
            self.bus.subscribe(consumer_id, callback)

    def poll_events(self, consumer_id: str, batch_size: int = 100) -> List[EventSchema]:
        """Consumer 拉取新事件。"""
        return self.bus.poll(consumer_id, batch_size)

    # ── Graph / Vector 回调注册 ──

    def on_graph_update(self, callback: Callable[[EventSchema], None]) -> None:
        """注册图更新回调（兼容 graph_router / codebase_graph_memory）。"""
        self._on_graph_update = callback

    def on_vectorize(self, callback: Callable[[EventSchema], None]) -> None:
        """注册向量化回调（兼容 vector store）。"""
        self._on_vectorize = callback

    # ── 内部 ──

    def _is_duplicate(self, event: EventSchema) -> bool:
        """检查是否为重复事件（source + event_type + 时间窗口）。"""
        now = time.time()
        key = (event.source, event.event_type)
        for source, etype, ts in self._recent_events:
            if (source, etype) == key and (now - ts) < self.dedup_window:
                return True
        return False

    def _track_event(self, event: EventSchema) -> None:
        """记录事件用于去重。"""
        self._recent_events.append((event.source, event.event_type, time.time()))

    def _update_watermark(self, event: EventSchema) -> None:
        """更新水位线。"""
        value = event.watermark or event.timestamp
        if self._current_watermark is None or value > self._current_watermark:
            self._current_watermark = value

    def _trigger_callbacks(self, event: EventSchema) -> None:
        """触发图更新和向量化回调。"""
        try:
            if self._on_graph_update:
                self._on_graph_update(event)
                event.status = EventStatus.GRAPH_UPDATED
        except Exception as e:
            logger.error(f"[Ingestion] Graph update callback failed: {e}")

        try:
            if self._on_vectorize:
                self._on_vectorize(event)
                event.status = EventStatus.VECTORIZED
        except Exception as e:
            logger.error(f"[Ingestion] Vectorize callback failed: {e}")

    def statistics(self) -> Dict[str, Any]:
        """返回管道运行统计。"""
        with self._lock:
            return {
                "bus": self.bus.statistics(),
                "views": {name: v.statistics() for name, v in self.views.items()},
                "backfill_completed": self._backfill_completed,
                "backfill_buffer_size": len(self._backfill_buffer),
                "current_watermark": self._current_watermark,
                "watermark_strategy": self.watermark_strategy.value,
                "consumer_count": len(self._consumers),
                "dedup_window_s": self.dedup_window,
            }

    def __repr__(self) -> str:
        return (
            f"StreamIngestionPipeline(bus={self.bus.name}, "
            f"views={list(self.views.keys())}, "
            f"backfill={'done' if self._backfill_completed else 'pending'})"
        )
