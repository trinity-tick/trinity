# -*- coding: utf-8 -*-
"""trinity/brain/memory_lineage.py — 记忆谱系（EXECUTION 270，大脑化）。

借鉴 MemLineage（2026：Lineage-Guided Enforcement）——记忆的完整
来源链（每个记忆的来源/派生关系——可追踪到源头）。

与审计（记录操作）互补：审计=操作日志；谱系=来源关系。
Trinity 现在：
  record_lineage(memory_id, source, derived_from): 记录谱系
  lineage_trace(memory_id): 追踪来源链
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/memory_lineage.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"lineage": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def record_lineage(memory_id: str, source: str = "manual",
                   derived_from: str = "") -> dict:
    """记录记忆谱系：来源 + 派生关系。"""
    st = _load()
    st["lineage"][memory_id] = {
        "source": str(source)[:50],
        "derived_from": str(derived_from)[:50] if derived_from else None,
        "ts": time.time(),
    }
    st["lineage"] = dict(list(st["lineage"].items())[-200:])
    _save(st)
    return {"recorded": True, "memory_id": memory_id[:12], "source": source[:30]}


def lineage_trace(memory_id: str, depth: int = 3) -> dict:
    """谱系追踪：沿派生链回溯到源头。"""
    st = _load()
    chain = []
    current = memory_id
    for _ in range(depth):
        entry = st["lineage"].get(current)
        if not entry:
            break
        chain.append({"memory": current[:12], "source": entry["source"],
                      "derived_from": entry["derived_from"]})
        if not entry.get("derived_from"):
            break
        current = entry["derived_from"]
    return {"memory_id": memory_id[:12], "chain": chain,
            "depth": len(chain),
            "note": "谱系：来源可追踪（MemLineage）"}


def lineage_report() -> dict:
    """谱系统计。"""
    st = _load()
    return {"lineage_entries": len(st.get("lineage", {})),
            "note": "记忆来源/派生关系已记录"}
