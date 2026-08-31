# -*- coding: utf-8 -*-
"""trinity/brain/meta_improvement.py — 元改进（EXECUTION 258，大脑化）。

借鉴 HyperAgents（Meta 2026：改进"改进自己的方式"）——元元学习：
不仅从经验改进，还评估"改进方法"本身是否有效并调整。

Trinity 现在：
  evaluate_method(history): 评估各改进方法有效性（哪些有效）
  adjust_method(): 调整改进策略（有效加权/无效替换）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/meta_improvement.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"method_scores": {}, "history": []}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def record_outcome(method: str, success: bool) -> dict:
    """记录改进方法的结果（元元学习数据）。"""
    st = _load()
    ms = st["method_scores"].get(method, {"ok": 0, "fail": 0})
    if success:
        ms["ok"] += 1
    else:
        ms["fail"] += 1
    st["method_scores"][method] = ms
    st["history"].append({"method": method, "success": success,
                          "ts": __import__("time").time()})
    st["history"] = st["history"][-50:]
    _save(st)
    return {"method": method, "ok": ms["ok"], "fail": ms["fail"]}


def evaluate_method() -> dict:
    """评估改进方法：各方法成功率（有效/无效）。"""
    st = _load()
    ms = st.get("method_scores", {})
    evaluated = []
    for method, counts in ms.items():
        total = counts["ok"] + counts["fail"]
        if total >= 2:
            rate = counts["ok"] / total
            evaluated.append({"method": method, "success_rate": round(rate, 2),
                              "effective": rate >= 0.6, "samples": total})
    evaluated.sort(key=lambda x: x["success_rate"], reverse=True)
    return {"methods": evaluated, "count": len(evaluated)}


def adjust_method() -> dict:
    """调整改进策略：有效方法优先/无效方法替换。"""
    ev = evaluate_method()
    effective = [m["method"] for m in ev["methods"] if m["effective"]]
    ineffective = [m["method"] for m in ev["methods"] if not m["effective"]]
    st = _load()
    st["preferred_methods"] = effective
    st["retired_methods"] = ineffective
    _save(st)
    return {"preferred": effective, "retired": ineffective,
            "note": "改进方式自我优化（有效优先/无效退役）"}
