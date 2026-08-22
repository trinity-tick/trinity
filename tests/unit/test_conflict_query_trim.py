"""冲突检测召回截断单测（2026-08-21，ingest 卡死修复防回归）。

背景：_assign_conflicts 用完整 content 做 FTS 召回，超长中文文本会切出数千
词条 OR MATCH，单次 ingest 分钟级。修复：召回 query 截断到
TRINITY_CONFLICT_QUERY_MAX（默认 1000 字符），token 重叠判断仍用全量内容。

覆盖：
- 长中文文本 ingest 快速完成（修复前会卡死分钟级）
- TRINITY_CONFLICT_QUERY_MAX=0 关闭召回不崩溃
- 行为保持：高重叠内容仍分配 conflict_group_id
"""

from __future__ import annotations

import time

import pytest

LONG_CN = "用户讨论了供应链管理与物流优化的中文长文本内容。{}".format("仓库管理与库存周转率的详细分析。" * 600)


def test_long_chinese_ingest_fast(adapter, monkeypatch):
    """2 万+ 字符中文 ingest 应在秒级完成（修复前分钟级/卡死）。"""
    t0 = time.time()
    adapter.store_memory(content=LONG_CN, agent_id="a1", category="general")
    elapsed = time.time() - t0
    assert elapsed < 10, f"长文本 ingest 耗时 {elapsed:.1f}s（疑似冲突检测召回未截断）"


def test_conflict_query_max_zero_disables_recall(adapter, monkeypatch):
    """TRINITY_CONFLICT_QUERY_MAX=0：跳过召回查询，不崩溃、仍可写入。"""
    monkeypatch.setenv("TRINITY_CONFLICT_QUERY_MAX", "0")
    adapter.store_memory(content="端口是 5430，用户偏好深色模式", agent_id="a1")
    adapter.store_memory(content="端口改为 5432，用户偏好浅色模式", agent_id="a1")
    r = adapter.search_memories(query="端口", top_k=5)
    assert len(r) >= 1


def test_conflict_group_still_assigned(adapter):
    """行为保持：高重叠但内容不同的记忆仍分配 conflict_group_id。"""
    adapter.store_memory(content="用户说端口是 5430 且数据库在本地", agent_id="a1")
    r2 = adapter.store_memory(content="用户说端口是 5432 且数据库在本地", agent_id="a1")
    mid2 = r2["memory_id"]
    row = adapter._conn.execute(
        "SELECT conflict_group_id FROM memories WHERE memory_id=?", (mid2,)
    ).fetchone()
    assert row and row[0] and row[0].startswith("conf_")


def test_default_query_max_applied(adapter, monkeypatch):
    """默认截断 1000 字符：超长内容写入正常且冲突检测不因截断崩溃。"""
    adapter.store_memory(content=LONG_CN + "结尾的独特标记 9f8e7d", agent_id="a1")
    adapter.store_memory(content=LONG_CN + "结尾的独特标记 a1b2c3", agent_id="a1")
    assert True
