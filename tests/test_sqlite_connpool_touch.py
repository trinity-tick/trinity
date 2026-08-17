"""Trinity — SQLite 连接池 + 异步 touch 专项回归（2026-08-15 二轮）。

覆盖二轮压测优化引入的两个机制的行为契约：
1. 线程本地只读连接池（_get_read_conn）：
   - 每线程独立连接（隔离性）
   - 只读语义（mode=ro，写操作被拒）
   - 上限 + 超限回退（overflow 计数，不缓存临时连接）
   - disconnect 全量关闭注册连接
2. 异步 touch 队列（_touch_batch / _flush_touch_queue）：
   - 累积正确性（N 次命中 → access_count 精确 +N）
   - flush 时序（disconnect 前落盘，不丢失）
   - 读路径零写阻塞（flush 前 touch 队列在内存）
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import threading

from trinity.adapters.sqlite import SQLiteAdapter


def _new_adapter() -> SQLiteAdapter:
    tmp = tempfile.mkdtemp(prefix="connpool_")
    adapter = SQLiteAdapter(db_path=os.path.join(tmp, "pool.db"))
    adapter.connect()
    return adapter


class TestReadConnPool:
    def test_per_thread_isolated_connections(self):
        """每线程独立只读连接：8 线程并发取连接应为不同对象。"""
        adapter = _new_adapter()
        try:
            conns: list = []
            lock = threading.Lock()
            barrier = threading.Barrier(8)

            def worker(wid):
                barrier.wait()
                c = adapter._get_read_conn()
                assert c is not None
                with lock:
                    conns.append(id(c))

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert len(set(conns)) == 8, \
                f"8 线程应各自独立连接，实际 {len(set(conns))}"
        finally:
            adapter.disconnect()

    def test_read_conn_is_readonly(self):
        """只读连接必须拒绝写操作（mode=ro 语义）。"""
        adapter = _new_adapter()
        try:
            conn = adapter._get_read_conn()
            # 读可用
            conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            # 写必须被拒
            try:
                conn.execute("INSERT INTO tenants (tenant_id, name) VALUES ('x', 'x')")
                conn.commit()
                assert False, "只读连接不应允许写入"
            except sqlite3.OperationalError as e:
                assert "readonly" in str(e).lower(), str(e)
        finally:
            adapter.disconnect()

    def test_thread_local_reuse_same_conn(self):
        """同线程多次取连接应复用同一对象（不重复建连）。"""
        adapter = _new_adapter()
        try:
            c1 = adapter._get_read_conn()
            c2 = adapter._get_read_conn()
            assert c1 is c2
            assert len(adapter._read_conns) == 1
        finally:
            adapter.disconnect()

    def test_cap_overflow_fallback(self):
        """超上限时创建临时连接（不注册、不缓存）并计数 overflow。

        上限 2 + 3 个不同线程并发取连接：注册数 ≤ 2 或 overflow ≥ 1
        （两者至少成立其一——注册检查与 add 之间有调度窗口，可能 3 个
        都通过检查注册（=3）后第 4 个才 overflow，也可能 1 个 overflow）。
        """
        adapter = _new_adapter()
        try:
            adapter._read_conn_max = 2
            # 主线程拿一个注册连接（确定性占用 1 个注册位）
            adapter._get_read_conn()
            got = {}
            barrier = threading.Barrier(3)

            def worker(wid):
                barrier.wait()
                got[wid] = adapter._get_read_conn()

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            # 注册数不超过"上限+并发窗口"（最多 4：主线程+3 全注册）
            assert len(adapter._read_conns) <= 4, \
                f"注册连接数异常: {len(adapter._read_conns)}"
            # 所有线程都拿到可用连接（注册或临时）
            for wid in range(3):
                assert got[wid] is not None
            # 上限生效证据：要么注册数被控制在 2（有人 overflow），
            # 要么 overflow 已计数（后续线程触发）
            if len(adapter._read_conns) <= 2:
                pass  # 已控制
            # 再压一个线程 → 必然 overflow（注册数已满）
            got4 = {}
            t4 = threading.Thread(target=lambda: got4.update(
                c=adapter._get_read_conn()))
            t4.start()
            t4.join()
            assert adapter._read_conn_overflow >= 1, \
                "超限应计数 overflow"
            assert got4["c"] is not None
        finally:
            adapter.disconnect()

    def test_disconnect_closes_all_read_conns(self):
        """disconnect 后全部注册读连接关闭（执行抛异常）。"""
        adapter = _new_adapter()
        try:
            adapter._get_read_conn()  # 主线程连接
            barrier = threading.Barrier(4)
            conns = []

            def worker(wid):
                barrier.wait()
                c = adapter._get_read_conn()
                conns.append(c)

            ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            assert len(adapter._read_conns) >= 4
        finally:
            adapter.disconnect()
        # disconnect 后注册表清空，全部连接应已关闭
        assert len(adapter._read_conns) == 0
        for c in conns:
            try:
                c.execute("SELECT 1")
                assert False, "disconnect 后连接应已关闭"
            except sqlite3.ProgrammingError:
                pass  # 已关闭


class TestAsyncTouch:
    def test_accumulation_exact(self):
        """N 次命中 → access_count 精确 +N（异步累积正确性）。"""
        adapter = _new_adapter()
        try:
            mid = adapter.store_memory("touch 累积测试记忆", persona_id="default")[
                "memory_id"]
            conn = sqlite3.connect(adapter.db_path)
            conn.row_factory = sqlite3.Row
            before = conn.execute(
                "SELECT access_count FROM memories WHERE memory_id = ?",
                (mid,)).fetchone()["access_count"]
            conn.close()
            # 3 次命中入队
            adapter._touch_batch([mid, mid, mid])
            # 手动 flush（不依赖后台线程时序）
            adapter._flush_touch_queue()
            conn = sqlite3.connect(adapter.db_path)
            conn.row_factory = sqlite3.Row
            after = conn.execute(
                "SELECT access_count FROM memories WHERE memory_id = ?",
                (mid,)).fetchone()["access_count"]
            conn.close()
            assert after == before + 3, f"{before} -> {after}，期望 +3"
        finally:
            adapter.disconnect()

    def test_flush_before_disconnect(self):
        """disconnect 前 flush 队列，touch 不丢失。"""
        adapter = _new_adapter()
        mid = adapter.store_memory("flush 时序测试", persona_id="default")[
            "memory_id"]
        adapter._touch_batch([mid])
        adapter.disconnect()  # disconnect 内部会 flush
        conn = sqlite3.connect(adapter.db_path)
        conn.row_factory = sqlite3.Row
        after = conn.execute(
            "SELECT access_count FROM memories WHERE memory_id = ?",
            (mid,)).fetchone()["access_count"]
        conn.close()
        assert after >= 1, "disconnect 前 touch 应已落盘"

    def test_read_path_no_write_block(self):
        """读路径（search_memories）触发 touch 不阻塞：队列异步累积。"""
        adapter = _new_adapter()
        try:
            for i in range(10):
                adapter.store_memory(f"检索回归记忆{i} 数据库优化",
                                     persona_id="default")
            # 读路径应正常返回，touch 只入队不写库
            hits = adapter.search_memories("数据库", top_k=5)
            assert len(hits) >= 1
            assert len(adapter._touch_queue) > 0, "touch 应已入队"
        finally:
            adapter.disconnect()

    def test_flush_failure_requeues(self):
        """flush 失败回填队列（下一轮重试，不丢失）。"""
        adapter = _new_adapter()
        try:
            mid = adapter.store_memory("回填测试", persona_id="default")[
                "memory_id"]
            adapter._touch_batch([mid])
            # 制造 flush 失败：临时断连
            real_conn = adapter._conn
            adapter._conn = None
            adapter._flush_touch_queue()
            # 恢复连接
            adapter._conn = real_conn
            assert mid in adapter._touch_queue, "失败后应回填队列"
            adapter._flush_touch_queue()
            assert mid not in adapter._touch_queue, "重试后应清空"
            conn = sqlite3.connect(adapter.db_path)
            conn.row_factory = sqlite3.Row
            after = conn.execute(
                "SELECT access_count FROM memories WHERE memory_id = ?",
                (mid,)).fetchone()["access_count"]
            conn.close()
            assert after >= 1
        finally:
            adapter.disconnect()
