"""P1-7: prompt cache 前缀管理 (COMPARISON_VS_2026_SOTA_R8).

Verifies:
  - stable_prefix_messages: system 固定前缀 + tag 版本化 + 变体放 user 尾部
  - 同一 system 不同 user → 前缀字节级一致（可命中缓存）
  - tag 变更 → 前缀变化（缓存失效）
  - cache_hit_stats 解析 DeepSeek prompt_cache_hit_tokens
  - normalize_response 透出 cache 统计
  - RouteReasoner._chat 使用 stable_prefix_messages
"""

import json

import pytest

from trinity.llm.client import (
    cache_hit_stats,
    normalize_response,
    stable_prefix_messages,
)


def test_stable_prefix_messages_structure():
    msgs = stable_prefix_messages("SYSTEM PROMPT", "USER VARIANT", tag="trinity-qa-v1")
    assert msgs[0]["role"] == "system"
    assert msgs[0]["content"] == "[trinity-qa-v1]\nSYSTEM PROMPT"
    assert msgs[1] == {"role": "user", "content": "USER VARIANT"}


def test_prefix_stable_across_variants():
    m1 = stable_prefix_messages("SYS", "question one", tag="v1")
    m2 = stable_prefix_messages("SYS", "question two", tag="v1")
    # system 前缀字节级一致（缓存命中条件）
    assert m1[0] == m2[0]
    assert m1[1]["content"] != m2[1]["content"]


def test_tag_change_invalidates_prefix():
    m1 = stable_prefix_messages("SYS", "q", tag="v1")
    m2 = stable_prefix_messages("SYS", "q", tag="v2")
    assert m1[0]["content"] != m2[0]["content"]


def test_no_tag_prefix():
    msgs = stable_prefix_messages("SYS", "q", tag="")
    assert msgs[0]["content"] == "SYS"


def test_cache_hit_stats_deepseek_format():
    usage = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 60, "prompt_cache_miss_tokens": 40}
    s = cache_hit_stats(usage)
    assert s["cache_hit_tokens"] == 60
    assert s["cache_miss_tokens"] == 40
    assert s["prompt_tokens"] == 100
    assert s["cache_hit_rate_pct"] == 60.0


def test_cache_hit_stats_openai_cached_tokens():
    usage = {"prompt_tokens": 200, "cached_tokens": 150}
    s = cache_hit_stats(usage)
    assert s["cache_hit_tokens"] == 150
    assert s["cache_hit_rate_pct"] == 75.0


def test_cache_hit_stats_empty():
    s = cache_hit_stats({})
    assert s["cache_hit_tokens"] == 0
    assert s["cache_hit_rate_pct"] == 0.0


def test_normalize_response_includes_cache():
    body = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 10, "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 2},
    }
    r = normalize_response(body)
    assert r["cache"]["cache_hit_tokens"] == 8
    assert r["cache"]["cache_hit_rate_pct"] == 80.0


def test_route_reasoner_uses_stable_prefix():
    """RouteReasoner._chat 的 payload system 带 [trinity-qa-v1] 前缀。"""
    import trinity.qa.route_reasoner as rr_mod

    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "answer"}}]}).encode()

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    import urllib.request
    rr_mod.urllib.request.urlopen = _fake_urlopen

    from trinity.qa.route_reasoner import RouteReasoner

    rr = RouteReasoner(api_key="test-key", model="deepseek-chat")
    rr._chat("SYSTEM FIXED", "user variant")
    messages = captured["payload"]["messages"]
    assert messages[0]["content"].startswith("[trinity-qa-v1]")
    assert messages[1]["content"] == "user variant"
