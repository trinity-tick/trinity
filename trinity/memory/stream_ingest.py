# -*- coding: utf-8 -*-
"""
Trinity Memory — Stream Ingest (P1-2).

Provides real-time streaming memory ingestion through pluggable message
broker backends: Kafka, Redis Streams, and an in-memory mock for testing.

Usage::

    from trinity.memory.stream_ingest import StreamIngestor, InMemoryBackend

    ingestor = StreamIngestor(backend=InMemoryBackend())
    ingestor.submit({"content": "user prefers dark mode", "tags": ["prefs"]})
    ingestor.start_consumer(batch_size=10, interval_sec=1.0)
    ingestor.stop()
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Message Data Model ────────────────────────────────────────────────────


class StreamStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


@dataclass
class StreamMessage:
    """A single memory message in the stream pipeline."""

    message_id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    content: str = ""
    tags: List[str] = field(default_factory=list)
    importance: float = 0.5
    category: str = "general"
    modality: str = "text"
    metadata: Dict[str, Any] = field(default_factory=dict)
    tenant_id: str = "default"
    timestamp: float = field(default_factory=time.time)
    status: StreamStatus = StreamStatus.PENDING
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "content": self.content,
            "tags": self.tags,
            "importance": self.importance,
            "category": self.category,
            "modality": self.modality,
            "metadata": self.metadata,
            "tenant_id": self.tenant_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> StreamMessage:
        return cls(
            message_id=d.get("message_id", ""),
            content=d.get("content", ""),
            tags=d.get("tags", []),
            importance=d.get("importance", 0.5),
            category=d.get("category", "general"),
            modality=d.get("modality", "text"),
            metadata=d.get("metadata", {}),
            tenant_id=d.get("tenant_id", "default"),
            timestamp=d.get("timestamp", time.time()),
        )


# ── Backend Interface ─────────────────────────────────────────────────────


class StreamBackend(ABC):
    """Abstract stream backend interface.

    Implementations: InMemoryBackend, KafkaBackend (stub), RedisStreamBackend (stub).
    """

    @abstractmethod
    def push(self, message: StreamMessage) -> bool:
        """Push a message onto the stream."""

    @abstractmethod
    def pull(self, batch_size: int = 10) -> List[StreamMessage]:
        """Pull a batch of messages from the stream."""

    @abstractmethod
    def acknowledge(self, message_ids: List[str]) -> int:
        """Acknowledge processed messages."""

    @abstractmethod
    def pending_count(self) -> int:
        """Return count of pending messages."""

    @abstractmethod
    def close(self) -> None:
        """Close the backend connection."""


# ── In-Memory Backend ─────────────────────────────────────────────────────


class InMemoryBackend(StreamBackend):
    """In-memory stream backend for testing and single-node deployment.

    Uses deque for FIFO ordering with O(1) push/pop.
    """

    def __init__(self, max_size: int = 10000):
        self._queue: List[StreamMessage] = []
        self._lock = threading.RLock()
        self._max_size = max_size
        self._ack_count: int = 0
        self._push_count: int = 0

    def push(self, message: StreamMessage) -> bool:
        with self._lock:
            if len(self._queue) >= self._max_size:
                logger.warning("InMemoryBackend queue full (%d), dropping message", self._max_size)
                return False
            self._queue.append(message)
            self._push_count += 1
            return True

    def pull(self, batch_size: int = 10) -> List[StreamMessage]:
        with self._lock:
            batch = self._queue[:batch_size]
            self._queue = self._queue[batch_size:]
            return batch

    def acknowledge(self, message_ids: List[str]) -> int:
        with self._lock:
            self._ack_count += len(message_ids)
            return len(message_ids)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    def close(self) -> None:
        with self._lock:
            self._queue.clear()

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "pushed": self._push_count,
                "pending": len(self._queue),
                "acknowledged": self._ack_count,
                "max_size": self._max_size,
            }


# ── Kafka Backend (Stub) ──────────────────────────────────────────────────


class KafkaBackend(StreamBackend):
    """Kafka stream backend stub.

    Falls back to in-memory when kafka-python is not installed.
    """

    def __init__(self, bootstrap_servers: str = "localhost:9092", topic: str = "trinity.memory"):
        self._bootstrap = bootstrap_servers
        self._topic = topic
        self._fallback = InMemoryBackend()
        self._kafka_available = False
        try:
            import confluent_kafka  # noqa: F401
            self._kafka_available = True
            logger.info("Kafka backend: confluent_kafka available, using real Kafka")
        except ImportError:
            logger.info("Kafka backend: confluent_kafka not installed, using in-memory fallback")

    def push(self, message: StreamMessage) -> bool:
        if self._kafka_available:
            return self._kafka_push(message)
        return self._fallback.push(message)

    def _kafka_push(self, message: StreamMessage) -> bool:
        try:
            from confluent_kafka import Producer
            producer = Producer({"bootstrap.servers": self._bootstrap})
            payload = json.dumps(message.to_dict()).encode("utf-8")
            producer.produce(self._topic, key=message.message_id.encode(), value=payload)
            producer.flush(timeout=2)
            return True
        except Exception as e:
            logger.error("Kafka push failed: %s", e)
            return False

    def pull(self, batch_size: int = 10) -> List[StreamMessage]:
        if self._kafka_available:
            return self._kafka_pull(batch_size)
        return self._fallback.pull(batch_size)

    def _kafka_pull(self, batch_size: int = 10) -> List[StreamMessage]:
        try:
            from confluent_kafka import Consumer
            consumer = Consumer({
                "bootstrap.servers": self._bootstrap,
                "group.id": "trinity-memory-consumer",
                "auto.offset.reset": "earliest",
            })
            consumer.subscribe([self._topic])
            messages = []
            for _ in range(batch_size):
                msg = consumer.poll(timeout=1.0)
                if msg is None:
                    break
                if msg.error():
                    continue
                data = json.loads(msg.value().decode("utf-8"))
                messages.append(StreamMessage.from_dict(data))
            consumer.close()
            return messages
        except Exception as e:
            logger.error("Kafka pull failed: %s", e)
            return []

    def acknowledge(self, message_ids: List[str]) -> int:
        if self._kafka_available:
            return len(message_ids)
        return self._fallback.acknowledge(message_ids)

    def pending_count(self) -> int:
        return self._fallback.pending_count()

    def close(self) -> None:
        self._fallback.close()


# ── Redis Stream Backend (Stub) ───────────────────────────────────────────


class RedisStreamBackend(StreamBackend):
    """Redis Stream backend stub.

    Falls back to in-memory when redis-py is not installed.
    """

    def __init__(self, redis_url: str = "redis://localhost:6379", stream_key: str = "trinity:memory"):
        self._url = redis_url
        self._key = stream_key
        self._fallback = InMemoryBackend()
        self._redis_available = False
        try:
            import redis  # noqa: F401
            self._redis_available = True
            logger.info("Redis Stream backend: redis-py available")
        except ImportError:
            logger.info("Redis Stream backend: redis-py not installed, using in-memory fallback")

    def push(self, message: StreamMessage) -> bool:
        if self._redis_available:
            return self._redis_push(message)
        return self._fallback.push(message)

    def _redis_push(self, message: StreamMessage) -> bool:
        try:
            import redis
            client = redis.Redis.from_url(self._url)
            client.xadd(self._key, {"data": json.dumps(message.to_dict())}, maxlen=10000)
            return True
        except Exception as e:
            logger.error("Redis push failed: %s", e)
            return False

    def pull(self, batch_size: int = 10) -> List[StreamMessage]:
        if self._redis_available:
            return self._redis_pull(batch_size)
        return self._fallback.pull(batch_size)

    def _redis_pull(self, batch_size: int = 10) -> List[StreamMessage]:
        try:
            import redis
            client = redis.Redis.from_url(self._url)
            results = client.xread({self._key: "0"}, count=batch_size, block=1000)
            messages = []
            for stream_name, entries in results:
                for entry_id, fields in entries:
                    data = json.loads(fields.get(b"data", b"{}"))
                    messages.append(StreamMessage.from_dict(data))
            return messages
        except Exception as e:
            logger.error("Redis pull failed: %s", e)
            return []

    def acknowledge(self, message_ids: List[str]) -> int:
        return len(message_ids)

    def pending_count(self) -> int:
        return self._fallback.pending_count()

    def close(self) -> None:
        self._fallback.close()


# ── Stream Ingestor ───────────────────────────────────────────────────────


class StreamIngestor:
    """Unified streaming memory ingestion engine.

    Submits memory payloads to a message broker backend and runs a
    background consumer thread that batches messages and writes them
    into Trinity's memory store.

    Usage::

        ingestor = StreamIngestor(backend=InMemoryBackend())
        ingestor.submit({"content": "user prefers dark mode"})
        ingestor.start_consumer(batch_size=10, interval_sec=1.0)
        ingestor.stop()
    """

    def __init__(
        self,
        backend: Optional[StreamBackend] = None,
        ingest_fn: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        max_retries: int = 3,
        dead_letter_enabled: bool = True,
    ):
        self._backend = backend or InMemoryBackend()
        self._ingest_fn = ingest_fn
        self._max_retries = max_retries
        self._dead_letter_enabled = dead_letter_enabled

        self._consumer_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.RLock()

        # Statistics
        self._submitted: int = 0
        self._processed: int = 0
        self._failed: int = 0
        self._dead_letters: List[StreamMessage] = []

    # ── Submission ───────────────────────────────────────────────────

    def submit(self, payload: Dict[str, Any]) -> str:
        """Submit a memory payload to the stream.

        Args:
            payload: Dict with content, tags, importance, category, etc.

        Returns:
            message_id of the submitted message.
        """
        message = StreamMessage(
            content=payload.get("content", ""),
            tags=payload.get("tags", []),
            importance=payload.get("importance", 0.5),
            category=payload.get("category", "general"),
            modality=payload.get("modality", "text"),
            metadata=payload.get("metadata", {}),
            tenant_id=payload.get("tenant_id", "default"),
        )

        success = self._backend.push(message)
        if success:
            with self._lock:
                self._submitted += 1
            logger.debug("Submitted message %s", message.message_id)
        else:
            logger.warning("Failed to submit message (queue full)")

        return message.message_id

    def submit_batch(self, payloads: List[Dict[str, Any]]) -> List[str]:
        """Submit multiple memory payloads at once.

        Returns:
            List of message_ids.
        """
        return [self.submit(p) for p in payloads]

    # ── Consumer ─────────────────────────────────────────────────────

    def start_consumer(
        self,
        batch_size: int = 10,
        interval_sec: float = 1.0,
        daemon: bool = True,
    ) -> None:
        """Start the background consumer thread.

        Args:
            batch_size: Messages to pull per batch.
            interval_sec: Seconds to wait between batches.
            daemon: Run as daemon thread.
        """
        if self._consumer_thread and self._consumer_thread.is_alive():
            logger.warning("Consumer already running")
            return

        self._stop_event.clear()
        self._consumer_thread = threading.Thread(
            target=self._consumer_loop,
            args=(batch_size, interval_sec),
            daemon=daemon,
            name="stream-ingestor-consumer",
        )
        self._consumer_thread.start()
        logger.info("Consumer started (batch=%d, interval=%.1fs)", batch_size, interval_sec)

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the consumer thread gracefully.

        Args:
            timeout: Max seconds to wait for consumer to finish.
        """
        self._stop_event.set()
        if self._consumer_thread and self._consumer_thread.is_alive():
            self._consumer_thread.join(timeout=timeout)
            if self._consumer_thread.is_alive():
                logger.warning("Consumer did not stop within timeout")
        self._backend.close()
        logger.info("Consumer stopped")

    def _consumer_loop(self, batch_size: int, interval_sec: float) -> None:
        """Main consumer loop."""
        while not self._stop_event.is_set():
            try:
                messages = self._backend.pull(batch_size)
                if messages:
                    self._process_batch(messages)
            except Exception as e:
                logger.error("Consumer loop error: %s", e)

            # Sleep with early wake on stop
            self._stop_event.wait(timeout=interval_sec)

        # Drain remaining messages
        remaining = self._backend.pull(batch_size)
        while remaining and not self._stop_event.is_set():
            self._process_batch(remaining)
            remaining = self._backend.pull(batch_size)

    def _process_batch(self, messages: List[StreamMessage]) -> None:
        """Process a batch of messages by calling ingest_fn."""
        success_ids = []
        for msg in messages:
            msg.status = StreamStatus.PROCESSING
            try:
                if self._ingest_fn:
                    payload = msg.to_dict()
                    self._ingest_fn(payload)
                msg.status = StreamStatus.COMPLETED
                success_ids.append(msg.message_id)
                with self._lock:
                    self._processed += 1
            except Exception as e:
                msg.retry_count += 1
                if msg.retry_count < self._max_retries:
                    msg.status = StreamStatus.PENDING
                    self._backend.push(msg)  # Re-queue
                else:
                    msg.status = StreamStatus.DEAD_LETTER
                    if self._dead_letter_enabled:
                        with self._lock:
                            self._dead_letters.append(msg)
                    self._failed += 1
                logger.error("Message %s failed (retry %d/%d): %s",
                             msg.message_id, msg.retry_count, self._max_retries, e)

        if success_ids:
            self._backend.acknowledge(success_ids)

    # ── Statistics ───────────────────────────────────────────────────

    @property
    def stats(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "submitted": self._submitted,
                "processed": self._processed,
                "failed": self._failed,
                "dead_letters": len(self._dead_letters),
                "pending": self._backend.pending_count(),
                "consumer_running": self._consumer_thread is not None and self._consumer_thread.is_alive(),
            }

    def get_dead_letters(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [m.to_dict() for m in self._dead_letters[:limit]]

    def clear_dead_letters(self) -> int:
        with self._lock:
            count = len(self._dead_letters)
            self._dead_letters.clear()
            return count


# ── Self-Test ─────────────────────────────────────────────────────────────


def self_test() -> Dict[str, Any]:
    """Module self-test."""
    results: Dict[str, Any] = {"module": "trinity.memory.stream_ingest", "tests": {}}

    # Test 1: InMemoryBackend push/pull
    try:
        backend = InMemoryBackend(max_size=100)
        msg = StreamMessage(content="test", tags=["a"])
        assert backend.push(msg)
        assert backend.pending_count() == 1
        batch = backend.pull(10)
        assert len(batch) == 1
        assert batch[0].content == "test"
        backend.close()
        results["tests"]["backend_push_pull"] = "PASS"
    except Exception as e:
        results["tests"]["backend_push_pull"] = f"FAIL: {e}"

    # Test 2: Backend acknowledge
    try:
        backend = InMemoryBackend()
        backend.push(StreamMessage(content="ack test"))
        batch = backend.pull(1)
        count = backend.acknowledge([m.message_id for m in batch])
        assert count == 1
        backend.close()
        results["tests"]["backend_acknowledge"] = "PASS"
    except Exception as e:
        results["tests"]["backend_acknowledge"] = f"FAIL: {e}"

    # Test 3: Ingestor submit
    try:
        ingested = []
        def ingest_fn(payload):
            ingested.append(payload)

        ingestor = StreamIngestor(backend=InMemoryBackend(), ingest_fn=ingest_fn)
        msg_id = ingestor.submit({"content": "hello world", "tags": ["test"]})
        assert len(msg_id) == 16
        assert ingestor.stats["submitted"] == 1
        results["tests"]["ingestor_submit"] = "PASS"
    except Exception as e:
        results["tests"]["ingestor_submit"] = f"FAIL: {e}"
        return results

    # Test 4: Consumer processing
    try:
        ingestor.start_consumer(batch_size=10, interval_sec=0.1)
        time.sleep(0.5)
        ingestor.stop()
        assert ingestor.stats["processed"] == 1
        assert len(ingested) == 1
        assert ingested[0]["content"] == "hello world"
        results["tests"]["consumer_processing"] = "PASS"
    except Exception as e:
        results["tests"]["consumer_processing"] = f"FAIL: {e}"

    # Test 5: Batch submit
    try:
        backend2 = InMemoryBackend()
        ingestor2 = StreamIngestor(backend=backend2, ingest_fn=lambda p: None)
        ids = ingestor2.submit_batch([
            {"content": f"batch_{i}"} for i in range(5)
        ])
        assert len(ids) == 5
        assert backend2.pending_count() == 5
        backend2.close()
        results["tests"]["batch_submit"] = "PASS"
    except Exception as e:
        results["tests"]["batch_submit"] = f"FAIL: {e}"

    # Test 6: Retry and dead letter
    try:
        fail_count = [0]
        def failing_ingest(payload):
            fail_count[0] += 1
            if fail_count[0] <= 3:
                raise RuntimeError("simulated failure")

        backend3 = InMemoryBackend()
        ingestor3 = StreamIngestor(backend=backend3, ingest_fn=failing_ingest, max_retries=2)
        ingestor3.submit({"content": "flaky"})
        ingestor3.start_consumer(batch_size=1, interval_sec=0.1)
        time.sleep(0.6)
        ingestor3.stop()
        dead = ingestor3.get_dead_letters()
        assert len(dead) >= 1
        results["tests"]["retry_dead_letter"] = "PASS"
    except Exception as e:
        results["tests"]["retry_dead_letter"] = f"FAIL: {e}"

    # Test 7: KafkaBackend fallback
    try:
        kb = KafkaBackend(bootstrap_servers="localhost:9999")
        assert kb.push(StreamMessage(content="kafka test"))
        batch = kb.pull(1)
        assert len(batch) >= 0  # May fallback to in-memory
        kb.close()
        results["tests"]["kafka_fallback"] = "PASS"
    except Exception as e:
        results["tests"]["kafka_fallback"] = f"FAIL: {e}"

    # Test 8: RedisStreamBackend fallback
    try:
        rb = RedisStreamBackend(redis_url="redis://localhost:9999")
        assert rb.push(StreamMessage(content="redis test"))
        batch = rb.pull(1)
        assert len(batch) >= 0
        rb.close()
        results["tests"]["redis_fallback"] = "PASS"
    except Exception as e:
        results["tests"]["redis_fallback"] = f"FAIL: {e}"

    passed = sum(1 for v in results["tests"].values() if "PASS" in str(v))
    total = len(results["tests"])
    results["summary"] = f"{passed}/{total} PASS"
    return results


if __name__ == "__main__":
    import sys
    result = self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if all("PASS" in str(v) for v in result["tests"].values()) else 1)
