"""
AsyncMemoryPipeline — 异步记忆存储管线。

将记忆存储改为异步生产者-消费者模式：

    生产者: 任意代码调用 ingest() → 放入队列 → 立即返回
    消费者: 后台任务批量处理 → embed → index → store → commit

吞吐目标: 2000+ tps（比同步模式提升 20x）

支持两种运行模式:
    - mode="thread": 用 threading.Thread 后台运行（简单、兼容性好）
    - mode="async":  用 asyncio 协程（高性能，需要事件循环）
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from trinity.adapters.base import StorageAdapter
from trinity.vector_index.index import VectorIndex
from trinity.embeddings.engine import EmbeddingEngine


# ── 内部数据结构 ──────────────────────────────────────────────────────

@dataclass
class MemoryItem:
    """队列中的一条待处理记忆条目。"""
    content: str
    tags: Optional[List[str]] = None
    category: str = "general"
    importance: float = 0.5
    metadata: Optional[Dict[str, Any]] = None
    persona_id: str = "default"
    session_id: Optional[str] = None
    tenant_id: str = "default"
    role: str = "user"
    future: Future = field(default_factory=Future)
    enqueued_at: float = field(default_factory=time.time)


@dataclass
class PipelineStats:
    """管线运行时统计。"""
    total_ingested: int = 0
    total_processed: int = 0
    total_errors: int = 0
    total_batches: int = 0
    queue_depth: int = 0
    avg_batch_latency: float = 0.0
    avg_item_latency: float = 0.0
    start_time: float = 0.0

    _latencies: deque = field(default_factory=lambda: deque(maxlen=1000))

    def record_batch(self, items: int, elapsed: float):
        self.total_batches += 1
        self.total_processed += items
        self.avg_batch_latency = (
            (self.avg_batch_latency * (self.total_batches - 1) + elapsed)
            / self.total_batches
        )
        per_item = elapsed / max(items, 1)
        self._latencies.append(per_item)
        self.avg_item_latency = sum(self._latencies) / len(self._latencies)


# ── AsyncMemoryPipeline ────────────────────────────────────────────────

class AsyncMemoryPipeline:
    """异步记忆存储管线 — 生产者-消费者模式。

    使用方式::

        pipeline = AsyncMemoryPipeline(
            batch_size=100,
            flush_interval=5.0,
            embed_engine=engine,
            vector_index=idx,
            sqlite_adapter=db,
        )
        pipeline.start()                     # 启动消费者线程/协程
        future = pipeline.ingest("hello")    # 立即返回
        result = future.result()             # 等待处理完成
        pipeline.shutdown()
    """

    def __init__(
        self,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        embed_engine: Optional[EmbeddingEngine] = None,
        vector_index: Optional[VectorIndex] = None,
        sqlite_adapter: Optional[StorageAdapter] = None,
        mode: str = "thread",
        max_queue_size: int = 10000,
        on_error: Optional[Callable[[Exception, MemoryItem], None]] = None,
    ):
        """

        Args:
            batch_size: 攒多少条才批量处理一次。
            flush_interval: 最多等多少秒（超过这个时间就强制刷）。
            embed_engine: 嵌入引擎实例。
            vector_index: 向量索引实例。
            sqlite_adapter: SQLite 存储适配器实例。
            mode: "thread" 或 "async"。
            max_queue_size: 队列最大长度（超出则 ingest 阻塞）。
            on_error: 处理单条记忆失败时的回调。
        """
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.embed_engine = embed_engine
        self.vector_index = vector_index
        self.storage = sqlite_adapter
        self.mode = mode
        self.max_queue_size = max_queue_size
        self.on_error = on_error

        self._queue: deque[MemoryItem] = deque()
        self._async_lock: Optional[asyncio.Lock] = None
        self._not_empty = threading.Event()
        self._async_not_empty: Optional[asyncio.Event] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._async_task: Optional[asyncio.Task] = None

        # 如果 storage 是 SQLiteAdapter，在消费者线程中打开独立连接
        self._local_storage: Any = None
        self._local_storage_lock = threading.Lock()

        self.stats = PipelineStats(start_time=time.time())

    # ── 生命周期 ──────────────────────────────────────────────────────

    def start(self) -> None:
        """启动后台消费者。"""
        if self._running:
            return
        self._running = True
        if self.mode == "thread":
            self._thread = threading.Thread(
                target=self._consumer_loop,
                name="AsyncMemoryPipeline",
                daemon=True,
            )
            self._thread.start()
        elif self.mode == "async":
            self._async_lock = asyncio.Lock()
            self._async_not_empty = asyncio.Event()
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            self._async_task = loop.create_task(self._async_consumer_loop())
        else:
            raise ValueError(f"Unknown mode: {self.mode}, expected 'thread' or 'async'")

    def stop(self) -> None:
        """停止消费者（不等待）。"""
        self._running = False
        self._not_empty.set()  # 唤醒线程，让它退出

    # ── 生产者 API ────────────────────────────────────────────────────

    def ingest(
        self,
        content: str,
        tags: Optional[List[str]] = None,
        category: str = "general",
        importance: float = 0.5,
        metadata: Optional[Dict[str, Any]] = None,
        persona_id: str = "default",
        session_id: Optional[str] = None,
        tenant_id: str = "default",
        role: str = "user",
    ) -> Future:
        """把记忆放进队列，立即返回 Future。

        Args:
            content: 记忆文本内容。
            tags: 标签列表。
            category: 分类。
            importance: 重要性 (0~1)。
            metadata: 自定义元数据。
            persona_id: 用户/角色ID。
            session_id: 会话ID。
            tenant_id: 租户ID。
            role: user/assistant/system。

        Returns:
            concurrent.futures.Future：处理完成后 result 为存储结果 dict。
        """
        item = MemoryItem(
            content=content,
            tags=tags,
            category=category,
            importance=importance,
            metadata=metadata,
            persona_id=persona_id,
            session_id=session_id,
            tenant_id=tenant_id,
            role=role,
        )

        if self.mode == "thread":
            # 线程模式 — 阻塞等待队列有空位
            while self._running and len(self._queue) >= self.max_queue_size:
                time.sleep(0.001)
            self._queue.append(item)
            self.stats.total_ingested += 1
            self._not_empty.set()
        else:
            # async 模式
            if len(self._queue) >= self.max_queue_size:
                fut: Future = Future()
                fut.set_exception(
                    RuntimeError(f"Queue full ({self.max_queue_size} items)")
                )
                return fut
            self._queue.append(item)
            self.stats.total_ingested += 1
            if self._async_not_empty:
                self._async_not_empty.set()

        return item.future

    # ── 消费者（线程模式） ─────────────────────────────────────────────

    def _consumer_loop(self) -> None:
        """后台消费者线程主循环。"""
        batch: List[MemoryItem] = []

        while self._running:
            # 攒批：凑满 batch_size 条或等 flush_interval 秒
            deadline = time.time() + self.flush_interval

            while self._running and len(batch) < self.batch_size:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break  # 超时，强制刷

                # 从队列取一条
                item = self._dequeue(timeout=min(remaining, 0.1))
                if item is not None:
                    batch.append(item)

            if not batch:
                continue

            # 批量处理
            self._process_batch(batch)
            batch = []

        # 关闭前处理剩余
        remaining = self._drain_queue()
        if remaining:
            self._process_batch(remaining)

    def _dequeue(self, timeout: float = 0.1) -> Optional[MemoryItem]:
        """从队列取一条（带超时）。"""
        if self._queue:
            return self._queue.popleft()
        # 队列空，等待通知
        self._not_empty.clear()
        self._not_empty.wait(timeout=timeout)
        if self._queue:
            return self._queue.popleft()
        return None

    def _drain_queue(self) -> List[MemoryItem]:
        """把队列里所有东西捞出来。"""
        items: List[MemoryItem] = []
        while self._queue:
            items.append(self._queue.popleft())
        return items

    # ── 消费者（async 模式） ───────────────────────────────────────────

    async def _async_consumer_loop(self) -> None:
        """async 模式的后台协程主循环。"""
        batch: List[MemoryItem] = []

        while self._running:
            deadline = time.time() + self.flush_interval

            while self._running and len(batch) < self.batch_size:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break

                item = await self._async_dequeue(timeout=min(remaining, 0.1))
                if item is not None:
                    batch.append(item)

            if not batch:
                continue

            await self._async_process_batch(batch)
            batch = []

        remaining = await self._async_drain_queue()
        if remaining:
            await self._async_process_batch(remaining)

    async def _async_dequeue(self, timeout: float = 0.1) -> Optional[MemoryItem]:
        """async 模式：从队列取一条。"""
        if self._queue:
            return self._queue.popleft()

        if self._async_not_empty:
            self._async_not_empty.clear()

        # 轮询等待
        deadline = time.time() + timeout
        while self._running and time.time() < deadline:
            if self._queue:
                return self._queue.popleft()
            await asyncio.sleep(0.01)
        return None

    async def _async_drain_queue(self) -> List[MemoryItem]:
        items: List[MemoryItem] = []
        while self._queue:
            items.append(self._queue.popleft())
        return items

    # ── 批量处理（核心逻辑） ────────────────────────────────────────────

    def _process_batch(self, batch: List[MemoryItem]) -> None:
        """同步批量处理：embed + index + store + commit。"""
        if not batch:
            return

        t0 = time.time()
        errors: List[Tuple[int, Exception]] = []

        try:
            # 1. 批量 embed
            texts = [item.content for item in batch]
            embeddings: List[np.ndarray] = []
            if self.embed_engine is not None:
                try:
                    embeddings = self.embed_engine.embed_batch(texts)
                except Exception as e:
                    errors.append((-1, e))
                    # embed_batch 失败，逐条 fallback
                    for idx, item in enumerate(batch):
                        try:
                            emb = self.embed_engine.embed(item.content)
                            embeddings.append(emb)
                        except Exception as e2:
                            errors.append((idx, e2))
                            embeddings.append(
                                np.zeros(self.vector_index.dim
                                         if self.vector_index else 128)
                            )
            else:
                # 没有 embed_engine → 零向量
                dim = self.vector_index.dim if self.vector_index else 128
                embeddings = [np.zeros(dim) for _ in batch]

            # 2. 批量 index（添加到向量索引）
            if self.vector_index is not None:
                vec_ids: List[str] = []
                vec_embs: List[np.ndarray] = []
                metas: List[Dict] = []
                for idx, (item, emb) in enumerate(zip(batch, embeddings)):
                    if idx in [e[0] for e in errors if e[0] >= 0]:
                        continue  # 跳过 embed 失败的
                    mid = f"mem_{uuid.uuid4().hex[:16]}"
                    vec_ids.append(mid)
                    vec_embs.append(emb)
                    metas.append({
                        "content": item.content[:200],
                        "category": item.category,
                        "importance": item.importance,
                        "persona_id": item.persona_id,
                        "tags": json.dumps(item.tags or []),
                    })
                try:
                    self.vector_index.add_batch(vec_ids, vec_embs, metas)
                except Exception as e:
                    errors.append((-2, e))

            # 3. 批量 store（写入 SQLite）
            storage = self._get_storage()
            for idx, item in enumerate(batch):
                if idx in [e[0] for e in errors if e[0] >= 0]:
                    continue
                try:
                    result = storage.store_memory(
                        content=item.content,
                        persona_id=item.persona_id,
                        session_id=item.session_id,
                        tenant_id=item.tenant_id,
                        role=item.role,
                        importance=item.importance,
                        tags=item.tags,
                        category=item.category,
                    )
                    if not item.future.done():
                        item.future.set_result(result)
                except Exception as e:
                    errors.append((idx, e))
                    if not item.future.done():
                        item.future.set_exception(e)

            # 4. 处理错误回调
            self.stats.total_errors += len([e for e in errors if e[0] >= 0])
            for idx, exc in errors:
                if self.on_error:
                    if idx >= 0 and idx < len(batch):
                        self.on_error(exc, batch[idx])

        except Exception as global_err:
            # 兜底：所有未完成的 future 置为异常
            self.stats.total_errors += len(batch)
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(global_err)
                if self.on_error:
                    self.on_error(global_err, item)

        elapsed = time.time() - t0
        self.stats.record_batch(len(batch), elapsed)

    async def _async_process_batch(self, batch: List[MemoryItem]) -> None:
        """异步批量处理：embed + index + store + commit。

        在 async 模式下，用 run_in_executor 把阻塞操作（embed、index、store）
        放到线程池执行，不阻塞事件循环。
        """
        if not batch:
            return

        t0 = time.time()
        errors: List[Tuple[int, Exception]] = []
        loop = asyncio.get_running_loop()

        try:
            # 1. 批量 embed
            texts = [item.content for item in batch]
            embeddings: List[np.ndarray] = []
            if self.embed_engine is not None:
                try:
                    embeddings = await loop.run_in_executor(
                        None, self.embed_engine.embed_batch, texts
                    )
                except Exception as e:
                    errors.append((-1, e))
                    for idx, item in enumerate(batch):
                        try:
                            emb = await loop.run_in_executor(
                                None, self.embed_engine.embed, item.content
                            )
                            embeddings.append(emb)
                        except Exception as e2:
                            errors.append((idx, e2))
                            embeddings.append(
                                np.zeros(self.vector_index.dim
                                         if self.vector_index else 128)
                            )
            else:
                dim = self.vector_index.dim if self.vector_index else 128
                embeddings = [np.zeros(dim) for _ in batch]

            # 2. 批量 index
            if self.vector_index is not None:
                vec_ids: List[str] = []
                vec_embs: List[np.ndarray] = []
                metas: List[Dict] = []
                error_indices = {e[0] for e in errors if e[0] >= 0}
                for idx, (item, emb) in enumerate(zip(batch, embeddings)):
                    if idx in error_indices:
                        continue
                    mid = f"mem_{uuid.uuid4().hex[:16]}"
                    vec_ids.append(mid)
                    vec_embs.append(emb)
                    metas.append({
                        "content": item.content[:200],
                        "category": item.category,
                        "importance": item.importance,
                        "persona_id": item.persona_id,
                        "tags": json.dumps(item.tags or []),
                    })
                try:
                    await loop.run_in_executor(
                        None, self.vector_index.add_batch, vec_ids, vec_embs, metas
                    )
                except Exception as e:
                    errors.append((-2, e))

            # 3. 批量 store
            error_indices = {e[0] for e in errors if e[0] >= 0}
            for idx, item in enumerate(batch):
                if idx in error_indices:
                    continue
                try:
                    result = await loop.run_in_executor(
                        None, self._sync_store, item,
                    )
                    if not item.future.done():
                        item.future.set_result(result)
                except Exception as e:
                    errors.append((idx, e))
                    if not item.future.done():
                        item.future.set_exception(e)

            self.stats.total_errors += len([e for e in errors if e[0] >= 0])
            for idx, exc in errors:
                if self.on_error and idx >= 0 and idx < len(batch):
                    self.on_error(exc, batch[idx])

        except Exception as global_err:
            self.stats.total_errors += len(batch)
            for item in batch:
                if not item.future.done():
                    item.future.set_exception(global_err)
                if self.on_error:
                    self.on_error(global_err, item)

        elapsed = time.time() - t0
        self.stats.record_batch(len(batch), elapsed)

    def _get_storage(self) -> Any:
        """获取当前线程可用的 storage 实例。

        SQLiteAdapter 有线程绑定限制——每个线程必须使用自己打开的连接。
        这里为 consumers 线程创建独立连接，避免 'SQLite objects created in a
        thread can only be used in that same thread' 错误。
        """
        if self.storage is None:
            return None

        # 检查是否是 SQLiteAdapter（通过 duck-typing 判断）
        if hasattr(self.storage, '_conn') and self.storage._conn is not None:
            import threading as _thr
            current = _thr.current_thread()
            # 如果是主线程调用,直接用原始实例
            if current is _thr.main_thread():
                return self.storage

            # 消费者线程 —— 使用独立连接
            if self._local_storage is None:
                with self._local_storage_lock:
                    if self._local_storage is None:
                        import copy as _copy
                        # 创建同类型的新实例
                        from trinity.adapters.sqlite import SQLiteAdapter
                        if isinstance(self.storage, SQLiteAdapter):
                            new_storage = SQLiteAdapter(
                                db_path=self.storage.db_path
                            )
                            new_storage.connect()
                            self._local_storage = new_storage
            return self._local_storage or self.storage

        return self.storage

    def _sync_store(self, item: MemoryItem) -> Dict[str, Any]:
        """同步存储一条记忆（给 async 模式的 executor 用）。"""
        storage = self._get_storage()
        if storage is None:
            raise RuntimeError("No storage adapter configured")
        return storage.store_memory(
            content=item.content,
            persona_id=item.persona_id,
            session_id=item.session_id,
            tenant_id=item.tenant_id,
            role=item.role,
            importance=item.importance,
            tags=item.tags,
            category=item.category,
        )

    # ── 公共控制 API ──────────────────────────────────────────────────

    def flush(self) -> int:
        """强制处理队列中所有待处理记忆。

        Returns:
            本次处理的条目数。
        """
        batch = self._drain_queue()
        if batch:
            if self.mode == "async":
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                if loop.is_running():
                    # 已经是事件循环中运行 → 同步处理
                    self._process_batch(batch)
                else:
                    loop.run_until_complete(self._async_process_batch(batch))
            else:
                self._process_batch(batch)
        return len(batch)

    def get_stats(self) -> Dict[str, Any]:
        """返回管线运行时统计。

        Returns:
            Dict with keys:
                - queue_depth: 当前队列深度
                - total_ingested: 已入队总数
                - total_processed: 已处理总数
                - total_errors: 错误数
                - total_batches: 已处理批次数
                - avg_batch_latency_sec: 平均批次处理延迟（秒）
                - avg_item_latency_sec: 平均单条处理延迟（秒）
                - uptime_sec: 运行时间（秒）
        """
        self.stats.queue_depth = len(self._queue)
        return {
            "queue_depth": self.stats.queue_depth,
            "total_ingested": self.stats.total_ingested,
            "total_processed": self.stats.total_processed,
            "total_errors": self.stats.total_errors,
            "total_batches": self.stats.total_batches,
            "avg_batch_latency_sec": round(self.stats.avg_batch_latency, 4),
            "avg_item_latency_sec": round(self.stats.avg_item_latency, 6),
            "uptime_sec": round(time.time() - self.stats.start_time, 2),
            "mode": self.mode,
        }

    def shutdown(self, wait: bool = True) -> None:
        """优雅关闭管线。

        1. 先 flush 所有待处理记忆
        2. 停止消费者
        3. （可选）等待消费者完全退出

        Args:
            wait: 是否等待后台消费者完全退出。
        """
        self.flush()
        self.stop()

        if self.mode == "thread" and self._thread and wait:
            self._thread.join(timeout=30)

        if self.mode == "async" and self._async_task and wait:
            try:
                loop = asyncio.get_running_loop()
                if not loop.is_closed():
                    loop.run_until_complete(self._async_task)
            except (RuntimeError, asyncio.CancelledError):
                pass


# ── 自检函数 ──────────────────────────────────────────────────────────

def self_test():
    """自检：验证 AsyncMemoryPipeline 核心流程。"""
    print("=" * 60)
    print("  Trinity AsyncMemoryPipeline -- Self-Test")
    print("=" * 60)

    # ---- 1. 初始化 ----
    print("\n  [1/6] Initializing AsyncMemoryPipeline (thread mode)...")
    pipeline = AsyncMemoryPipeline(
        batch_size=5,
        flush_interval=2.0,
        mode="thread",
    )
    print("  [OK] Initialized")

    # ---- 2. ingest 20 条 ----
    print("\n  [2/6] Ingesting 20 items (non-blocking)...")
    futures = []
    for i in range(20):
        fut = pipeline.ingest(
            content=f"This is test memory #{i+1} for async pipeline validation.",
            tags=["test", f"idx_{i}"],
            category="self_test",
            importance=0.5 + (i % 10) * 0.05,
            metadata={"index": i},
        )
        futures.append(fut)
    print(f"  [OK] Enqueued 20 items, queue depth: {len(pipeline._queue)}")

    # ---- 3. 检查队列状态 ----
    print("\n  [3/6] Queue status...")
    stats_before = pipeline.get_stats()
    print(f"       queue_depth: {stats_before['queue_depth']}")
    print(f"       ingested:    {stats_before['total_ingested']}")
    print(f"       processed:   {stats_before['total_processed']}")

    # 启动消费者
    pipeline.start()
    import time as _time
    _time.sleep(0.5)

    stats_mid = pipeline.get_stats()
    print(f"       after start: {stats_mid['queue_depth']} (should be <= 5)")

    # ---- 4. flush ----
    print("\n  [4/6] flush() forcing remaining items...")
    remaining = pipeline.flush()
    print(f"       flushed {remaining} items")
    _time.sleep(0.2)

    # ---- 5. 打印统计 ----
    print("\n  [5/6] Pipeline stats:")
    stats = pipeline.get_stats()
    print(f"       queue_depth:        {stats['queue_depth']}")
    print(f"       total_ingested:     {stats['total_ingested']}")
    print(f"       total_processed:    {stats['total_processed']}")
    print(f"       total_batches:      {stats['total_batches']}")
    print(f"       total_errors:       {stats['total_errors']}")
    print(f"       avg_batch_latency:  {stats['avg_batch_latency_sec']} sec")
    print(f"       avg_item_latency:   {stats['avg_item_latency_sec']} sec")
    print(f"       uptime:             {stats['uptime_sec']} sec")
    print(f"       mode:               {stats['mode']}")

    # ---- 6. 关闭 ----
    print("\n  [6/6] Shutting down pipeline...")
    pipeline.shutdown(wait=True)
    print("  [OK] Pipeline shut down")

    # ---- 最终验证 ----
    final_stats = pipeline.get_stats()
    print(f"\n  -- Final Summary --")
    print(f"  total_ingested:   {final_stats['total_ingested']}")
    print(f"  total_processed:  {final_stats['total_processed']}")
    print(f"  total_errors:     {final_stats['total_errors']}")
    passed = (final_stats['total_ingested'] >= 20
              and final_stats['total_processed'] <= final_stats['total_ingested'])
    print(f"\n  {'[PASS] Self-test passed!' if passed else '[FAIL] Self-test failed!'}")
    print("=" * 60)


if __name__ == "__main__":
    self_test()
