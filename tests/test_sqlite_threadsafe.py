"""Regression: SQLiteAdapter connection must be usable across threads.

曾发现：SQLiteAdapter.connect() 未设 check_same_thread=False，多线程服务
（HTTP 线程池 / A2A 跨进程演示）从工作线程调用 search/store 抛
"SQLite objects created in a thread can only be used in that same thread"。
修复：connect() 增加 check_same_thread=False。
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.adapters.sqlite import SQLiteAdapter


class TestSqliteThreadSafe:
    def test_search_from_worker_thread(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "threadsafe.db")
        adapter = SQLiteAdapter(db_path=db)
        adapter.connect()
        try:
            adapter.store_memory("thread safety test memory", persona_id="default")
            errors = []
            results = []

            def worker():
                try:
                    results.extend(adapter.search_memories("thread safety", top_k=5))
                except Exception as e:  # pragma: no cover
                    errors.append(str(e))

            threads = [threading.Thread(target=worker) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not errors, f"thread errors: {errors[:3]}"
            assert len(results) >= 1, "expected search results from worker threads"
        finally:
            adapter.disconnect()

    def test_store_from_worker_thread(self):
        tmp = tempfile.mkdtemp()
        adapter = SQLiteAdapter(db_path=os.path.join(tmp, "threadsafe2.db"))
        adapter.connect()
        try:
            errors = []

            def worker():
                try:
                    adapter.store_memory(f"from thread", persona_id="default")
                except Exception as e:  # pragma: no cover
                    errors.append(str(e))

            t = threading.Thread(target=worker)
            t.start()
            t.join(timeout=15)
            assert not errors, f"store from thread failed: {errors[:3]}"
            hits = adapter.search_memories("from thread", top_k=5)
            assert len(hits) >= 1
        finally:
            adapter.disconnect()

    def test_concurrent_search_no_cursor_corruption(self):
        """Regression (2026-08-15): 8-thread concurrent search_memories on one
        connection used to corrupt cursors → "bad parameter or other API misuse"
        and None scores. search_memories now holds _write_lock (single SQLite
        connection must serialize all statements)."""
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, "threadsafe3.db")
        adapter = SQLiteAdapter(db_path=db)
        adapter.connect()
        try:
            for i in range(30):
                adapter.store_memory(
                    f"并发检索回归测试记忆 {i} 数据库优化", persona_id="default")
            errors = []
            barrier = threading.Barrier(8)

            def worker(wid):
                barrier.wait()
                for j in range(25):
                    try:
                        adapter.search_memories("数据库", top_k=10)
                    except Exception as e:  # pragma: no cover
                        errors.append(f"w{wid}-{j}: {e}")

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)
            assert not errors, f"concurrent search errors: {errors[:5]}"
        finally:
            adapter.disconnect()
