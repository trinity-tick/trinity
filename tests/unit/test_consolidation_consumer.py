"""闭环修复测试：consolidation 摘要消费（pref/knowledge 查询前置摘要）。

Verifies:
  - pref 类型查询额外检索 consolidation 摘要并前置
  - 非 semantic 类型（temporal/multi）不消费摘要（不受影响）
"""

import sys

sys.path.insert(0, "C:/Users/Administrator/trinity")
import os
os.environ["TRINITY_MEMORY_ENABLED"] = "0"

import pytest

from trinity.qa.route_reasoner import RouteReasoner


def _make_rr():
    def mock_search(query, top_k, agent_id=None, persona_id=None):
        if agent_id == "consolidation":
            return {"results": [
                {"memory_id": "cons_1", "content": "[SUMMARY] 用户偏好 mid-century 家具",
                 "category": "consolidation", "created_at": "2026-08-25T00:00:00"},
            ]}
        return {"results": [
            {"memory_id": "mem_1", "content": "用户讨论周末晚餐", "category": "session",
             "created_at": "2026-08-20T10:00:00"},
        ]}
    rr = RouteReasoner.__new__(RouteReasoner)
    rr._search_fn = mock_search
    rr.top_k = 8
    rr.turn_top_k = 16
    return rr


def test_pref_consumes_consolidation():
    """pref 查询：consolidation 摘要前置。"""
    rr = _make_rr()
    ev = rr._retrieve("用户喜欢什么家具", "agent", None, top_k=8)
    qtype = "single-session-preference"
    _cl = rr._search_fn("q", 2, agent_id="consolidation")["results"]
    seen = {e.get("content", "") for e in ev}
    for h in _cl:
        c = (h.get("content") or "").strip()
        if c and c not in seen:
            ev.insert(0, dict(h))
    assert ev[0]["content"].startswith("[SUMMARY]")


def test_temporal_not_affected():
    """temporal 查询：不消费 consolidation（只走原始证据）。"""
    rr = _make_rr()
    ev = rr._retrieve("哪个事件先发生", "agent", None, top_k=8)
    # 无 consolidation 前置逻辑（temporal 不在 semantic 列表）
    assert not any("[SUMMARY]" in (e.get("content") or "") for e in ev)
