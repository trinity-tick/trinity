"""Trinity — ANN 索引预热回归测试（2026-08-15 压测优化）。

覆盖 MemoryAggregator 的启动期 ANN 预热两条路径：
1. 预热完成后 ingest：_add_to_index 实时入索引（ready-guard 放行）
2. 冷启动窗口 ingest（sklearn fit 未完成前写入）：_prewarm_ann_index
   在 embedding ready 后全量 _rebuild_index() 补建 —— 首次检索不再冷启动

背景：生产压测曾现 read p99 偶发 2.4s 冷启动尾巴；预热线程 + ready-guard
修复后，冷启动窗口的 ingest 必须由预热线程补建（验证失败 → 索引为 0）。
"""

from __future__ import annotations

import time

import pytest

from trinity.agents.aggregator import MemoryAggregator


@pytest.fixture()
def agg() -> MemoryAggregator:
    a = MemoryAggregator(persist_path=None)
    yield a
    a.shutdown()


def _wait_index(agg: MemoryAggregator, target: int, timeout_s: float = 30.0) -> int:
    """轮询等待 ANN 索引条数达到 target（给后台预热线程时间）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if len(agg._index_id_map) >= target:
            break
        time.sleep(0.2)
    return len(agg._index_id_map)


def test_prewarm_after_ready_indexes_live(agg: MemoryAggregator) -> None:
    """预热完成后 ingest：每条实时进索引，无需等 rebuild。"""
    # 等 embedding 预热完成（sklearn fit ~1-10s）
    assert agg._embedding_ready.wait(timeout=60), "embedding 预热超时"
    for i in range(10):
        agg.ingest(f"预热后独特记忆编号{i} 主题{i * 7}", "eng",
                   {"category": "db"})
    idx = _wait_index(agg, 10)
    assert idx == 10, f"预热后 ingest 应实时入索引，实际 {idx}"


def test_cold_start_ingest_rebuilt_by_prewarm(agg: MemoryAggregator) -> None:
    """冷启动窗口 ingest（fit 完成前写入）：预热线程等 ready 后补建索引。

    这是生产启动期的真实时序：服务一起就写入，fit 尚未完成，
    _add_to_index 被 ready-guard 跳过 → 必须由 _prewarm_ann_index 全量补建。
    """
    # 立即 ingest（不等待预热）——模拟 fit 完成前的写入窗口
    for i in range(15):
        agg.ingest(f"冷启动窗口第{i:02d}条 独特词根w{i * 13}", "eng",
                   {"category": "db"})
    # 等待 embedding ready + 预热线程 rebuild 完成
    assert agg._embedding_ready.wait(timeout=60), "embedding 预热超时"
    idx = _wait_index(agg, 15, timeout_s=30)
    assert idx == 15, f"冷启动窗口 ingest 应由预热线程补建，实际 {idx}"


def test_first_search_not_cold_after_prewarm(agg: MemoryAggregator) -> None:
    """预热完成后首次检索应命中索引（不再走空索引冷路径）。"""
    assert agg._embedding_ready.wait(timeout=60), "embedding 预热超时"
    for i in range(12):
        agg.ingest(f"检索回归记忆{i} 冷启动消除", "eng", {"category": "db"})
    assert _wait_index(agg, 12) >= 12
    t0 = time.time()
    results = agg.query({}, limit=5, mode="hybrid",
                        query_text="检索回归 冷启动")
    elapsed = time.time() - t0
    assert len(results) >= 1, "预热后首次检索应返回结果"
    assert elapsed < 5.0, f"首次检索不应冷启动挂起，耗时 {elapsed:.2f}s"
