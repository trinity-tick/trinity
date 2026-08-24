# -*- coding: utf-8 -*-
"""engine_worker 首请求预热（2026-08-22 优化）单测。

覆盖：
  - should_prewarm 三态（默认 on / TRINITY_MEMORY_ENABLED=0 跳过 / 显式 off）
  - _run_prewarm 成功与异常降级（不崩、完成标记翻转）
  - _start_prewarm 的开关门控与线程启动

注意：import trinity.engine_worker 有模块级副作用（os.dup(1)/stdout 重定向），
因此延迟到用例内加载并在加载后恢复 sys.stdout。
"""
import importlib
import sys
import threading

import pytest

_worker = None
_orig_stdout = None


@pytest.fixture(scope="module")
def worker():
    global _worker, _orig_stdout
    if _worker is None:
        _orig_stdout = sys.stdout
        try:
            _worker = importlib.import_module("trinity.engine_worker")
        finally:
            sys.stdout = _orig_stdout
    return _worker


# ── should_prewarm 判定 ────────────────────────────────────────────

def test_should_prewarm_default_on(worker):
    assert worker.should_prewarm({}) is True
    assert worker.should_prewarm({"TRINITY_MEMORY_ENABLED": "1"}) is True


def test_should_prewarm_memory_disabled_still_on(worker):
    # 2026-08-22 收尾：引擎预热与聚合器自举解耦——MEMORY_ENABLED=0（worker
    # 默认形态，抑制 import 期聚合器自举）不再门控引擎预热。
    assert worker.should_prewarm({"TRINITY_MEMORY_ENABLED": "0"}) is True
    assert worker.should_prewarm(
        {"TRINITY_MEMORY_ENABLED": "0", "TRINITY_WORKER_PREWARM": "on"}
    ) is True


def test_should_prewarm_explicit_off(worker):
    for val in ("off", "0", "false", "no"):
        assert worker.should_prewarm({"TRINITY_WORKER_PREWARM": val}) is False


def test_should_prewarm_explicit_on_keeps_default(worker):
    assert worker.should_prewarm({"TRINITY_WORKER_PREWARM": "on"}) is True
    assert worker.should_prewarm({"TRINITY_WORKER_PREWARM": "1"}) is True


# ── _run_prewarm 成功 / 降级 ───────────────────────────────────────

class _FakeEngine:
    def __init__(self):
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return {"results": [], "pushed_memories": []}


def test_run_prewarm_success(worker, monkeypatch):
    fake = _FakeEngine()
    monkeypatch.setattr(worker, "_get_engine", lambda: fake)
    worker._prewarm_done = False
    worker._run_prewarm()
    assert worker._prewarm_done is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["mode"] == "keyword"
    assert fake.calls[0]["top_k"] == 1


def test_run_prewarm_failure_degrades(worker, monkeypatch):
    def boom():
        raise RuntimeError("engine init failed")

    monkeypatch.setattr(worker, "_get_engine", boom)
    worker._prewarm_done = False
    worker._run_prewarm()  # 不得抛出
    assert worker._prewarm_done is True


# ── _start_prewarm 门控 ────────────────────────────────────────────

def test_start_prewarm_skips_when_disabled(worker, monkeypatch):
    monkeypatch.setenv("TRINITY_WORKER_PREWARM", "off")
    monkeypatch.delenv("TRINITY_MEMORY_ENABLED", raising=False)

    def _no_thread(*a, **k):
        raise AssertionError("prewarm thread must not start when disabled")

    monkeypatch.setattr(threading, "Thread", _no_thread)
    worker._start_prewarm()  # 不应启动线程


def test_start_prewarm_spawns_thread(worker, monkeypatch):
    monkeypatch.delenv("TRINITY_MEMORY_ENABLED", raising=False)
    monkeypatch.delenv("TRINITY_WORKER_PREWARM", raising=False)
    spawned = {}

    class _FakeThread:
        def __init__(self, target=None, daemon=False, name=None):
            spawned["target"] = target
            spawned["daemon"] = daemon
            spawned["name"] = name
            spawned["started"] = False

        def start(self):
            spawned["started"] = True

    monkeypatch.setattr(threading, "Thread", _FakeThread)
    worker._start_prewarm()
    assert spawned.get("target") is worker._run_prewarm
    assert spawned.get("daemon") is True
    assert "prewarm" in (spawned.get("name") or "")
    assert spawned.get("started") is True
