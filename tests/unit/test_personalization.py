"""Trinity — R3 P0-2 个性化接入单元测试（2026-08-15）。

覆盖：Trinity 的 PAHF 个性化入口——
- personalization 惰性实例化
- integrate_feedback 反馈入库
- get_preference_context 偏好检索
- should_clarify 澄清判断（降级安全）
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from trinity.core.client import Trinity


@pytest.fixture()
def t(tmp_path: Path) -> Trinity:
    return Trinity(store_path=str(tmp_path / "p.db"))


def test_personalization_engine_available(t: Trinity) -> None:
    assert t.personalization is not None


def test_integrate_feedback(t: Trinity) -> None:
    r = t.integrate_feedback("u1", {
        "content": "偏好简洁的中文回答", "domain": "style",
        "feedback_type": "explicit_confirm",
    })
    assert r["integrated"] is True
    assert r["changes"] >= 1


def test_get_preference_context(t: Trinity) -> None:
    t.integrate_feedback("u1", {"content": "偏好 markdown 输出", "domain": "format"})
    ctx = t.get_preference_context("u1", "format")
    assert ctx["enabled"] is True
    assert len(ctx["preferences"]) >= 1
    assert ctx["preferences"][0]["content"] == "偏好 markdown 输出"


def test_get_preference_context_no_user(t: Trinity) -> None:
    ctx = t.get_preference_context("nobody", "search")
    assert ctx["enabled"] is True
    assert ctx["preferences"] == []


def test_should_clarify_empty_context(t: Trinity) -> None:
    assert t.should_clarify("u1", "") is False


def test_integrate_feedback_empty_content(t: Trinity) -> None:
    r = t.integrate_feedback("u1", {"content": ""})
    assert r["integrated"] is False
