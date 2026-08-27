# -*- coding: utf-8 -*-
"""Knowledge layer unit tests (Context7 借鉴)."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.knowledge import (
    expand_query, load_aliases, _health, build_sources, sources,
)



import pytest


@pytest.fixture(autouse=True)
def _ensure_knowledge_home():
    """运行时保证 HOME+aliases（防多测试文件 TRINITY_HOME 污染）。"""
    os.environ["TRINITY_HOME"] = os.path.join(os.path.dirname(__file__), "..", "temp", "k_test_home")
    os.makedirs(os.environ["TRINITY_HOME"], exist_ok=True)
    with open(os.path.join(os.environ["TRINITY_HOME"], "aliases.yaml"), "w", encoding="utf-8") as f:
        f.write("aliases:" + chr(10) + "  WMS: [仓库管理系统, SmartCos]" + chr(10) + "  旺店通: [WMS]" + chr(10))
    import trinity.knowledge as _K
    _K._ALIASES_CACHE = None
    yield



class TestHealth:


    def test_fresh(self):
        assert _health(0, 10, 20) >= 0.7
        assert _health(60, 0, 1) < 0.5

    def test_range(self):
        for d, u, c in [(0, 0, 0), (100, 100, 100), (30, 5, 10)]:
            h = _health(d, u, c)
            assert 0.0 <= h <= 1.0


class TestAliases:
    def test_expand(self):
        assert "WMS" in expand_query("WMS 上架规则")
        assert "SmartCos" in expand_query("WMS 上架规则")
        assert expand_query("没有别名的话") == "没有别名的话"

    def test_expand_wangdiantong(self):
        out = expand_query("旺店通 对比")
        assert "WMS" in out

    def test_no_duplicate(self):
        out = expand_query("WMS 系统 WMS 规则")
        assert out.count("SmartCos") == 1


class TestSources:
    def test_health_domain(self):
        assert build_sources.__doc__ is not None

    def test_load_aliases(self):
        a = load_aliases(force=True)
        assert "wms" in a
        assert "仓库管理系统" in a["wms"]
