# -*- coding: utf-8 -*-
"""trinity/brain/world_rehearsal.py — 世界排练（EXECUTION 243，大脑化）。

借鉴 EnvACE（2026：World Rehearsal for Agentic RL）——行动前在
内部排练（模拟环境动态→预测结果），选择最佳行动。大脑对应：
"预演"（行动前在脑中模拟——运动皮层的前馈模拟）。

与心理模拟（想象情境）区分：模拟=设想；排练=行动预演择优。
Trinity 现在：
  rehearse(action, context): 行动排练（模拟→预测结果）
  choose_best(candidates): 排练多行动 → 选预测最佳
"""
import os
import sys
import json


def rehearse(action: str, context: str = "", prior_score: float = 0.5) -> dict:
    """行动排练：内部模拟行动 → 预测结果（基于经验）。"""
    # 经验参考（检索相关记忆）
    prior = 0.5
    evidence = []
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity import Trinity
        m = Trinity(adapter="postgresql")
        r = m.search_hybrid(action[:30], top_k=2)
        items = r if isinstance(r, list) else r.get("results", [])
        if items:
            prior = 0.6
            evidence = [str(x.get("content") or "")[:40] for x in items[:2]]
    except Exception:
        pass
    # 风险评估（行动词）
    risk = 0.3
    if any(w in action for w in ("删除", "清空", "覆盖", "强制")):
        risk = 0.7
        evidence.append("高风险操作（删除/覆盖类）")
    predicted = round(prior_score * 0.7 + prior * 0.3 - risk * 0.2, 2)
    return {"action": str(action)[:40], "predicted_outcome": predicted,
            "risk": risk, "evidence": evidence[:2],
            "verdict": "可行" if predicted >= 0.5 else "谨慎"}


def choose_best(candidates: list) -> dict:
    """排练多个行动 → 选预测最佳。"""
    rehearsed = []
    for c in candidates[:5]:
        r = rehearse(c.get("action", ""), c.get("context", ""),
                     c.get("prior", 0.5))
        rehearsed.append(r)
    rehearsed.sort(key=lambda x: x["predicted_outcome"], reverse=True)
    return {"best": rehearsed[0] if rehearsed else None,
            "all": rehearsed, "count": len(rehearsed)}
