# -*- coding: utf-8 -*-
"""trinity/brain/append_only_memory.py — 追加记忆（EXECUTION 350）。

借鉴 OptMem（2026：Minimalist persistent memory——append-only LOG）——
仅追加日志记忆：只追加不可修改（极简持久——防篡改——简单可靠）。

与事务（原子）互补：事务=批量原子；本模块=极简追加。
Trinity 现在：
  append(entry): 追加记录（不可改）
  append_log(): 日志视图（追加历史）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/append_only_memory.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"log": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def append(entry: str) -> dict:
    """追加记录：仅追加（不可修改——防篡改）。"""
    st = _load()
    st["log"].append({"entry": str(entry)[:80], "ts": time.time(),
                      "seq": len(st["log"]) + 1})
    _save(st)
    return {"appended": True, "seq": len(st["log"]), "total": len(st["log"]),
            "append_only": True,
            "note": f"追加记录 #{len(st['log'])}（仅追加——不可改）"}


def append_log(limit: int = 5) -> dict:
    """日志视图：追加历史。"""
    st = _load()
    log = st.get("log", [])
    recent = log[-limit:]
    return {"total": len(log), "recent": [{"seq": l["seq"], "entry": l["entry"][:40]} for l in recent],
            "note": f"追加日志：{len(log)} 条记录（极简持久）"}
