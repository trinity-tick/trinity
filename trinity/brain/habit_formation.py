# -*- coding: utf-8 -*-
"""trinity/brain/habit_formation.py — 习惯形成（EXECUTION 218，大脑化）。

基底节习惯回路：重复成功动作 → 习惯化（自动执行不经深思）。
节省认知资源（注意力可转向他处）——大脑的自动化机制。

Trinity 现在：
  track(action, done): 行动结果记录（成功计数）
  form_habits(): 重复成功（>=3 连续/累计）→ 习惯化
  auto_execute(habit): 习惯自动执行（跳过深思——标记/直接跑）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/habits_state.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tracking": {}, "habits": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def track(action: str, done: bool) -> dict:
    """行动结果记录（成功/失败计数）。"""
    st = _load()
    t = st["tracking"].get(action, {"ok": 0, "fail": 0})
    if done:
        t["ok"] += 1
    else:
        t["fail"] += 1
    st["tracking"][action] = t
    # 习惯形成：连续成功 >= 3 且成功率 >= 0.7
    total = t["ok"] + t["fail"]
    if total >= 3 and t["ok"] / total >= 0.7:
        st["habits"][action] = {"strength": min(1.0, t["ok"] / 10.0),
                                "formed": True}
    _save(st)
    return {"action": action, "ok": t["ok"], "fail": t["fail"],
            "habit_formed": action in st["habits"]}


def habits() -> dict:
    """当前习惯列表。"""
    return _load().get("habits", {})


def auto_execute(action: str, fallback=None) -> dict:
    """习惯自动执行：习惯存在 → 自动（标记 automatized）；否则走正常（fallback）。"""
    h = habits()
    if action in h:
        return {"automatized": True, "action": action,
                "strength": h[action]["strength"],
                "note": "习惯自动执行（节省深思）"}
    if fallback:
        return fallback()
    return {"automatized": False, "action": action, "note": "非习惯（正常处理）"}
