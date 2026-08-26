# -*- coding: utf-8 -*-
"""Trinity 基建夯实——压测/锁测试/自污染写入隔离回归。

2026-08-16:ingest 层 TRINITY_ISOLATE_TEST_WRITES 守卫(默认 on):
已知测试 agent/category/标签/内容标记的写入强制 archived——
仍落库可验证,但不进入 active 检索面。
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from trinity.core.client import Trinity

import pytest

@pytest.fixture(autouse=True)
def _iso_env_guard():
    """2026-08-26（下一步建议）：防御其他测试残留的 ISOLATE=off 污染。"""
    saved = os.environ.get("TRINITY_ISOLATE_TEST_WRITES")
    os.environ["TRINITY_ISOLATE_TEST_WRITES"] = "on"
    yield
    if saved is None:
        os.environ.pop("TRINITY_ISOLATE_TEST_WRITES", None)
    else:
        os.environ["TRINITY_ISOLATE_TEST_WRITES"] = saved



def _fresh_store():
    d = tempfile.mkdtemp(prefix="iso_")
    return os.path.join(d, "t.db")


class TestIsolatedTestWrites:
    def _client(self):
        return Trinity(store_path=_fresh_store())

    def test_stress_agent_and_category_isolated(self):
        mem = self._client()
        r = mem.ingest("压测模拟项目 #1 会议纪要 Alpha 接口优化",
                       agent_id="stress-agent", category="stress-test",
                       postprocess=False)
        mid = r.get("memory_id")
        assert mid, r
        # 不进入检索面
        hits = mem._adapter.search_memories("压测模拟项目", top_k=5)
        assert all(h["memory_id"] != mid for h in hits), "压测写入不应可检索"
        # 状态为 archived(仍落库)
        got = mem._adapter.get_memory(mid)
        assert got is not None and got["status"] == "archived", got

    def test_locktest_agent_isolated(self):
        mem = self._client()
        r = mem.ingest("APILOCK-1-1786", agent_id="lock-test",
                       tags=["locktest"], postprocess=False)
        mid = r["memory_id"]
        got = mem._adapter.get_memory(mid)
        assert got["status"] == "archived", got

    def test_auto_link_noise_content_isolated(self):
        mem = self._client()
        r = mem.ingest("[自动关联] 与 3 条已有记忆相关: 旧管线自污染",
                       agent_id="default", postprocess=False)
        mid = r["memory_id"]
        got = mem._adapter.get_memory(mid)
        assert got["status"] == "archived", got

    def test_normal_write_stays_active_and_searchable(self):
        mem = self._client()
        r = mem.ingest("正常记忆:数据库查询优化记录",
                       agent_id="default", category="general",
                       postprocess=False)
        mid = r["memory_id"]
        got = mem._adapter.get_memory(mid)
        assert got["status"] == "active", got
        hits = mem._adapter.search_memories("数据库查询优化", top_k=5)
        assert any(h["memory_id"] == mid for h in hits), "正常写入应可检索"

    def test_guard_off_disables_isolation(self, monkeypatch):
        monkeypatch.setenv("TRINITY_ISOLATE_TEST_WRITES", "off")
        mem = self._client()
        r = mem.ingest("压测关闭隔离验证", agent_id="stress-agent",
                       category="stress-test", postprocess=False)
        mid = r["memory_id"]
        got = mem._adapter.get_memory(mid)
        assert got["status"] == "active", "关闭守卫后压测写入应保持 active"
