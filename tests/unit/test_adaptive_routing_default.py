"""P0-3: 自适应路由默认 on (COMPARISON_VS_2026_SOTA_R8).

Verifies:
  - TRINITY_ADAPTIVE_ROUTING unset → routing auto 生效（默认 on）
  - short query (≤8 chars) → light 路径（FTS）
  - long query → full 路径
  - TRINITY_ADAPTIVE_ROUTING=off → 强制 full（兼容旧行为）
  - explicit routing="light"/"full" 永远优先
"""

import pytest

from trinity.core.client import Trinity


@pytest.fixture()
def _clear_env(monkeypatch, tmp_path):
    monkeypatch.delenv("TRINITY_ADAPTIVE_ROUTING", raising=False)
    monkeypatch.delenv("TRINITY_DB_PATH", raising=False)
    return tmp_path


def _mem(tmp_path):
    return Trinity(adapter="sqlite", store_path=str(tmp_path))


def test_default_on_routes_short_query_to_light(monkeypatch, _clear_env, tmp_path):
    """未设置 env → 默认 on：短查询走 light（FTS 快通道）。"""
    mem = _mem(tmp_path)
    mem.ingest("用户偏好暗色模式", tags=["preference"])

    class _Probe:
        """拦截 search_memories 调用，标记 light 路径是否命中。"""

        def __init__(self, adapter):
            self._adapter = adapter
            self.calls = []

        def search_memories(self, *a, **kw):
            self.calls.append(("fts", a, kw))
            return self._adapter.search_memories(*a, **kw)

    probe = _Probe(mem._adapter)
    mem._adapter = probe

    res = mem.search_hybrid("用户偏好", top_k=5, routing="auto")
    assert res["breakdown"]["routing"] == "light"
    assert probe.calls, "light 路径应调用 FTS search_memories"


def test_default_on_routes_long_query_to_full(monkeypatch, _clear_env, tmp_path):
    """长查询（>8 字符）走 full 5 通道。"""
    mem = _mem(tmp_path)
    mem.ingest("用户偏好暗色模式并且喜欢深色主题", tags=["preference"])
    res = mem.search_hybrid("用户偏好暗色模式并且喜欢深色主题", top_k=5, routing="auto")
    # full 路径可能因混合检索器懒初始化走 FTS 兜底，但 routing 标记应为 full
    assert res["breakdown"]["routing"] == "full"


def test_env_off_forces_full(monkeypatch, _clear_env, tmp_path):
    """TRINITY_ADAPTIVE_ROUTING=off → 短查询也走 full（旧行为兼容）。"""
    monkeypatch.setenv("TRINITY_ADAPTIVE_ROUTING", "off")
    mem = _mem(tmp_path)
    mem.ingest("用户偏好", tags=["preference"])
    res = mem.search_hybrid("用户偏好", top_k=5, routing="auto")
    assert res["breakdown"]["routing"] == "full"


def test_explicit_routing_wins(monkeypatch, _clear_env, tmp_path):
    """显式 routing 参数优先于 env。"""
    monkeypatch.setenv("TRINITY_ADAPTIVE_ROUTING", "off")
    mem = _mem(tmp_path)
    mem.ingest("用户偏好", tags=["preference"])
    res = mem.search_hybrid("用户偏好", top_k=5, routing="light")
    assert res["breakdown"]["routing"] == "light"
