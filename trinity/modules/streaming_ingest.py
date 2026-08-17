"""
Trinity 流式记忆摄入模块 (Streaming Memory Ingest)

提供高吞吐、线程安全的记忆流式写入能力：
- 环形缓冲区 + 背压控制
- 批量写入 + 定时刷新
- 上下文管理器优雅关闭
"""

import threading
import time
import logging
from collections import deque
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class StreamBuffer:
    """环形缓冲区，支持背压控制。

    当缓冲区满时，根据 backpressure 策略决定行为：
    - 'block'：阻塞等待空间释放（默认）
    - 'drop'：丢弃最旧条目
    - 'reject'：拒绝新条目
    """

    def __init__(self, max_size: int = 100, backpressure: str = 'block'):
        if backpressure not in ('block', 'drop', 'reject'):
            raise ValueError(f"Invalid backpressure strategy: {backpressure}")

        self._max_size = max_size
        self._backpressure = backpressure
        self._buffer: deque = deque(maxlen=max_size if backpressure == 'drop' else None)
        self._lock = threading.Condition(threading.Lock())
        self._closed = False

    def put(self, item: Any, timeout: float = 5.0) -> bool:
        """向缓冲区写入一个条目。

        Args:
            item: 要写入的条目
            timeout: 'block' 策略下的最大等待秒数

        Returns:
            True 表示写入成功，False 表示被拒绝或丢弃
        """
        with self._lock:
            if self._closed:
                return False

            if self._backpressure == 'drop':
                self._buffer.append(item)
                return True

            if self._backpressure == 'reject':
                if len(self._buffer) >= self._max_size:
                    return False
                self._buffer.append(item)
                self._lock.notify_all()
                return True

            # block 策略
            if len(self._buffer) >= self._max_size:
                if not self._lock.wait(timeout=timeout):
                    return False
                if self._closed:
                    return False
                if len(self._buffer) >= self._max_size:
                    return False

            self._buffer.append(item)
            self._lock.notify_all()
            return True

    def get_batch(self, batch_size: int) -> list:
        """取出最多 batch_size 个条目。

        Args:
            batch_size: 最大取出数量

        Returns:
            条目列表（可能为空）
        """
        with self._lock:
            batch = []
            while len(batch) < batch_size and self._buffer:
                batch.append(self._buffer.popleft())
            self._lock.notify_all()
            return batch

    def get_all(self) -> list:
        """取出当前缓冲区中全部条目。"""
        with self._lock:
            items = list(self._buffer)
            self._buffer.clear()
            self._lock.notify_all()
            return items

    def __len__(self) -> int:
        with self._lock:
            return len(self._buffer)

    @property
    def size(self) -> int:
        return len(self)

    @property
    def is_full(self) -> bool:
        with self._lock:
            return len(self._buffer) >= self._max_size

    def close(self):
        """关闭缓冲区，唤醒所有等待线程。"""
        with self._lock:
            self._closed = True
            self._lock.notify_all()


