# -*- coding: utf-8 -*-
"""trinity/brain/delta_memory.py — 增量记忆（EXECUTION 316，大脑化）。

借鉴 DeltaMem（2026：Incremental Experience Memory via Residual
Trees）——只存增量：对比新旧 → 只存变化部分（残差——不重复
存全量——存储效率）。

Trinity 现在：
  store_delta(prev, new): 增量存储（差异提取）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/delta_memory.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"deltas": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def store_delta(prev: str, new: str) -> dict:
    """增量存储：提取差异（新增/变化内容）。"""
    p, n = str(prev), str(new)
    # 差异提取：新内容中不在旧内容的部分（2 字词级）
    p_words = set()
    for i in range(len(p) - 1):
        if "\u4e00" <= p[i] <= "\u9fff" and "\u4e00" <= p[i+1] <= "\u9fff":
            p_words.add(p[i:i+2])
    n_words = []
    for i in range(len(n) - 1):
        if "\u4e00" <= n[i] <= "\u9fff" and "\u4e00" <= n[i+1] <= "\u9fff":
            n_words.append(n[i:i+2])
    delta_words = [w for w in n_words if w not in p_words]
    delta = "".join(delta_words)[:80] if delta_words else "(结构无变化)"
    st = _load()
    st["deltas"].append({"prev_len": len(p), "new_len": len(n),
                         "delta": delta, "ts": __import__("time").time()})
    st["deltas"] = st["deltas"][-30:]
    _save(st)
    return {"prev_len": len(p), "new_len": len(n), "delta": delta[:50],
            "saved_ratio": round(len(delta) / max(len(n), 1), 2),
            "note": f"增量存储：只存差异（{len(delta)}/{(len(n))} 字符——残差）"}


def delta_report() -> dict:
    """增量效率。"""
    st = _load()
    deltas = st.get("deltas", [])
    avg_saved = sum(d.get("saved_ratio", 1) for d in deltas) / max(len(deltas), 1)
    return {"deltas": len(deltas), "avg_saved_ratio": round(avg_saved, 2),
            "note": "增量记忆：只存差异（存储效率优化）"}
