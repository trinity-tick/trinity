"""P0: Structured Outputs + reasoning effort (CLOSURE_AND_OPTIMIZATION_20260824).

Verifies:
  - chat_completion 透传 response_format / reasoning_effort 到 payload
  - parse_structured_response: JSON 解析 + schema 语义校验 + 失败返回 None
  - proposition_extractor: schema 包装格式 {"propositions": [...]} 兼容
  - proposition_extractor._llm_json_call 走 chat_completion（schema + effort=low）
"""

import json

import pytest

from trinity.llm.client import (
    chat_completion,
    parse_structured_response,
    stable_prefix_messages,
)
from trinity.memory.proposition_extractor import (
    PROPOSITION_SCHEMA,
    _parse_propositions,
    _llm_json_call,
)


# ── chat_completion 透传 ────────────────────────────────────────────

def test_chat_completion_passes_response_format_and_effort(monkeypatch):
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr("trinity.llm.client.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("TRINITY_LLM_API_KEY", "k")

    chat_completion(
        {"model": "deepseek-chat", "messages": []},
        response_format={"type": "json_object"},
        reasoning_effort="low",
    )
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["reasoning_effort"] == "low"


def test_chat_completion_default_no_extra_fields(monkeypatch):
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr("trinity.llm.client.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("TRINITY_LLM_API_KEY", "k")

    chat_completion({"model": "deepseek-chat", "messages": []})
    assert "response_format" not in captured["payload"]
    assert "reasoning_effort" not in captured["payload"]


# ── parse_structured_response ───────────────────────────────────────

def test_parse_structured_valid():
    resp = {"content": json.dumps({"propositions": [{"type": "user_preference",
                                                     "proposition": "喜欢深色"}]})}
    obj = parse_structured_response(resp, schema=PROPOSITION_SCHEMA)
    assert obj is not None
    assert len(obj["propositions"]) == 1


def test_parse_structured_missing_required():
    resp = {"content": json.dumps({"propositions": [{"type": "user_preference"}]})}
    # proposition 缺失 → schema 校验失败
    obj = parse_structured_response(resp, schema=PROPOSITION_SCHEMA)
    assert obj is None


def test_parse_structured_bad_json():
    assert parse_structured_response({"content": "not json"}, schema=PROPOSITION_SCHEMA) is None
    assert parse_structured_response({"content": ""}) is None


def test_parse_structured_dict_content():
    resp = {"content": {"propositions": []}}
    obj = parse_structured_response(resp, schema=PROPOSITION_SCHEMA)
    assert obj == {"propositions": []}


def test_parse_structured_type_check():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert parse_structured_response({"content": '{"n": "x"}'}, schema=schema) is None
    assert parse_structured_response({"content": '{"n": 3}'}, schema=schema) == {"n": 3}


# ── proposition_extractor schema 兼容 ───────────────────────────────

def test_parse_propositions_wrapped_format():
    """Structured Outputs 包装格式 {"propositions": [...]} 兼容解析。"""
    text = json.dumps({"propositions": [
        {"type": "user_preference", "proposition": "用户喜欢深色模式", "ts": "2026-08-18", "expires": None},
    ]})
    out = _parse_propositions(text)
    assert len(out) == 1
    assert out[0]["type"] == "user_preference"
    assert out[0]["proposition"] == "用户喜欢深色模式"


def test_parse_propositions_string_array():
    """实测 DeepSeek json_schema 简化为字符串数组形态兼容。"""
    text = json.dumps({"propositions": ["用户是供应链项目经理", "用户喜欢深色模式"]})
    out = _parse_propositions(text)
    assert len(out) == 2
    assert all(p["type"] == "user_fact" for p in out)
    assert out[0]["proposition"] == "用户是供应链项目经理"


def test_parse_propositions_legacy_array():
    """旧格式数组仍兼容。"""
    text = json.dumps([
        {"type": "user_fact", "proposition": "用户是项目经理", "ts": None, "expires": None},
    ])
    out = _parse_propositions(text)
    assert len(out) == 1
    assert out[0]["proposition"] == "用户是项目经理"


def test_parse_propositions_wrapped_bad_item_filtered():
    text = json.dumps({"propositions": [
        {"type": "bad_type", "proposition": "x"},
        {"type": "user_fact", "proposition": ""},
        {"type": "user_done", "proposition": "有效命题"},
    ]})
    out = _parse_propositions(text)
    assert len(out) == 1
    assert out[0]["proposition"] == "有效命题"


def test_llm_json_call_uses_chat_completion(monkeypatch):
    """_llm_json_call 走 chat_completion（response_format + effort=low）。"""
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            body = {"choices": [{"message": {"content": json.dumps(
                {"propositions": [{"type": "user_preference",
                                   "proposition": "P", "ts": None, "expires": None}]}
            )}}]}
            return json.dumps(body).encode()

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr("trinity.llm.client.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("TRINITY_LLM_API_KEY", "k")

    out = _llm_json_call("sys", "user")
    parsed = json.loads(out)
    assert isinstance(parsed, list)  # schema 命中后返回数组
    assert captured["payload"]["reasoning_effort"] == "low"
    assert captured["payload"]["response_format"]["type"] == "json_schema"
    assert captured["payload"]["response_format"]["json_schema"]["name"] == "propositions"


def test_stable_prefix_still_works_with_new_params():
    """stable_prefix_messages 与新参数正交（回归保护）。"""
    msgs = stable_prefix_messages("SYS", "USER", tag="t1")
    assert msgs[0]["content"].startswith("[t1]")
