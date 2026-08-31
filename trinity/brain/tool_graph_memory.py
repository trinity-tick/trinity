# -*- coding: utf-8 -*-
"""trinity/brain/tool_graph_memory.py — 工具图记忆（EXECUTION 313）。

借鉴 SEARL（2026：Joint Optimization of Policy and Tool Graph
Memory）——策略与工具的记忆图：工具使用经验的图结构（哪些
工具组合有效——联合优化）。

Trinity 现在：
  tool_experience(tool, outcome): 工具经验记录
  tool_graph(): 工具图（工具关联——组合推荐）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/tool_graph_memory.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tools": {}, "combos": {}}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def tool_experience(tool: str, outcome_score: float = 0.5,
                    with_tool: str = "") -> dict:
    """工具经验：记录工具效果 + 组合。"""
    st = _load()
    t = st["tools"].get(tool, {"score": 0.5, "uses": 0})
    t["score"] = round(min(1.0, max(0.0, t["score"] * 0.8 + outcome_score * 0.2)), 2)
    t["uses"] += 1
    st["tools"][tool] = t
    if with_tool and with_tool != tool:
        key = "|".join(sorted([tool, with_tool]))
        c = st["combos"].get(key, {"count": 0, "score": 0.0})
        c["count"] += 1
        c["score"] = round(min(1.0, c["score"] + outcome_score * 0.2), 2)
        st["combos"][key] = c
    _save(st)
    return {"tool": str(tool)[:20], "score": t["score"], "uses": t["uses"],
            "combo": with_tool if with_tool else None}


def tool_graph(tool: str = "") -> dict:
    """工具图：工具效果排序 + 组合推荐。"""
    st = _load()
    tools = sorted(st.get("tools", {}).items(), key=lambda x: -x[1]["score"])
    # 组合推荐（与指定工具配合最有效的）
    combos = []
    if tool:
        for key, c in st.get("combos", {}).items():
            if tool in key.split("|"):
                other = [x for x in key.split("|") if x != tool][0]
                combos.append({"with": other, "score": c["score"], "count": c["count"]})
        combos.sort(key=lambda x: -x["score"])
    return {"top_tools": [(t, d["score"]) for t, d in tools[:5]],
            "recommended_combos": combos[:3],
            "note": "工具图：效果排序+组合推荐（SEARL 联合优化）"}
