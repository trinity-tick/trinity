"""Trinity — ANN 索引预热回归测试（2026-08-15 压测优化）。

覆盖 MemoryAggregator 的启动期 ANN 预热两条路径：
1. 预热完成后 ingest：_add_to_index 实时入索引（ready-guard 放行）
2. 冷启动窗口 ingest（sklearn fit 未完成前写入）：_prewarm_ann_index
   在 embedding ready 后全量 _rebuild_index() 补建 —— 首次检索不再冷启动

背景：生产压测曾现 read p99 偶发 2.4s 冷启动尾巴；预热线程 + ready-guard
修复后，冷启动窗口的 ingest 必须由预热线程补建（验证失败 → 索引为 0）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
2026-08-21: 追加 `retrieval/ann_index.py` 的 ANNIndex 预热状态机回归：
  1. 初始未 warm → is_warm=False（调用方应走降级通道）
  2. load 落盘索引后即 warm（毫秒级）
  3. 无盘 → 后台构建完成后 warm（注入快速假 build 避免真实耗时）
  4. TRINITY_ANN_PREWARM=off → startup_prewarm 空转、行为不变
  5. 损坏索引文件 load 容错（静默降级不崩）
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

from trinity.agents.aggregator import MemoryAggregator
from trinity.retrieval.ann_index import ANNIndex


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


# ═══════════════════════════════════════════════════════════════════════
# ANNIndex 预热状态机（retrieval/ann_index.py，2026-08-21）
# 使用 numpy 后端（hnswlib/faiss 缺失时默认 numpy），确定性、毫秒级。
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture()
def ann_idx() -> ANNIndex:
    return ANNIndex(dim=16, space="cosine", max_elements=1000, M=8, ef_construction=50)


def _vec(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(16).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-12)


def _wait_warm(idx: ANNIndex, timeout_s: float = 10.0) -> bool:
    """轮询等待索引 warm 置位（给后台预热线程时间）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if idx.is_warm:
            return True
        time.sleep(0.02)
    return idx.is_warm


def test_initial_not_warm(ann_idx: ANNIndex) -> None:
    """初始状态：未 warm → is_warm=False（调用方据此走降级通道）。"""
    assert ann_idx.is_warm is False
    assert ann_idx.prewarming is False
    # 未 warm 的索引不阻塞：search 返回空（无损降级）
    assert ann_idx.search(_vec(1), k=5) == []


def test_load_disk_index_is_warm(ann_idx: ANNIndex, tmp_path) -> None:
    """落盘索引 load 后即 warm（毫秒级，免重建等待）。"""
    ids = [f"m_{i}" for i in range(6)]
    vecs = [_vec(i) for i in range(6)]
    ann_idx.add_vectors(ids, vecs)
    path = str(tmp_path / "ann_index.bin")
    ann_idx.save(path)

    cold = ANNIndex(dim=16)
    assert cold.is_warm is False
    t0 = time.time()
    cold.load(path)
    assert cold.is_warm is True
    assert (time.time() - t0) < 5.0, "load 应为毫秒级, 不应重建"
    assert cold.size == 6


def test_background_build_becomes_warm(ann_idx: ANNIndex, tmp_path) -> None:
    """无盘 → 后台构建完成后 warm（注入快速假 build，避免真实 embed 耗时）。"""
    path = str(tmp_path / "nonexistent.bin")  # 无盘

    def fake_build():
        ids = [f"b_{i}" for i in range(5)]
        vecs = [_vec(i) for i in range(5)]
        return ids, vecs

    ann_idx.startup_prewarm(path, build_func=fake_build)
    # 立即返回（非阻塞）；短暂后 warm
    assert ann_idx.prewarming is True or ann_idx.is_warm is True
    assert _wait_warm(ann_idx), "后台构建完成后应 warm"
    assert ann_idx.size == 5


def test_prewarm_off_behavior_unchanged(ann_idx: ANNIndex, tmp_path) -> None:
    """TRINITY_ANN_PREWARM=off：startup_prewarm 空转，索引保持未 warm，等价现状。"""
    path = str(tmp_path / "ann_index.bin")
    # 造一份落盘索引（即使有盘，off 时也完全不加载）
    warm_idx = ANNIndex(dim=16)
    warm_idx.add_vectors([f"m_{i}" for i in range(3)], [_vec(i) for i in range(3)])
    warm_idx.save(path)

    os.environ["TRINITY_ANN_PREWARM"] = "off"
    try:
        assert ann_idx.is_warm is False
        ann_idx.startup_prewarm(path)
        time.sleep(0.1)  # 空转应立即返回，不启动线程
        assert ann_idx.is_warm is False
        assert ann_idx.prewarming is False
        assert ann_idx.size == 0
    finally:
        os.environ.pop("TRINITY_ANN_PREWARM", None)


def test_corrupt_file_silent_degrade(ann_idx: ANNIndex, tmp_path) -> None:
    """损坏索引文件：load 容错不崩；startup_prewarm 静默降级不抛。"""
    path = str(tmp_path / "corrupt.bin")
    # 写坏 meta（非法 JSON）+ 坏 vec.npz（非 zip 字节）
    with open(path + ".meta.json", "w", encoding="utf-8") as f:
        f.write("{ not valid json !")
    with open(path + ".vec.npz", "wb") as f:
        f.write(b"\x00garbage-not-an-npz\xff")
    with open(path, "wb") as f:
        f.write(b"\x00corrupt-native-index\xff")

    # 直接 load：应抛或被容错，但绝不崩溃进程
    try:
        ann_idx.load(path)
    except Exception:
        pass  # 允许抛（错误传播给显式 load），但 startup_prewarm 必须吞掉

    # startup_prewarm：静默降级（无 build_func → 保持未 warm，不抛）
    ann_idx.startup_prewarm(path)
    assert ann_idx.is_warm is False
    assert ann_idx.search(_vec(3), k=5) == []


def test_corrupt_file_falls_back_to_build(ann_idx: ANNIndex, tmp_path) -> None:
    """损坏索引文件 + 提供 build_func：load 失败后回退后台构建 → 最终 warm。"""
    path = str(tmp_path / "corrupt2.bin")
    with open(path + ".meta.json", "w", encoding="utf-8") as f:
        f.write("{ bad json")
    with open(path, "wb") as f:
        f.write(b"\x00garbage\xff")

    def fake_build():
        return [f"r_{i}" for i in range(4)], [_vec(i) for i in range(4)]

    ann_idx.startup_prewarm(path, build_func=fake_build)
    assert _wait_warm(ann_idx), "损坏文件应回退 build 并 warm"
    assert ann_idx.size == 4


def test_prewarm_idempotent_no_duplicate_thread(ann_idx: ANNIndex, tmp_path) -> None:
    """重复调用 startup_prewarm：只启动一次构建（防重复 GIL 竞争）。"""
    path = str(tmp_path / "empty.bin")  # 无盘
    calls = {"n": 0}

    def fake_build():
        calls["n"] += 1
        return [], []  # 空构建，仅计数

    for _ in range(5):
        ann_idx.startup_prewarm(path, build_func=fake_build)
    time.sleep(0.05)
    assert calls["n"] <= 1, f"重复预热应只启动一次构建, 实际 {calls['n']}"
