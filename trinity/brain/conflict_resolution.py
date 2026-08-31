# -*- coding: utf-8 -*-
"""trinity/brain/conflict_resolution.py — 确定性冲突解决（EXECUTION 275）。

借鉴 Deterministic Recipe for Memory Conflict Resolution（2026）——
新旧记忆冲突的确定性判定（新鲜度/来源可信度/价值规则——不靠 LLM）。

与撤销（239 标记）互补：撤销=标记旧；解决=决策判定。
Trinity 现在：
  resolve(new_info, old_info): 确定性冲突判定（规则打分）
"""
import os
import sys
import json


def resolve(new_info: dict, old_info: dict) -> dict:
    """确定性冲突解决：新鲜度×可信度×价值 规则判定。"""
    # 新鲜度（新信息默认更新——但需满足条件）
    new_fresh = new_info.get("is_new", True)
    # 来源可信度
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.source_credibility import credibility
        new_cred = credibility(new_info.get("source", "unknown"))
        old_cred = credibility(old_info.get("source", "unknown"))
    except Exception:
        new_cred = old_cred = 0.5
    # 价值
    new_val = float(new_info.get("importance", 0.5))
    old_val = float(old_info.get("importance", 0.5))

    # 判定规则
    if new_fresh and new_cred >= old_cred and new_val >= old_val * 0.8:
        verdict = "replace"
        reason = "新信息更新（新鲜+可信不低+价值相近）"
    elif new_cred > old_cred + 0.3:
        verdict = "replace"
        reason = "新来源显著更可信"
    elif new_fresh and new_val > old_val:
        verdict = "replace"
        reason = "新信息价值更高"
    else:
        verdict = "keep"
        reason = "旧信息仍有效（新信息优势不足）"

    action = {"replace": "新胜出——更新记忆", "keep": "旧保留——新信息降级"}[verdict]
    return {"verdict": verdict, "reason": reason, "action": action,
            "scores": {"new_cred": round(new_cred, 2), "old_cred": round(old_cred, 2),
                       "new_val": new_val, "old_val": old_val}}


def resolve_batch(conflicts: list) -> dict:
    """批量冲突解决。"""
    results = []
    for c in conflicts[:10]:
        r = resolve(c.get("new", {}), c.get("old", {}))
        results.append({"pair": c.get("name", "?"), "verdict": r["verdict"]})
    return {"results": results, "count": len(results)}
