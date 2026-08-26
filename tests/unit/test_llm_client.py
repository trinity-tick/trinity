"""P1-7: LLM reasoning-format adapter (COMPARISON_VS_2026_SOTA_R7).

Verifies:
  - is_reasoning_model detects reasoning models
  - normalize_response extracts answer from reasoning_content when
    content is empty (deepseek-v4-pro style: reasoning_content + length)
  - extract_answer_from_reasoning handles answer markers / last paragraph
  - chat model responses pass through unchanged
  - reasoning_budget applies thinking budget for reasoning models only
"""

import json

import pytest

from trinity.llm.client import (
    extract_answer_from_reasoning,
    is_reasoning_model,
    normalize_response,
    reasoning_budget,
    resolve_default_model,
    resolve_api_key,
)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("deepseek-v4-pro", True),
        ("deepseek-reasoner", True),
        ("deepseek-r1", True),
        ("o3-mini", True),
        ("gpt-5-pro", True),
        ("deepseek-chat", False),
        ("deepseek-v4-flash", False),
        ("gpt-4o-mini", False),
    ],
)
def test_is_reasoning_model(model, expected):
    assert is_reasoning_model(model) is expected


def test_normalize_reasoning_model_empties_content():
    body = {
        "choices": [{
            "message": {
                "content": "",
                "reasoning_content": "先看第一条证据。\n\n再看第二条。\n\n最终答案: 2026-08-24",
            },
            "finish_reason": "length",
        }],
        "model": "deepseek-v4-pro",
    }
    r = normalize_response(body)
    assert r["content"] == "2026-08-24"
    assert r["finish_reason"] == "length"
    assert "最终答案" in r["reasoning"]


def test_normalize_reasoning_model_missing_content_key():
    body = {
        "choices": [{
            "message": {"reasoning_content": "推理过程……\n结论是 3"},
            "finish_reason": "stop",
        }],
        "model": "deepseek-reasoner",
    }
    r = normalize_response(body)
    assert r["content"] == "3"


def test_normalize_chat_model_passthrough():
    body = {
        "choices": [{
            "message": {"content": "正常回答", "reasoning_content": None},
            "finish_reason": "stop",
        }],
        "model": "deepseek-chat",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    r = normalize_response(body)
    assert r["content"] == "正常回答"
    assert r["reasoning"] == ""
    assert r["usage"]["completion_tokens"] == 5


def test_normalize_empty_choices():
    r = normalize_response({"choices": []})
    assert r["content"] == ""
    assert r["finish_reason"] is None


def test_extract_answer_marker_priority():
    reasoning = "第一段无关。\n\n最终答案: 42\n\n再想想……"
    assert extract_answer_from_reasoning(reasoning) == "42"


def test_extract_answer_last_paragraph():
    reasoning = "step one\n\nstep two\n\nthe answer is seven"
    assert extract_answer_from_reasoning(reasoning) == "seven"


def test_extract_answer_cleans_prefix():
    reasoning = "因此，答案是 5"
    out = extract_answer_from_reasoning(reasoning)
    assert out == "5"


def test_extract_answer_empty():
    assert extract_answer_from_reasoning("") == ""
    assert extract_answer_from_reasoning("   ") == ""


def test_reasoning_budget(monkeypatch):
    monkeypatch.delenv("TRINITY_LLM_THINKING_TOKENS", raising=False)
    assert reasoning_budget("deepseek-v4-pro") == 4096
    assert reasoning_budget("deepseek-chat") == 0


def test_reasoning_budget_env_override(monkeypatch):
    monkeypatch.setenv("TRINITY_LLM_THINKING_TOKENS", "8192")
    assert reasoning_budget("deepseek-v4-pro") == 8192


def test_chat_completion_adds_budget_for_reasoning_model(monkeypatch):
    """推理模型未显式 max_tokens 时自动补 thinking budget。"""
    monkeypatch.delenv("TRINITY_LLM_THINKING_TOKENS", raising=False)

    captured = {}

    class _FakeResp:
        def __init__(self, body):
            self._body = json.dumps(body).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return self._body

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["url"] = req.full_url
        return _FakeResp({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr("trinity.llm.client.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("TRINITY_LLM_API_KEY", "k")

    from trinity.llm.client import chat_completion

    r = chat_completion({"model": "deepseek-v4-pro", "messages": []})
    assert r["content"] == "ok"
    assert captured["payload"]["max_tokens"] == 4096
    assert captured["url"].endswith("/chat/completions")


def test_chat_completion_respects_explicit_max_tokens(monkeypatch):
    captured = {}

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()

    def _fake_urlopen(req, timeout):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr("trinity.llm.client.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setenv("TRINITY_LLM_API_KEY", "k")

    from trinity.llm.client import chat_completion

    chat_completion({"model": "deepseek-v4-pro", "messages": [], "max_tokens": 100})
    assert captured["payload"]["max_tokens"] == 100


def test_chat_completion_no_key_raises(monkeypatch):
    monkeypatch.delenv("TRINITY_LLM_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    from trinity.llm.client import chat_completion
    with pytest.raises(RuntimeError):
        chat_completion({"model": "deepseek-chat", "messages": []})
