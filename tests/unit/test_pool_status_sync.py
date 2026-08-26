"""P0-1: 聚合池 status 同步与检索口径统一 (COMPARISON_VS_2026_SOTA_R8).

Verifies:
  - DimensionVector source_status 字段序列化/反序列化往返
  - aggregator.query() 默认过滤 archived/deleted（include_archived=False）
  - include_archived=True 时返回全部
  - API /agents/memory/search include_archived 参数接线
"""

import pytest

from trinity.agents.dimensions import DimensionVector
from trinity.agents.aggregator import MemoryAggregator


def test_dimension_vector_source_status_roundtrip():
    dv = DimensionVector(
        memory_id="m1", content="测试", source_status="archived",
    )
    d = dv.to_dict(full=True)
    assert d["source_status"] == "archived"
    dv2 = DimensionVector.from_dict(d)
    assert dv2.source_status == "archived"


def test_dimension_vector_source_status_default_none():
    dv = DimensionVector(memory_id="m1", content="测试")
    assert dv.source_status is None
    d = dv.to_dict(full=True)
    assert d["source_status"] is None
    dv2 = DimensionVector.from_dict(d)
    assert dv2.source_status is None


def _pool_with_statuses():
    agg = MemoryAggregator()
    # 走完整 index_memory 路径（自动提取 topics、填充 topic 索引），
    # 再注入 source_status——模拟 sync 脚本行为。
    specs = [
        ("ma", "active memory about hiking", "active"),
        ("mb", "archived memory about hiking", "archived"),
        ("mc", "legacy memory about hiking", None),
    ]
    for mid, content, status in specs:
        dv = agg._engine.index_memory(content, "db-sync", {"category": "general"})
        dv.memory_id = mid
        dv.source_status = status
        agg._pool[mid] = dv
    return agg


def test_query_excludes_archived_by_default():
    agg = _pool_with_statuses()
    results = agg.query({"category": "general"}, limit=10, mode="keyword")
    ids = {r.memory_id for r in results}
    assert "ma" in ids          # active 保留
    assert "mc" in ids          # None 视为 active（旧数据兼容）
    assert "mb" not in ids      # archived 被过滤


def test_query_include_archived_returns_all():
    agg = _pool_with_statuses()
    results = agg.query(
        {"category": "general"}, limit=10, mode="keyword",
        include_archived=True,
    )
    ids = {r.memory_id for r in results}
    assert ids == {"ma", "mb", "mc"}


def test_query_vector_path_filters_archived():
    agg = _pool_with_statuses()
    # vector 路径在无向量索引时回退 keyword；用空 query_text 直接验证过滤逻辑
    results = agg.query(
        {"category": "general"}, limit=10, mode="vector",
        query_text="", include_archived=False,
    )
    ids = {r.memory_id for r in results}
    assert "mb" not in ids


def test_api_include_archived_param():
    from fastapi.testclient import TestClient
    from trinity.api.server import app

    with TestClient(app) as client:
        r = client.get(
            "/agents/memory/search",
            params={"q": "test", "top_k": 5},
        )
        assert r.status_code == 200
        body = r.json()
        assert "results" in body
        r2 = client.get(
            "/agents/memory/search",
            params={"q": "test", "top_k": 5, "include_archived": "true"},
        )
        assert r2.status_code == 200


def test_pool_namespace_scope_isolation(monkeypatch, tmp_path):
    """聚合池命名空间 ACL（R5 P1-⑤ 确认已存在）：scope 写读隔离。

    scope=teamA 的记忆只被 scope=teamA 检索命中；teamB 不可见；
    无 scope 过滤时全量可见（共享池语义）。隔离 TRINITY_HOME 避免
    加载全局持久池（内容相似会触发 merge 干扰断言）。
    """
    import trinity.agents.aggregator as agg_mod
    monkeypatch.setenv("TRINITY_HOME", str(tmp_path))
    agg_mod.PERSIST_MAX_DIRTY = 10**9  # 防持久化写盘

    agg = MemoryAggregator()
    a = agg.ingest("team A 机密数据", "agent-a", {"scope": "teamA"})
    b = agg.ingest("team B 项目数据", "agent-b", {"scope": "teamB"})

    r_a = agg.query({"scope": "teamA"}, limit=10, mode="keyword")
    r_b = agg.query({"scope": "teamB"}, limit=10, mode="keyword")

    a_ids = {d.memory_id for d in r_a}
    b_ids = {d.memory_id for d in r_b}
    assert a.memory_id in a_ids
    assert b.memory_id in b_ids
    assert a.memory_id not in b_ids  # 命名空间隔离
    assert b.memory_id not in a_ids

    # 无 scope 过滤 = 共享池全量（a/b 均在池中）
    assert a.memory_id in agg._pool
    assert b.memory_id in agg._pool
    r_all = agg.query({}, limit=100, mode="keyword")
    all_ids = {d.memory_id for d in r_all}
    assert a.memory_id in all_ids and b.memory_id in all_ids
