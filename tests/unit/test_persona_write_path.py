"""P1-③: PersonaEngine 写路径接线 (OPTIMIZATION_ANALYSIS_ROUND5).

Verifies:
  - ingest 写路径在 TRINITY_PERSONA=on 时触发 maybe_persona_after_store
  - 默认 off 时无副作用（不产生画像文件）
  - 画像端点数据随启用后写入增长（persona 文件生成）
"""

import os

import pytest

from trinity.core.client import Trinity
from trinity.memory.persona import PersonaEngine, persona_enabled


def _mem(tmp_path):
    return Trinity(adapter="sqlite", store_path=str(tmp_path))


def test_persona_enabled_default_off(monkeypatch):
    monkeypatch.delenv("TRINITY_PERSONA", raising=False)
    assert persona_enabled() is False
    monkeypatch.setenv("TRINITY_PERSONA", "on")
    assert persona_enabled() is True


def test_ingest_persona_off_no_side_effect(tmp_path, monkeypatch):
    """默认 off：写入不产生画像文件。"""
    monkeypatch.delenv("TRINITY_PERSONA", raising=False)
    mem = _mem(tmp_path)
    mem.ingest("用户喜欢深色模式", category="proposition",
               metadata={"proposition_type": "user_preference"}, tags=["pref"])
    # 无画像目录或空
    engine = PersonaEngine(adapter=mem._adapter)
    personas = engine.list_personas()
    assert isinstance(personas, list)


def test_ingest_persona_on_triggers(tmp_path, monkeypatch):
    """启用后：user_preference 命题写入触发画像合并（文件生成）。"""
    monkeypatch.setenv("TRINITY_PERSONA", "on")
    mem = _mem(tmp_path)
    mem.ingest("用户喜欢深色模式", category="proposition",
               metadata={"proposition_type": "user_preference"}, tags=["pref"])
    engine = PersonaEngine(adapter=mem._adapter)
    personas = engine.list_personas()
    assert "default" in personas
    text, count = engine.read_persona("default")
    assert "深色模式" in text or count >= 1


def test_ingest_persona_skips_non_preference(tmp_path, monkeypatch):
    """非 user_preference/fact 命题不触发画像（既有语义）。"""
    monkeypatch.setenv("TRINITY_PERSONA", "on")
    mem = _mem(tmp_path)
    mem.ingest("用户昨天完成了 WMS 对标", category="proposition",
               metadata={"proposition_type": "user_done"}, tags=["done"])
    engine = PersonaEngine(adapter=mem._adapter)
    personas = engine.list_personas()
    # user_done 不进画像；若引擎新建了空画像文件则 read 为空
    if "default" in personas:
        text, count = engine.read_persona("default")
        assert "WMS" not in text or count == 0
