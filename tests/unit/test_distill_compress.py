"""Trinity — R4 结构化蒸馏压缩单元测试（2026-08-15）。

覆盖：MemoryCompressor.distill_compress——
- 记忆批 → 结构化蒸馏复合对象 → 摘要文本
- 摘要包含 Intent/Summary 字段
- 空批/失败返回 None（回退安全）
"""

from __future__ import annotations

import pytest

from trinity.daemon.memory_compressor import MemoryCompressor


@pytest.fixture()
def compressor() -> MemoryCompressor:
    return MemoryCompressor(llm_callable=None, pg_adapter=None)


def _memories() -> list:
    return [
        {"memory_id": "m1", "content": "用户问：数据库选型建议？助手答：PostgreSQL 适合生产",
         "role": "user"},
        {"memory_id": "m2", "content": "用户确认使用 PostgreSQL，要求 JSONB 存储",
         "role": "assistant"},
        {"memory_id": "m3", "content": "团队决定迁移到 PostgreSQL，改用 JSONB",
         "role": "system"},
        {"memory_id": "m4", "content": "迁移完成，JSONB 性能验证通过",
         "role": "assistant"},
    ]


def test_distill_compress_produces_summary(compressor: MemoryCompressor) -> None:
    s = compressor.distill_compress(_memories())
    assert s is not None
    assert "Summary" in s or "Intent" in s


def test_distill_compress_empty(compressor: MemoryCompressor) -> None:
    assert compressor.distill_compress([]) is None


def test_distill_compress_single(compressor: MemoryCompressor) -> None:
    s = compressor.distill_compress(_memories()[:1])
    assert s is not None  # 单条也可蒸馏（含 Intent）


def test_distill_compress_invalid(compressor: MemoryCompressor) -> None:
    """无 content 的输入不崩溃，返回 None 或摘要均可。"""
    s = compressor.distill_compress([{"memory_id": "a"}, {"memory_id": "b"}])
    assert s is None or isinstance(s, str)
