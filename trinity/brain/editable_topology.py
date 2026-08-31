# -*- coding: utf-8 -*-
"""trinity/brain/editable_topology.py — 记忆拓扑编辑（EXECUTION 327）。

借鉴 Realtime Editable Memory Topology（2026：From Simulated
Empathy to Structural Attunement）——记忆拓扑实时可编辑：
记忆关联权重实时调整（情感调谐——结构共鸣）。

与 SAGE（图谱进化）互补：SAGE=自动进化；本模块=实时可编辑。
Trinity 现在：
  edit_connection(a, b, weight): 拓扑编辑（实时调整关联）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/editable_topology.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"edges": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def edit_connection(a: str, b: str, weight: float = 0.5) -> dict:
    """拓扑编辑：实时调整记忆关联权重。"""
    st = _load()
    key = "|".join(sorted([str(a)[:20], str(b)[:20]]))
    st["edges"][key] = {"a": str(a)[:20], "b": str(b)[:20],
                        "weight": min(1.0, max(0.0, weight)),
                        "ts": __import__("time").time()}
    st["edges"] = dict(list(st["edges"].items())[-100:])
    _save(st)
    return {"edge": key, "weight": st["edges"][key]["weight"],
            "edited": True,
            "note": f"拓扑编辑：『{str(a)[:15]}』↔『{str(b)[:15]}』权重 {weight}（实时生效）"}


def attune(memory_a: str, memory_b: str, emotion: float = 0.5) -> dict:
    """情感调谐：按情感强度调整关联（结构共鸣）。"""
    # 情感调谐：积极情感 → 加强关联；消极 → 减弱
    weight = 0.5 + (emotion - 0.5) * 0.6
    return edit_connection(memory_a, memory_b, weight)


def topology_report() -> dict:
    """拓扑状态。"""
    st = _load()
    edges = st.get("edges", {})
    return {"edges": len(edges),
            "avg_weight": round(sum(e["weight"] for e in edges.values()) / max(len(edges), 1), 2),
            "note": "记忆拓扑实时可编辑（情感调谐——Structural Attunement）"}
