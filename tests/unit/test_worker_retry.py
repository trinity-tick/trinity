"""Trinity — engine_worker 写锁快速失败+重试测试（2026-08-17 根治）。

覆盖：
- database is locked → 自动重试 → 成功
- 持续锁冲突 → 快速抛明确错误（不再 60s 卡死）
- 非锁错误 → 不重试直接抛
"""

from __future__ import annotations

import os
import subprocess
import sys

WORKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "trinity"))


def _worker_module():
    sys.path.insert(0, WORKER_DIR)
    import engine_worker  # noqa: F401
    return engine_worker


def test_retry_on_locked_succeeds_after_one_conflict() -> None:
    ew = _worker_module()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database is locked")
        return "ok"

    assert ew._retry_on_locked(fn, retries=1) == "ok"
    assert calls["n"] == 2


def test_retry_on_locked_fails_fast_with_clear_error() -> None:
    ew = _worker_module()

    def fn():
        raise RuntimeError("database is locked (5)")

    import time
    t0 = time.time()
    try:
        ew._retry_on_locked(fn, retries=1, backoff_s=0.05)
        assert False, "should raise"
    except RuntimeError as e:
        assert "write lock busy" in str(e)
        assert "retried 1x" in str(e)
    assert time.time() - t0 < 5  # 秒级失败，非 60s 卡死


def test_retry_on_locked_non_lock_error_no_retry() -> None:
    ew = _worker_module()
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise ValueError("other error")

    try:
        ew._retry_on_locked(fn, retries=1)
        assert False
    except ValueError:
        pass
    assert calls["n"] == 1  # 非锁错误不重试


def test_worker_busy_timeout_env_applied() -> None:
    """worker 子进程应应用 TRINITY_SQLITE_BUSY_TIMEOUT_MS=3000。"""
    code = (
        "import os, sys; sys.path.insert(0, %r); "
        "import engine_worker; "
        "sys.stdout = sys.__stdout__; "  # engine_worker 重定向 stdout→stderr，先恢复
        "print(os.environ.get('TRINITY_SQLITE_BUSY_TIMEOUT_MS'))"
    ) % WORKER_DIR
    r = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    assert r.stdout.strip() == "3000"
