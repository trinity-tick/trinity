"""Trinity — R2 优化单元测试（LLM 事实抽取 + edge bi-temporal, 2026-08-15）。

覆盖：
- er_extractor 双参/单参 LLM callable 兼容
- client.ingest 写路径 LLM 事实抽取（mock LLM）→ 实体+语义关系入库
- create_relation valid_from/valid_to + query_relations_at 时点查询
- relations 表时序列迁移（幂等）
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from trinity.adapters.sqlite import SQLiteAdapter
from trinity.memory.er_extractor import EntityRelationExtractor


# ── er_extractor LLM 兼容 ──────────────────────────────────────────

class _FakeLLM:
    """mock LLM：返回固定 JSON（双参 (system, user) 签名）。"""

    def __call__(self, system: str, user: str) -> str:
        return json.dumps({
            "entities": [
                {"name": "Alice", "type": "person"},
                {"name": "Bob", "type": "person"},
                {"name": "Trinity", "type": "project"},
            ],
            "relations": [
                {"subject": "Alice", "predicate": "works_on", "object": "Trinity"},
                {"subject": "Bob", "predicate": "works_on", "object": "Trinity"},
            ],
        })


class _FakeLLMSingle:
    """单参 (prompt) 签名 mock。"""

    def __call__(self, prompt: str) -> str:
        return json.dumps({
            "entities": [{"name": "Alice", "type": "person"}],
            "relations": [],
        })


def test_er_extractor_dual_arg_llm(tmp_path) -> None:
    a = SQLiteAdapter(str(tmp_path / "t.db"))
    a.connect()
    r = a.store_memory(content="Alice 和 Bob 在 Trinity 项目上协作", persona_id="p")
    ex = EntityRelationExtractor(a, llm_call=_FakeLLM())
    out = ex.extract_from_memories([r["memory_id"]])
    assert out["entities_added"] >= 3
    assert out["relations_added"] >= 2
    rels = a.query_relations()
    assert any(x["predicate"] == "works_on" for x in rels)
    a.disconnect()


def test_er_extractor_single_arg_llm(tmp_path) -> None:
    a = SQLiteAdapter(str(tmp_path / "t.db"))
    a.connect()
    r = a.store_memory(content="Alice 单独工作", persona_id="p")
    ex = EntityRelationExtractor(a, llm_call=_FakeLLMSingle())
    out = ex.extract_from_memories([r["memory_id"]])
    assert out["entities_added"] >= 1
    a.disconnect()


def test_er_extractor_llm_fallback_regex(tmp_path) -> None:
    """LLM 返回非法 JSON → 回退正则提取（不崩溃）。"""
    class _Bad:
        def __call__(self, *a, **k):
            raise RuntimeError("llm down")
    a = SQLiteAdapter(str(tmp_path / "t.db"))
    a.connect()
    r = a.store_memory(content="Alice works on Trinity and Bob works on Trinity", persona_id="p")
    ex = EntityRelationExtractor(a, llm_call=_Bad())
    out = ex.extract_from_memories([r["memory_id"]])  # 不应抛异常
    assert isinstance(out, dict)
    a.disconnect()


# ── client 写路径 LLM 事实抽取 ─────────────────────────────────────

def test_ingest_llm_extract_write_path(tmp_path, monkeypatch) -> None:
    """TRINITY_LLM_EXTRACT=on 时 ingest 走 LLM 抽取：实体+关系入库。

    2026-08-17 修复: LLM 抽取默认异步(2026-08-16 起,真实 LLM ~4.5s/条,
    同步会阻塞写路径);测试期望返回时实体/关系已入库,须设
    TRINITY_LLM_EXTRACT_SYNC=on 强制同步(见 client.ingest 注释)。
    """
    from trinity.core.client import Trinity
    monkeypatch.setenv("TRINITY_LLM_EXTRACT", "on")
    monkeypatch.setenv("TRINITY_LLM_EXTRACT_SYNC", "on")
    # monkeypatch create_llm_compress_callable → 返回固定 LLM
    import trinity.daemon.memory_compressor as mc
    monkeypatch.setattr(mc, "create_llm_compress_callable", lambda **k: _FakeLLM())
    db = str(tmp_path / "w.db")
    t = Trinity(store_path=db)
    r = t.ingest("Alice 和 Bob 在 Trinity 项目上协作", persona_id="p")
    assert r.get("extracted_entities", 0) >= 3
    a = t._adapter
    rels = a.query_relations()
    assert any(x["predicate"] == "works_on" for x in rels)
    a.disconnect()


def test_ingest_rule_fallback_without_switch(tmp_path, monkeypatch) -> None:
    """开关未开 → 规则提取路径（不崩溃，返回 entity 列表）。"""
    from trinity.core.client import Trinity
    monkeypatch.delenv("TRINITY_LLM_EXTRACT", raising=False)
    db = str(tmp_path / "w2.db")
    t = Trinity(store_path=db)
    r = t.ingest("Alice works on Trinity project", persona_id="p")
    # 规则提取对英文大写词应能提取（ACRONYM/CamelCase）
    assert "extracted_entities" in r
    a = t._adapter
    a.disconnect()


# ── edge bi-temporal ───────────────────────────────────────────────

@pytest.fixture()
def adapter(tmp_path):
    a = SQLiteAdapter(str(tmp_path / "bt.db"))
    a.connect()
    yield a
    a.disconnect()


def test_relations_temporal_columns(adapter) -> None:
    cols = [c[1] for c in adapter._conn.execute("PRAGMA table_info(relations)").fetchall()]
    assert "valid_from" in cols
    assert "valid_to" in cols


def test_create_relation_with_validity(adapter) -> None:
    e1 = adapter.upsert_entity("X", "project", {})
    e2 = adapter.upsert_entity("Alice", "person", {})
    now = datetime.now(timezone.utc)
    r = adapter.create_relation(
        e1["id"], "works_on", e2["id"],
        valid_from=(now - timedelta(days=10)).isoformat(),
        valid_to=(now + timedelta(days=10)).isoformat(),
    )
    assert r.get("valid_from") is not None
    assert r.get("valid_to") is not None
    row = adapter._conn.execute(
        "SELECT valid_from, valid_to FROM relations WHERE id = ?", (r["id"],)
    ).fetchone()
    assert row["valid_from"] is not None and row["valid_to"] is not None


def test_query_relations_at_excludes_expired(adapter) -> None:
    e1 = adapter.upsert_entity("X", "project", {})
    e2 = adapter.upsert_entity("Alice", "person", {})
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=30)).isoformat()
    future = (now + timedelta(days=30)).isoformat()
    expired = (now - timedelta(days=10)).isoformat()
    adapter.create_relation(e1["id"], "active_edge", e2["id"], valid_from=past, valid_to=future)
    adapter.create_relation(e1["id"], "expired_edge", e2["id"], valid_from=past, valid_to=expired)

    now_iso = now.isoformat()
    active = adapter.query_relations_at(now_iso, subject_id=e1["id"])
    preds = [r["predicate"] for r in active]
    assert "active_edge" in preds
    assert "expired_edge" not in preds

    # 15 天前两条都有效
    back = adapter.query_relations_at((now - timedelta(days=15)).isoformat(), subject_id=e1["id"])
    preds_back = [r["predicate"] for r in back]
    assert "expired_edge" in preds_back
    assert "active_edge" in preds_back


def test_query_relations_at_no_ttl_default_now(adapter) -> None:
    e1 = adapter.upsert_entity("X", "project", {})
    e2 = adapter.upsert_entity("Alice", "person", {})
    adapter.create_relation(e1["id"], "no_ttl_edge", e2["id"])
    later = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
    res = adapter.query_relations_at(later, subject_id=e1["id"])
    assert any(r["predicate"] == "no_ttl_edge" for r in res)


def test_query_relations_backward_compat(adapter) -> None:
    """原 query_relations 仍可用（过滤 subject/predicate/object）。"""
    e1 = adapter.upsert_entity("X", "project", {})
    e2 = adapter.upsert_entity("Alice", "person", {})
    adapter.create_relation(e1["id"], "works_on", e2["id"])
    res = adapter.query_relations(subject_id=e1["id"])
    assert len(res) == 1
    res2 = adapter.query_relations(predicate="works_on")
    assert len(res2) == 1
