# -*- coding: utf-8 -*-
"""trinity/brain/temporal_graph.py — 时态图（EXECUTION 364）。

借鉴 TAPE（2026：Temporal Graph-Based Memory for Personal Agents）——
时态图记忆：带时间戳的图边（关联 + 时间——关系随时间演变）。

与时空记忆（时间坐标）互补：时空=记忆时间；本模块=图边时间。
Trinity 现在：
  temporal_edge(a, b, time): 时态边（时间戳关联）
"""
import os
import sys
import json
import time


STATE_FILE = os.path.expanduser("~/.trinity/temporal_graph.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"edges": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def temporal_edge(a: str, b: str, ts: float = None) -> dict:
    """时态边：带时间戳的关联（关系随时间演变）。"""
    ts = ts or time.time()
    st = _load()
    key = "|".join(sorted([str(a)[:20], str(b)[:20]]))
    st["edges"].append({"key": key, "a": str(a)[:20], "b": str(b)[:20],
                        "ts": ts})
    st["edges"] = st["edges"][-100:]
    _save(st)
    return {"edge": key, "timestamp": round(ts, 0),
            "note": f"时态边：『{str(a)[:12]}』↔『{str(b)[:12]}』@{round(ts,0)}（关系随时间演变）"}


def temporal_query(a: str, b: str) -> dict:
    """时态查询：两节点关联的历史（时间线）。"""
    st = _load()
    key = "|".join(sorted([str(a)[:20], str(b)[:20]]))
    history = [{"ts": round(e["ts"], 0)} for e in st.get("edges", []) if e["key"] == key]
    return {"edge": key, "occurrences": len(history),
            "timeline": history[-5:],
            "evolving": len(history) >= 2,
            "note": f"时态查询：'{key}' {len(history)} 次关联（时间线可追踪）"}
