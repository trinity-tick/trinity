# -*- coding: utf-8 -*-
"""trinity/brain/prospective_memory.py — 前瞻记忆（EXECUTION 236，大脑化）。

借鉴 PM-Bench（2026：Prospective Memory in LLM Agents）——前瞻记忆：
记住"将来要做的事"（意图编码→保持→触发检索）。大脑对应：
"记得下班去买牛奶"——面向未来的记忆。

Trinity 现在：
  encode_intention(task, trigger, when): 编码前瞻意图（持久化）
  check_intentions(): 检查到期/触发的意图 → 提醒（行动）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/prospective_memory.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"intentions": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def encode_intention(task: str, trigger: str = "", due_hours: float = 24.0) -> dict:
    """编码前瞻意图：将来要做的事（持久化）。"""
    st = _load()
    intention = {
        "task": str(task)[:100],
        "trigger": str(trigger)[:50],
        "due": time.time() + due_hours * 3600,
        "created": time.time(),
        "done": False,
    }
    st["intentions"].append(intention)
    st["intentions"] = st["intentions"][-50:]
    _save(st)
    return {"encoded": True, "task": intention["task"], "id": len(st["intentions"]) - 1,
            "due_in_h": due_hours}


def check_intentions(trigger_context: str = "") -> dict:
    """检查前瞻意图：到期或触发条件满足 → 提醒。"""
    st = _load()
    now = time.time()
    due_now, pending = [], 0
    for i, it in enumerate(st.get("intentions", [])):
        if it.get("done"):
            continue
        pending += 1
        due = it.get("due", 0) <= now
        triggered = bool(it.get("trigger")) and trigger_context and it["trigger"] in trigger_context
        if due or triggered:
            due_now.append({"id": i, "task": it["task"], "reason": "到期" if due else "触发"})
    return {"due_now": due_now, "pending": pending, "reminded": len(due_now)}


def mark_done(intention_id: int) -> dict:
    """完成意图（前瞻记忆闭环）。"""
    st = _load()
    if 0 <= intention_id < len(st["intentions"]):
        st["intentions"][intention_id]["done"] = True
        _save(st)
        return {"completed": True, "id": intention_id}
    return {"completed": False}
