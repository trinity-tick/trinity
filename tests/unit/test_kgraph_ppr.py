"""Trinity — R3 P0-1b kgraph PPR 增强单元测试（2026-08-15）。

覆盖：KnowledgeGraph.search 关键词 + PPR 图扩散——
- 关键词命中实体保留
- PPR 把图关联实体扩散进结果（对齐 HippoRAG 2 思路）
- 空图/无种子时回退纯关键词（不崩溃）
"""

from __future__ import annotations

import pytest

from trinity.kgraph.graph import KnowledgeGraph


@pytest.fixture()
def kg() -> KnowledgeGraph:
    g = KnowledgeGraph()
    g.add_entity("trinity_db", "system", {"name": "Trinity 数据库"})
    g.add_entity("sqlite", "backend", {"name": "SQLite FTS5"})
    g.add_entity("postgres", "backend", {"name": "PostgreSQL"})
    g.add_entity("ann_index", "system", {"name": "向量索引"})
    g.add_relation("trinity_db", "uses", "sqlite")
    g.add_relation("trinity_db", "uses", "postgres")
    g.add_relation("trinity_db", "uses", "ann_index")
    return g


def test_keyword_match_retained(kg: KnowledgeGraph) -> None:
    res = kg.search("Trinity", top_k=5)
    ids = [r["entity"]["id"] for r in res]
    assert "trinity_db" in ids  # 关键词命中


def test_ppr_diffuses_related(kg: KnowledgeGraph) -> None:
    """关键词命中的实体其图关联实体被 PPR 扩散进结果。"""
    res = kg.search("Trinity", top_k=10)
    ids = [r["entity"]["id"] for r in res]
    # sqlite/postgres/ann_index 都通过 trinity_db 的 uses 边被扩散
    assert "sqlite" in ids
    assert "postgres" in ids
    assert "ann_index" in ids


def test_ppr_score_present_on_diffused(kg: KnowledgeGraph) -> None:
    res = kg.search("Trinity", top_k=10)
    diffused = [r for r in res if r["ppr_score"] is not None]
    assert len(diffused) >= 1  # 至少一个图扩散实体带 ppr 分数


def test_empty_graph_no_crash() -> None:
    g = KnowledgeGraph()
    res = g.search("anything", top_k=5)
    assert isinstance(res, list)


def test_no_seed_fallback_keyword(kg: KnowledgeGraph) -> None:
    """无关键词命中种子时仍返回（可能为空但不崩溃）。"""
    res = kg.search("完全不存在的词xyz", top_k=5)
    assert isinstance(res, list)
