"""Trinity — engine_worker 看门狗与插件自愈测试（2026-08-17 worker 卡死修复）。

覆盖：
- 空闲 worker 不被看门狗误杀（in-flight 标志位只在请求处理中超时才触发）
- 请求处理卡死超过 _STALL_TIMEOUT → dump 栈 + os._exit(1)（插件据此重启）
- 正常 ping 协议往返仍可用
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

WORKER_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "trinity"))
WORKER = os.path.join(WORKER_DIR, "engine_worker.py")
PY = sys.executable


def _spawn(extra_env=None, stall_timeout="1"):
    env = dict(os.environ)
    env["TRINITY_WORKER_STALL_TIMEOUT"] = stall_timeout
    env["TRINITY_MEMORY_ENABLED"] = "0"  # 与插件一致：禁用 import 期聚合器自举
    if extra_env:
        env.update(extra_env)
    return subprocess.Popen(
        [PY, WORKER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
        env=env, cwd=os.path.dirname(os.path.abspath(__file__)),
    )


def test_worker_idle_not_killed_by_watchdog() -> None:
    """空闲（无 in-flight 请求）超过 STALL_TIMEOUT 不应退出，ping 仍可用。"""
    p = _spawn(stall_timeout="1")
    try:
        time.sleep(12)  # 远超 1s 阈值
        assert p.poll() is None, f"idle worker exited early: {p.poll()}"
        # 注: 首个 ping 触发懒引擎初始化（Trinity() 连接 74MB 大库+建表，
        # 实测 5-30s），可能被小 stall 阈值合法击杀——那是看门狗预期行为；
        # 空闲不杀才是本测试断言点。ping 协议由 test_worker_ping_protocol 覆盖。
    finally:
        p.kill()
        p.wait(timeout=10)


def test_worker_watchdog_kills_stalled_request() -> None:
    """in-flight 请求超过 STALL_TIMEOUT → 输出 stalled 并以非 0 退出。"""
    code = (
        "import sys, time; sys.path.insert(0, %r); "
        "import engine_worker as ew; "
        "ew._STALL_TIMEOUT = 1; "
        "ew._request_in_flight = True; "
        "ew._request_start = time.time() - 5; "
        "ew._start_watchdog(); "
        "time.sleep(15); "
        "print('NO_EXIT')"
    ) % WORKER_DIR
    r = subprocess.run(
        [PY, "-c", code], capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
    )
    assert r.returncode != 0, f"watchdog did not exit, stdout={r.stdout!r}"
    assert "stalled" in (r.stderr + r.stdout), f"no stall message: {r.stderr[-500:]!r}"
    assert "NO_EXIT" not in (r.stdout + r.stderr)


def test_worker_ping_protocol() -> None:
    """正常 ping 往返（协议可用性回归）。"""
    p = _spawn(stall_timeout="90")
    try:
        p.stdin.write(json.dumps({"id": 7, "method": "ping"}) + "\n")
        p.stdin.flush()
        line = p.stdout.readline()
        assert line and '"pong": true' in line, f"ping failed: {line!r}"
    finally:
        p.kill()
        p.wait(timeout=10)
