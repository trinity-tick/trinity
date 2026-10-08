# -*- coding: utf-8 -*-
"""API 存活心跳（2026-10-06 复评 G7）。

## 缺陷

2026-10-06 **12:56:22** 常驻 API **无痕消失**：supervisor 记录
`prevAlive=False procs=0`（进程真的没了），而 `api.out.log` 末条仍是正常的
`GET /health 200`，`api.err.log` 里**没有任何崩溃栈** ⇒ **死因不可查**，
只能靠排除法（已排除 lock-watchdog / 库损坏 / 内存守护），留下一个未定项。

## 处置

每 `TRINITY_LIVENESS_INTERVAL_S`（默认 30s）向
`~/.trinity/state/api_liveness.jsonl` 追加一行心跳。最后一次心跳给出**存活下界**，
并可区分"卡死"（进程在、心跳停更）与"瞬死"（心跳正常后突然中断）。

## 本文件锁三件事

1. 心跳真的落盘，字段齐全；
2. **`liveness_loop` 绝不因内部异常退出** —— 诊断工具绝不能成为服务死因；
3. **启动失败不得静默** —— 实现过程中真踩到一次：`trinity/api/server/__init__.py`
   当时**未导入 `asyncio`**，`create_task` 抛 `NameError` 被 `except` 吞掉、
   心跳静默不生效（正是本仓反复出现的失效形态）。现在异步任务启动失败会打 WARNING。
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trinity.api.server import _liveness as L  # noqa: E402
import logging

INIT_PY = ROOT / "trinity" / "api" / "server" / "__init__.py"


def _run_loop_for(seconds: float, interval: str = "1"):
    async def main():
        t = asyncio.create_task(L.liveness_loop())
        await asyncio.sleep(seconds)
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）tests/unit/test_api_liveness_20261006.py::main")
    asyncio.run(main())


def test_heartbeat_is_written(monkeypatch):
    monkeypatch.setenv("TRINITY_LIVENESS_INTERVAL_S", "1")
    p = pathlib.Path(L.liveness_path())
    before = p.stat().st_size if p.exists() else 0
    _run_loop_for(2.4)
    after = p.stat().st_size if p.exists() else 0
    assert after > before, "心跳没有落盘"


def test_heartbeat_fields_present(monkeypatch):
    monkeypatch.setenv("TRINITY_LIVENESS_INTERVAL_S", "1")
    _run_loop_for(1.4)
    hb = L.last_heartbeat()
    for k in ("ts", "epoch", "pid", "uptime_s"):
        assert k in hb, "心跳缺字段 %s：%r" % (k, hb)
    assert hb["pid"] == os.getpid()


def test_switch_off_writes_nothing(monkeypatch):
    monkeypatch.setenv(L.LIVENESS_ENV, "off")
    p = pathlib.Path(L.liveness_path())
    before = p.stat().st_size if p.exists() else 0
    _run_loop_for(1.2)
    after = p.stat().st_size if p.exists() else 0
    assert after == before, "关掉开关后仍在写心跳"
    assert L.is_enabled() is False


def test_loop_survives_append_failure(monkeypatch):
    """**诊断工具绝不能成为服务死因**：写盘抛异常时循环必须继续、且不冒泡。"""
    calls = {"n": 0}

    def _boom(_row):
        calls["n"] += 1
        raise OSError("disk full (simulated)")

    monkeypatch.setenv("TRINITY_LIVENESS_INTERVAL_S", "1")
    monkeypatch.setattr(L, "_append", _boom)
    _run_loop_for(2.4)          # 不抛异常即为通过（异常会打断 asyncio.run）
    assert calls["n"] >= 2, "循环在第一次写失败后就退出了（应继续重试）"


def test_startup_failure_is_loud_not_silent():
    """启动侧：异步任务创建失败必须打 WARNING，不得静默。

    这是本轮真实踩到的坑：`__init__.py` 当时未导入 `asyncio`，
    `create_task` 抛 NameError 被 `except` 吞掉 ⇒ 心跳静默不生效。
    """
    src = INIT_PY.read_text(encoding="utf-8")
    assert "import asyncio" in src, "本模块必须导入 asyncio（否则 create_task 抛 NameError）"
    assert "liveness heartbeat NOT started" in src, "启动失败必须 WARNING，不得静默"
    assert "liveness_loop" in src


def test_heartbeat_is_exposed_for_watchdogs():
    """心跳必须可被看护环读（`last_heartbeat()`），否则等于只写不读。"""
    assert callable(L.last_heartbeat)
    assert L.last_heartbeat() == {} or isinstance(L.last_heartbeat(), dict)
