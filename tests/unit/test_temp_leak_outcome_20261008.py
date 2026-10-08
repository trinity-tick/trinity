# -*- coding: utf-8 -*-
"""G31/t174 ② 判据面：`temp_leak_audit` 的 **proxy（原有）与 outcome（新增）并列** + **那处吞异常的可触发证明**。

背景（**别人替你发现的回归**，2026-10-08 14:35~14:40 由 t172 的门禁点名）：
我加 outcome 侧时写过一处 `except Exception: pass`（`os.path.getsize` 失败）⇒
`silent_failure:no_growth` 由 **334 → 335**（`scripts/temp_leak_audit.py 3 > 2`）。
修法按 t162 的同一惯例：**吞但计数**，并且**把"字节数可能少算"显式带进 outcome 读数**（不额外包一层）。

本文件三条判据：
- **C1 正向**：proxy 与 outcome **同时**可读（证明保留了对照片），且 outcome 带时点与库标签（temp_dir）。
- **C2 反向**：⭐ **确定性触发**那处 `OSError` 分支（替换 `os.path.getsize`）⇒ 计数必须 +1、
  且**样本被记下**、且 `swallow` 被调用（⇒ 证明我理解它"什么时候会发生"，不是为过门禁而包一层）。
- **C3 反例**：**正常的 outcome 扫描**（真 `%TEMP%`）里 `bytes_may_undercount` 必须为 **0**
  ⇒ 说明该分支在正常情况下**不会**乱报（否则读数会失去意义）。
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUDIT = os.path.join(ROOT, "scripts", "temp_leak_audit.py")


def _load():
    spec = importlib.util.spec_from_file_location("_g31_leak_audit", AUDIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_C1_proxy与outcome必须并列可读():
    """正向：同一脚本里 **proxy（scan→total/advisory_total）** 与 **outcome（outcome_scan）** 都能跑，
    且 outcome 读数带**时点**与**库标签（temp_dir）**。"""
    m = _load()
    proxy = m.scan(ROOT)
    assert "total" in proxy and "advisory_total" in proxy, "proxy 侧读数缺失"
    assert m.CLEANUP_MARKERS, "proxy 侧的清理标记表被删了 ⇒ 对照消失了"
    o = m.outcome_scan()
    assert o["ts"] and o["temp_dir"], "outcome 读数必须带时点与库标签（temp_dir）"
    print("[CHK] proxy: total=%d advisory=%d | outcome: temp_dir=%s prefixes=%d 时点=%s"
          % (proxy["total"], proxy.get("advisory_total", -1), o["temp_dir"], len(o["prefixes"]), o["ts"]))
    assert o["prefixes"], "outcome 侧一个前缀都没扫到 ⇒ 自动收集失效"


def test_C2_反向_确定性触发那处OSError分支():
    """⭐ 反向（**证明我理解它何时发生**）：
    把 `os.path.getsize` 替换成必抛 `OSError` ⇒
      ① `bytes_may_undercount` **必须 > 0**（= 计数了）；
      ② `size_error_sample` **必须有样本**（= 保留定位线索）；
      ③ `swallow` **必须被调用**（= 不是静默）。
    触发条件（真实形态）：目录被枚举之后、文件被删除/无权限（PID 竞态、pytest 清理并发）。
    """
    m = _load()
    calls = []
    real_getsize = os.path.getsize

    def fake_getsize(p):
        raise OSError(2, "No such file or directory (synthetic)", str(p))

    real_swallow = m.swallow

    def spy_swallow(*a, **k):
        calls.append(a)
        return None

    tmp_root = os.path.join(os.environ.get("TEMP", r"D:\Temp"), "g31_outcome_trigger")
    os.makedirs(os.path.join(tmp_root, "pre_", "inner"), exist_ok=True)
    open(os.path.join(tmp_root, "pre_", "inner", "f.bin"), "wb").write(b"x" * 32)
    try:
        m.os.path.getsize = fake_getsize
        m.swallow = spy_swallow
        o = m.outcome_scan(temp_dir=tmp_root, prefixes=["pre_"])
    finally:
        m.os.path.getsize = real_getsize
        m.swallow = real_swallow
        import shutil
        shutil.rmtree(tmp_root, ignore_errors=True)
    d = o["prefixes"]["pre_"]
    assert o["size_error_sample"], "getsize 失败必须留下样本（否则读数不可定位）"
    assert d["bytes_may_undercount"] >= 1, ("必须计数（吞但计数）", d)
    assert calls, "swallow 没被调用 ⇒ 仍是静默（这就是门禁点名的那种形态）"
    print("[CHK] 人造触发：bytes_may_undercount=%d 样本=%s swallow 调用=%d 次"
          % (d["bytes_may_undercount"], str(o["size_error_sample"])[:60], len(calls)))


def test_C3_反例_正常扫描不得乱报字节少算():
    """反例：真实 `%TEMP%` 扫描里 `bytes_may_undercount` 应为 0（否则该分支会在正常路径乱报）。"""
    m = _load()
    o = m.outcome_scan()
    bad = {k: v["bytes_may_undercount"] for k, v in o["prefixes"].items()
           if v.get("bytes_may_undercount")}
    assert bad == {}, ("正常扫描不该有 getsize 失败（若真出现 ⇒ 可能是权限/竞态，值得看样本）", bad)
    assert o.get("capped") is False, "枚举达上限 ⇒ 读数不完整（应调 OUTCOME_MAX_FILES 或查病态目录）"
