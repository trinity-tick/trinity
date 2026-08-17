"""Trinity — R3 P0-1c 意图聚类压缩单元测试（2026-08-15）。

覆盖：MemoryCompressor.intent_cluster_batch——
- 同类意图记忆聚类为子批
- 小批/失败回退原样（不破坏既有压缩链）
"""

from __future__ import annotations

import pytest

from trinity.daemon.memory_compressor import MemoryCompressor


@pytest.fixture()
def compressor() -> MemoryCompressor:
    return MemoryCompressor(llm_callable=None, pg_adapter=None)


def _memories() -> list:
    return [
        {"memory_id": "m1", "content": "用户设置暗色模式主题偏好，减少眼睛疲劳",
         "category": "preference", "importance": 0.6},
        {"memory_id": "m2", "content": "用户喜欢深色界面，夜间模式开启",
         "category": "preference", "importance": 0.5},
        {"memory_id": "m3", "content": "数据库迁移到 PostgreSQL 使用 JSONB 存储",
         "category": "ops", "importance": 0.7},
        {"memory_id": "m4", "content": "PostgreSQL JSONB 性能优化完成",
         "category": "ops", "importance": 0.6},
        {"memory_id": "m5", "content": "记忆市场挂单支持分片交易",
         "category": "market", "importance": 0.4},
        {"memory_id": "m6", "content": "TrustExchange 订单簿实现完成",
         "category": "market", "importance": 0.5},
        {"memory_id": "m7", "content": "网关限流配置调整",
         "category": "ops", "importance": 0.3},
    ]


def test_intent_cluster_splits(compressor: MemoryCompressor) -> None:
    sub = compressor.intent_cluster_batch(_memories(), min_cluster=2)
    assert 1 < len(sub) <= 7
    total = sum(len(b) for b in sub)
    assert total == 7  # 不丢记忆


def test_intent_cluster_small_batch_unchanged(compressor: MemoryCompressor) -> None:
    mems = _memories()[:3]
    sub = compressor.intent_cluster_batch(mems, min_cluster=2)
    assert len(sub) >= 1
    assert sum(len(b) for b in sub) == 3


def test_intent_cluster_single_memory(compressor: MemoryCompressor) -> None:
    mems = [{"memory_id": "x", "content": "single", "category": "g"}]
    sub = compressor.intent_cluster_batch(mems, min_cluster=2)
    assert sub == [mems]  # 单条原样


def test_intent_cluster_invalid_memories(compressor: MemoryCompressor) -> None:
    """无 content/异常输入不崩溃，回退原样。"""
    mems = [{"memory_id": "a"}, {"memory_id": "b"}, {"memory_id": "c"},
            {"memory_id": "d"}, {"memory_id": "e"}]
    sub = compressor.intent_cluster_batch(mems, min_cluster=2)
    assert isinstance(sub, list)
    assert sum(len(b) for b in sub) == 5
