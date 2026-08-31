# -*- coding: utf-8 -*-
"""trinity/brain/signal_context.py — 信号上下文（EXECUTION 366）。

借鉴 NeuroSkill（MIT Media Lab 2026：Proactive Real-Time System——
Signal-Driven Context Injection）——信号驱动上下文注入：外部
信号 → 上下文主动更新（不等待查询——主动循环）。

与反思上下文（反思驱动）互补：反思=内部；本模块=信号驱动。
Trinity 现在：
  inject_on_signal(signal): 信号注入（信号→上下文更新）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/signal_context.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"signals": [], "active_context": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def inject_on_signal(signal: str, priority: float = 0.5) -> dict:
    """信号注入：信号 → 上下文主动更新（不等待查询）。"""
    st = _load()
    st["signals"].append({"signal": str(signal)[:40], "priority": priority,
                          "ts": __import__("time").time()})
    st["signals"] = st["signals"][-20:]
    # 高优先信号注入上下文（主动）
    if priority >= 0.6:
        st["active_context"].append({"injected": str(signal)[:40], "source": "signal"})
        st["active_context"] = st["active_context"][-7:]
        injected = True
    else:
        injected = False
    _save(st)
    return {"signal": str(signal)[:30], "priority": round(priority, 2),
            "injected": injected,
            "context_size": len(st.get("active_context", [])),
            "note": f"信号注入：{'高优先——已注入上下文' if injected else '低优先——记录待用'}（NeuroSkill 主动循环）"}


def context_report() -> dict:
    """信号上下文状态。"""
    st = _load()
    return {"signals": len(st.get("signals", [])),
            "active": len(st.get("active_context", [])),
            "note": "信号驱动上下文：主动注入（不等待查询——Proactive Loop）"}
