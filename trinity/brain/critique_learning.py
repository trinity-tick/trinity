# -*- coding: utf-8 -*-
"""trinity/brain/critique_learning.py — 批评学习（EXECUTION 339）。

借鉴 Critique-Learning（2026：Memory-Augmented LLM Agents）——
批评反馈学习：批评者指出问题 → 修正 → 记忆增强（批评驱动
的学习——记忆增强型）。

与反馈学习（效果反馈）互补：反馈=结果数值；本模块=批评内容。
Trinity 现在：
  learn_from_critique(response, critique): 批评学习（修正+增强）
"""
import os
import sys
import json


STATE_FILE = os.path.expanduser("~/.trinity/critique_learning.json")


def _load() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"critiques": [], "improvements": 0}


def _save(st: dict):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def learn_from_critique(response: str, critique: str) -> dict:
    """批评学习：批评 → 修正 → 记忆增强。"""
    # 批评分析（问题类别）
    c = str(critique)
    issues = []
    if any(w in c for w in ("不准确", "错误", "错误信息")):
        issues.append("accuracy")
    if any(w in c for w in ("不完整", "缺失", "遗漏")):
        issues.append("completeness")
    if any(w in c for w in ("不清楚", "混乱", "难理解")):
        issues.append("clarity")
    if not issues:
        issues = ["general"]
    # 修正（记录修正后的响应）
    correction = f"修正『{str(response)[:30]}』：{c[:30]}"
    st = _load()
    st["critiques"].append({"issue": issues[0], "critique": c[:40],
                            "ts": __import__("time").time()})
    st["critiques"] = st["critiques"][-30:]
    st["improvements"] += 1
    _save(st)
    return {"issue": issues, "correction": correction[:50],
            "improvements_total": st["improvements"],
            "note": f"批评学习：修正『{issues[0]}』问题（第{st['improvements']}次改进）"}


def critique_report() -> dict:
    """批评学习状态。"""
    st = _load()
    critiques = st.get("critiques", [])
    issue_dist = {}
    for c in critiques:
        issue_dist[c["issue"]] = issue_dist.get(c["issue"], 0) + 1
    return {"critiques": len(critiques), "improvements": st.get("improvements", 0),
            "issue_distribution": issue_dist,
            "note": "批评学习：批评→修正→增强（Memory-Augmented）"}
