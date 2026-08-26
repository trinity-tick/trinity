"""R6: doc 分层隔离 + 证据/置信度标注 (OPTIMIZATION_ANALYSIS_ROUND6).

Verifies:
  - search_memories 默认排除 doc:*（include_docs=False）
  - include_docs=True 包含 doc 内容
  - Trinity.search include_docs 透传
  - _enrich_evidence: 结果附 evidence（category/version_count/audit_available）
    与 confidence；弱证据标注 verify_hint
"""

import pytest

from trinity.adapters.sqlite import SQLiteAdapter
from trinity.core.client import Trinity


@pytest.fixture()
def adapter(tmp_path):
    a = SQLiteAdapter(db_path=str(tmp_path / "t.db"))
    a.connect()
    yield a
    a.disconnect()


def _seed(adapter):
    """写入一条交互记忆 + 一条 doc 记忆。"""
    m1 = adapter.store_memory(content="用户偏好暗色模式", persona_id="p", agent_id="a",
                              tags=["preference"], category="general")
    m2 = adapter.store_memory(content="WMS 技术栈文档说明", persona_id="p", agent_id="a",
                              tags=["doc"], category="doc:general")
    return m1["memory_id"], m2["memory_id"]


def test_search_excludes_docs_by_default(adapter):
    m1, m2 = _seed(adapter)
    hits = adapter.search_memories("WMS", top_k=10, touch=False)
    ids = {h["memory_id"] for h in hits}
    assert m2 not in ids  # doc 默认排除
    # 交互记忆仍可检索
    hits2 = adapter.search_memories("暗色", top_k=10, touch=False)
    assert m1 in {h["memory_id"] for h in hits2}


def test_search_include_docs(adapter):
    _, m2 = _seed(adapter)
    hits = adapter.search_memories("WMS", top_k=10, touch=False, include_docs=True)
    ids = {h["memory_id"] for h in hits}
    assert m2 in ids


def test_trinity_search_include_docs(tmp_path):
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
    mem.ingest("用户偏好暗色模式", tags=["preference"])
    # 直接通过 adapter 写 doc 类（ingest 不写 doc category）
    mem._adapter.store_memory(content="WMS 技术栈文档", persona_id="p", agent_id="a",
                              tags=["doc"], category="doc:general")
    r1 = mem.search("WMS", top_k=10)
    ids1 = {h.get("memory_id") for h in r1.get("results", [])}
    doc_id = [m["memory_id"] for m in mem._adapter.search_memories("技术栈文档", touch=False,
                                                                   include_docs=True)]
    assert all(d not in ids1 for d in doc_id)  # 默认排除
    r2 = mem.search("WMS", top_k=10, include_docs=True)
    ids2 = {h.get("memory_id") for h in r2.get("results", [])}
    assert any(d in ids2 for d in doc_id)  # include_docs 包含


def test_enrich_evidence_fields(tmp_path):
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
    r = mem.ingest("用户偏好暗色模式", tags=["preference"])
    mem.update_memory(r.get("memory_id", ""), new_content="用户偏好深色模式")
    res = mem.search("深色模式", top_k=5)
    hits = res.get("results", [])
    assert hits
    h = hits[0]
    assert "evidence" in h
    assert h["evidence"]["category"] == "general"
    assert h["evidence"]["version_count"] >= 1
    assert h["evidence"]["audit_available"] is True
    assert "confidence" in h
    assert 0.0 <= h["confidence"] <= 1.0


def test_enrich_weak_evidence_hint(tmp_path):
    """无版本、低 importance 的记忆 → verify_hint 标注。"""
    mem = Trinity(adapter="sqlite", store_path=str(tmp_path))
    mem.ingest("测试残留文本 ABC", importance=0.2, tags=["x"])
    res = mem.search("测试残留文本 ABC", top_k=5)
    hits = res.get("results", [])
    assert hits
    h = hits[0]
    assert h["confidence"] < 0.4
    assert "verify_hint" in h