class StreamingMemoryIngest:
    """流式记忆摄入器。

    接收单条记忆内容，在后台自动批量写入 memory_store。
    支持上下文管理器，退出时自动刷新。

    Usage:
        with StreamingMemoryIngest(memory_store) as si:
            si.ingest("user said hello", {"source": "chat"})
            si.ingest("document summary ...", {"source": "file"})
    """

    def __init__(
        self,
        memory_store: Callable,
        batch_size: int = 5,
        flush_interval: float = 10.0,
        buffer_max_size: int = 500,
        backpressure: str = 'block',
    ):
        """
        Args:
            memory_store: 持久化写入回调，签名为 memory_store(batch: list[dict]) -> int
            batch_size: 批量大小，达到后自动刷新
            flush_interval: 刷新间隔（秒），超时后自动刷新
            buffer_max_size: 环形缓冲区最大容量
            backpressure: 背压策略（'block' / 'drop' / 'reject'）
        """
        if not callable(memory_store):
            raise TypeError("memory_store must be callable")

        self._memory_store = memory_store
        self._batch_size = max(1, batch_size)
        self._flush_interval = max(0.1, flush_interval)
        self._buffer = StreamBuffer(max_size=buffer_max_size, backpressure=backpressure)

        # 状态
        self._total_written = 0
        self._total_ingested = 0
        self._last_flush_time = time.time()
        self._lock = threading.Lock()
        self._running = False
        self._flush_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def ingest(self, content: str, metadata: dict = None) -> bool:
        """接收单条记忆，放入缓冲区。

        Args:
            content: 记忆文本内容
            metadata: 可选元数据字典

        Returns:
            True 表示成功进入缓冲区，False 表示被拒绝
        """
        if not content or not isinstance(content, str):
            logger.warning("ingest: content must be a non-empty string")
            return False

        item = {
            'content': content,
            'metadata': metadata or {},
            'timestamp': time.time(),
        }
        ok = self._buffer.put(item)
        if ok:
            with self._lock:
                self._total_ingested += 1
        return ok

    def flush(self) -> int:
        """手动强制刷新缓冲区，将所有待处理条目写入 memory_store。

        Returns:
            本次成功写入的条目数
        """
        items = self._buffer.get_all()
        if not items:
            return 0

        written = self._write_batch(items)
        with self._lock:
            self._last_flush_time = time.time()
        return written

    def get_buffer_status(self) -> dict:
        """返回当前缓冲状态。

        Returns:
            dict: {
                'buffer_size': 当前缓冲条目数,
                'buffer_capacity': 缓冲区最大容量,
                'last_flush_time': 上次刷新 Unix 时间戳,
                'total_ingested': 累计接收数,
                'total_written': 累计写入数,
                'is_running': 后台线程是否运行中,
            }
        """
        with self._lock:
            return {
                'buffer_size': len(self._buffer),
                'buffer_capacity': self._buffer._max_size,
                'last_flush_time': self._last_flush_time,
                'total_ingested': self._total_ingested,
                'total_written': self._total_written,
                'is_running': self._running,
            }

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False  # 不吞异常

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self):
        """启动后台刷新线程。"""
        if self._running:
            return
        self._running = True
        self._flush_event.clear()
        self._worker_thread = threading.Thread(
            target=self._buffer_manager,
            name="streaming-ingest-worker",
            daemon=True,
        )
        self._worker_thread.start()
        logger.info("StreamingMemoryIngest worker started")

    def stop(self, timeout: float = 10.0):
        """停止后台线程，flush 剩余数据。

        Args:
            timeout: 等待后台线程退出的最大秒数
        """
        if not self._running:
            return

        self._running = False
        self._flush_event.set()

        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=timeout)
            if self._worker_thread.is_alive():
                logger.warning("StreamingMemoryIngest worker did not exit within timeout")

        # 最后 flush 一次
        remaining = len(self._buffer)
        if remaining > 0:
            self.flush()

        self._buffer.close()
        logger.info(
            "StreamingMemoryIngest stopped — ingested=%d written=%d",
            self._total_ingested,
            self._total_written,
        )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _buffer_manager(self):
        """后台线程：定时检查缓冲区，满足条件时批量写入。"""
        while self._running:
            triggered = self._flush_event.wait(timeout=self._flush_interval)

            if triggered:
                # 外部信号唤醒（通常是 stop）
                break

            # 检查缓冲区大小
            if len(self._buffer) >= self._batch_size:
                self._flush_batch()

        logger.debug("_buffer_manager exiting")

    def _flush_batch(self):
        """从缓冲区取出一个 batch 并写入。"""
        items = self._buffer.get_batch(self._batch_size)
        if items:
            written = self._write_batch(items)
            with self._lock:
                self._last_flush_time = time.time()
            return written
        return 0

    def _write_batch(self, items: list) -> int:
        """调用 memory_store 写入批量条目，带异常保护。

        Args:
            items: 条目列表

        Returns:
            成功写入的条目数
        """
        if not items:
            return 0
        try:
            result = self._memory_store(items)
            if isinstance(result, int):
                count = result
            else:
                count = len(items)  # 回调不返回 int 时假设全成功
        except Exception as exc:
            logger.error("memory_store write failed: %s", exc)
            count = 0

        with self._lock:
            self._total_written += count
        return count
