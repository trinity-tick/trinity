# -*- coding: utf-8 -*-
"""trinity/brain/versioned_memory.py — 版本化记忆（EXECUTION 340）。

借鉴 GitOfThoughts（2026：Version-Controlled Reasoning——Replay,
Diff, Merge）——Git 式记忆版本控制：记忆可提交/差异/重放
（回滚/分支——推理可追溯）。

与事务（原子）互补：事务=原子提交；本模块=版本管理。
Trinity 现在：
  commit(memory): 版本提交
  diff(v1, v2): 版本差异
  replay(version): 重放（回到版本）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/versioned_memory.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"versions": [], "head": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def commit(memory: str) -> dict:
    """版本提交：快照记忆状态。"""
    st = _load()
    st["versions"].append({"content": str(memory)[:80], "ts": time.time()})
    st["head"] = len(st["versions"]) - 1
    _save(st)
    return {"version": st["head"], "committed": True,
            "note": f"记忆提交 v{st['head']}（共 {len(st['versions'])} 版）"}


def diff(v1: int, v2: int) -> dict:
    """版本差异：两版本内容对比。"""
    st = _load()
    versions = st.get("versions", [])
    if v1 < 0 or v2 < 0 or v1 >= len(versions) or v2 >= len(versions):
        return {"error": "版本不存在"}
    c1, c2 = versions[v1]["content"], versions[v2]["content"]
    # 简单差异（共享词）
    w1 = set(c1)
    w2 = set(c2)
    added = list(w2 - w1)[:5]
    removed = list(w1 - w2)[:5]
    return {"v1": v1, "v2": v2, "added": added, "removed": removed,
            "changed": bool(added or removed),
            "note": f"v{v1} → v{v2}：{'有变化' if added or removed else '无变化'}"}


def replay(version: int) -> dict:
    """重放：回到指定版本。"""
    st = _load()
    versions = st.get("versions", [])
    if version < 0 or version >= len(versions):
        return {"error": "版本不存在"}
    st["head"] = version
    _save(st)
    return {"replayed": True, "version": version,
            "content": versions[version]["content"][:60],
            "note": f"重放 v{version}（记忆回到该版本）"}
