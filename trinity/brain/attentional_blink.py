# -*- coding: utf-8 -*-
"""trinity/brain/attentional_blink.py — 注意瞬盲（EXECUTION 301，大脑化）。

借鉴 Attentional Blink（2026：注意难治期建模）——真实大脑现象：
快速连续两个刺激时，第二个被"瞬盲"漏过（注意恢复期——
约 200-500ms 内第二目标识别率骤降）。

Trinity 现在：
  blink_gate(interval_ms): 瞬盲门（处理间隔→第二目标可识别性）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/attentional_blink.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_target_ts": 0.0, "blinks": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def blink_gate(interval_ms: float = 300.0) -> dict:
    """瞬盲门：目标间隔 → 第二目标可识别性（200-500ms 难治）。"""
    st = _load()
    now = time.time() * 1000
    last = st.get("last_target_ts", 0.0)
    # 间隔：传入参数优先（模拟），否则真实时间差
    if interval_ms > 0:
        elapsed = float(interval_ms)
    else:
        elapsed = now - last if last else 1000.0
    st["last_target_ts"] = now

    # 瞬盲窗口：<200ms 全盲；200-500ms 部分；>500ms 正常
    if elapsed < 200:
        recognition = 0.0
        state = "blink"  # 完全瞬盲
    elif elapsed < 500:
        recognition = round((elapsed - 200) / 300, 2)  # 部分恢复
        state = "recovering"
    else:
        recognition = 1.0
        state = "clear"
    if state == "blink":
        st["blinks"] += 1
    _save(st)
    return {"interval_ms": round(elapsed, 1), "state": state,
            "recognition": recognition,
            "blinks_total": st.get("blinks", 0),
            "note": f"注意瞬盲：间隔 {round(elapsed)}ms → {state}（{'第二目标漏过' if state=='blink' else '可识别' if state=='clear' else '部分恢复'}）"}


def blink_report() -> dict:
    """瞬盲统计（注意恢复质量）。"""
    st = _load()
    return {"blinks": st.get("blinks", 0),
            "note": "注意瞬盲建模：快速刺激的难治期（Attentional Blink）"}
