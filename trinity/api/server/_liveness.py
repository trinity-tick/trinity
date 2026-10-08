# -*- coding: utf-8 -*-
"""API 进程存活心跳（2026-10-06 复评 G7）。

## 动机（实测）

2026-10-06 **12:56:22** 常驻 API **无痕消失**：supervisor 记录
`prev=24020 prevAlive=False grace=False procs=0`（进程真的没了），
而 `api.out.log` 末条仍是正常的 `GET /health HTTP/1.1 200 OK`，
`api.err.log` 里**没有任何崩溃栈**。结果 **死因不可查** —— 只能靠排除法
（已排除 lock-watchdog、库损坏、内存守护），留下一个未定项。

## 心跳把"无痕"变成"有痕"

每 `TRINITY_LIVENESS_INTERVAL_S`（默认 30s）向
`~/.trinity/state/api_liveness.jsonl` **追加**一行 JSON：

    {"ts": "...", "pid": 1234, "uptime_s": 812.3, "inflight": 0, "slots_held": 0}

判读方式：
* **最后一次心跳的时刻 = 存活下界** —— 死亡必发生在它之后、supervisor 发现之前；
* 心跳字段能区分"卡死"（进程在但心跳停更）与"瞬死"（心跳正常然后突然中断）；
* 与 `api.out.log`/`api.err.log` 的尾部对齐，可把死因收敛到某个请求之后。

## 安全约束（比功能更重要）

* **绝不抛异常**：整个循环体包在 `try/except BaseException` 里 ——
  一个诊断工具绝不能成为 API 的死因（本仓已有"守卫反而杀服务"的先例）。
* **绝不阻塞事件循环**：文件写在 `asyncio.to_thread` 里。
* 目录不可写时静默降级（只记一次 debug），不影响服务。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

LIVENESS_ENV = "TRINITY_LIVENESS"
INTERVAL_ENV = "TRINITY_LIVENESS_INTERVAL_S"


def is_enabled() -> bool:
    return os.environ.get(LIVENESS_ENV, "on").strip().lower() in ("on", "1", "true", "yes")


def liveness_path() -> str:
    return os.path.join(
        os.path.expanduser("~"), ".trinity", "state", "api_liveness.jsonl")


def _interval_s() -> float:
    try:
        v = float(os.environ.get(INTERVAL_ENV, "30"))
        return v if v >= 1.0 else 30.0
    except Exception:  # noqa: BLE001
        return 30.0


def _snapshot() -> dict:
    """采集一行心跳。所有子项都容错——缺哪个字段不影响这一行落盘。"""
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "epoch": round(time.time(), 3),
           "pid": os.getpid()}
    try:
        row["uptime_s"] = round(time.time() - _PROCESS_START_EPOCH, 1)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）trinity/api/server/_liveness.py::_snapshot")
    try:
        from trinity.api.server._routers_search import (  # noqa: E402
            _SEARCH_STATS, _VECTOR_STATS, search_pool_status)
        row["slots_held"] = int((search_pool_status() or {}).get("slots_held") or 0)
        row["search_timeouts"] = int((_SEARCH_STATS or {}).get("timeout") or 0)
        row["vector_timeouts"] = int((_VECTOR_STATS or {}).get("timeout") or 0)
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).debug("t95: 吞掉异常（已显式留痕）trinity/api/server/_liveness.py::_snapshot")
    return row


_PROCESS_START_EPOCH = time.time()


def _append(row: dict) -> None:
    p = liveness_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def liveness_loop() -> None:
    """心跳主循环。**任何异常都被吞掉并且循环继续**（诊断工具不得成为死因）。"""
    if not is_enabled():
        return
    interval = _interval_s()
    warned = False
    while True:
        try:
            await asyncio.to_thread(_append, _snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if not warned:
                warned = True
                logger.debug("liveness: 写心跳失败（已降级，不影响服务）: %s", exc)
        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise


def last_heartbeat() -> dict:
    """读最后一行心跳（供看护环/运维消费；无文件或无有效行时返回 {}）。"""
    try:
        p = liveness_path()
        last = None
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        return json.loads(last) if last else {}
    except Exception:  # noqa: BLE001
        return {}
