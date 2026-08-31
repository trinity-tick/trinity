# -*- coding: utf-8 -*-
"""trinity/brain/proactive_initiative.py — 主动发起（EXECUTION 213，大脑化）。

借鉴 Anima（Neuroscience-Inspired Architecture with Proactive Initiative）：
持久 Agent 的主动发起——基于内部状态自主开始行动（不等待刺激）。

Trinity 的主动理由源：
  - 好奇主题（探索未知）
  - 预测缺口（surprise）
  - 健康信号（内感受）
  - 自省建议（待改进）
  - 知识缺口（未知感知）

initiate(): 聚合主动理由 → 主动行动计划（发起行动）
"""
import os
import sys
import json


def collect_initiatives() -> dict:
    """聚合内部主动理由（不依赖外部刺激）。"""
    reasons = []

    # 1) 好奇主题（探索未知）
    try:
        sys.path.insert(0, r"D:\\trinity-code")
        from trinity.brain.curiosity import compute_curiosity
        topics = compute_curiosity(top_k=2)
        if topics:
            reasons.append({"source": "curiosity", "action": "explore",
                            "detail": topics[0]["topic"]})
    except Exception:
        pass

    # 2) 预测缺口
    try:
        pf = os.path.expanduser("~/.trinity/predictive_state.json")
        if os.path.exists(pf):
            st = json.load(open(pf, encoding="utf-8"))
            hist = st.get("history", [])
            if hist and hist[-1].get("surprises"):
                reasons.append({"source": "prediction", "action": "investigate",
                                "detail": "预测缺口存在"})
    except Exception:
        pass

    # 3) 健康信号（内感受）
    try:
        from trinity.brain.action_loop import ActionLoop
        al = ActionLoop()
        ic = al.interoceptive_check()
        if ic.get("internal_priority"):
            reasons.append({"source": "interoception", "action": "self_heal",
                            "detail": "内部异常"})
    except Exception:
        pass

    # 4) 自省待改进
    try:
        sf = os.path.expanduser("~/.trinity/action_loop_stats.json")
        if os.path.exists(sf):
            stats = json.load(open(sf, encoding="utf-8"))
            weak = [k for k, s in stats.items()
                    if s.get("ok", 0) + s.get("fail", 0) >= 2
                    and s.get("ok", 0) / max(s.get("ok", 0) + s.get("fail", 0), 1) < 0.5]
            if weak:
                reasons.append({"source": "reflection", "action": "improve",
                                "detail": weak[0]})
    except Exception:
        pass

    return {"reasons": reasons, "count": len(reasons),
            "initiative_score": min(len(reasons) * 25, 100)}


def initiate() -> dict:
    """主动发起：基于内部理由发起行动。"""
    r = collect_initiatives()
    actions = []
    for reason in r["reasons"]:
        actions.append({"initiated": reason["action"], "from": reason["source"],
                        "detail": str(reason["detail"])[:40]})
    return {"initiative": r["initiative_score"], "actions": actions,
            "active": len(actions) > 0}
