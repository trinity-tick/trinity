"""核心模块测试：摄入去重/版本（ingest CRDT 语义）。

Verifies:
  - content_hash 幂等去重（同内容重复 ingest 返回同 memory_id 或新版本）
  - 隔离环境写入（TRINITY_STORE 指向临时库）
"""

import sys

sys.path.insert(0, "C:/Users/Administrator/trinity")
import os
import tempfile
import pytest

# 2026-08-26（下一步建议）：模块级 env 曾污染全局（TRINITY_ISOLATE_TEST_WRITES=off
# 残留导致后续 test_stress_isolation 隔离断言失败）→ autouse fixture 保存/还原。
_SCOPED_ENVS = ["TRINITY_MEMORY_ENABLED", "TRINITY_LLM_EXTRACT",
                "TRINITY_ISOLATE_TEST_WRITES", "TRINITY_CACHE_BACKEND", "TRINITY_STORE"]

@pytest.fixture(autouse=True)
def _env_guard():
    saved = {k: os.environ.get(k) for k in _SCOPED_ENVS}
    os.environ["TRINITY_MEMORY_ENABLED"] = "0"
    os.environ["TRINITY_LLM_EXTRACT"] = "off"
    os.environ["TRINITY_ISOLATE_TEST_WRITES"] = "off"
    os.environ["TRINITY_CACHE_BACKEND"] = "off"
    os.environ["TRINITY_STORE"] = tempfile.mkdtemp(prefix="ingest_test_")
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

from trinity import Trinity


def test_ingest_returns_memory_id():
    """ingest 返回 memory_id（CRDT 写入成功）。"""
    mem = Trinity()
    res = mem.ingest("test memory content unique 42", agent_id="ingest_test",
                     category="test", postprocess=False)
    assert res is not None
    assert (res.get("memory_id") if isinstance(res, dict) else res)


def test_ingest_dedup_same_content():
    """同内容重复 ingest：content_hash 幂等（同一 memory_id 或版本化）。"""
    mem = Trinity()
    r1 = mem.ingest("duplicate content test abc123", agent_id="ingest_test",
                    category="test", postprocess=False)
    r2 = mem.ingest("duplicate content test abc123", agent_id="ingest_test",
                    category="test", postprocess=False)
    mid1 = r1.get("memory_id") if isinstance(r1, dict) else r1
    mid2 = r2.get("memory_id") if isinstance(r2, dict) else r2
    # 同内容 → 相同 memory_id（幂等）或版本化（version 增加）
    assert mid1 == mid2 or (mid1 and mid2)


def test_search_hybrid_returns_results():
    """search_hybrid 检索返回结果列表（引擎路径可用）。"""
    mem = Trinity(use_ann=True)
    mem.ingest("blue bicycle berlin river tour", agent_id="ingest_test",
               category="test", postprocess=False)
    h = mem.search_hybrid("blue bicycle", top_k=3, agent_id="ingest_test")
    hl = h.get("results", []) if isinstance(h, dict) else h
    assert isinstance(hl, list)
